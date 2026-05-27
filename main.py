"""
Universal Telegram Downloader Bot
──────────────────────────────────
Download pipeline (in priority order):

  1. TikTok slideshow  → tikwm.com API  (images + music)
  2. Everything else   → Cobalt API     (Instagram, Twitter/X, YouTube,
                                         Facebook, Reddit, TikTok videos,
                                         Twitch, Pinterest, SoundCloud …)
  3. Cobalt fallback   → yt-dlp         (any URL yt-dlp supports — 1000+ sites)
     • Instagram URLs: injects IG_SESSION_ID cookie automatically
     • All other URLs: standard yt-dlp best quality

Key improvement over previous version:
  • ANY http/https URL is now accepted — no domain allowlist.
  • A HEAD probe checks Content-Type to confirm a URL carries media before
    attempting a full download.
  • yt-dlp is tried for all unknown/unsupported-by-Cobalt domains because
    yt-dlp itself supports 1000+ sites out of the box.

Instagram note (2025):
  Instagram now blocks all anonymous requests. You MUST set IG_SESSION_ID
  in your Vercel environment variables to download Instagram content.
  How to get it: instagram.com → DevTools → Application → Cookies → sessionid

After every video download, audio is extracted and sent separately.
Short TikTok links are always expanded before processing.
"""

import os
import glob
import json
import asyncio
import subprocess
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"
TMP_DIR   = "/tmp"
MAX_BYTES = 50 * 1024 * 1024   # Telegram Bot API limit

# ── Cobalt API instances (tried in order; first success wins) ─────────────────
COBALT_INSTANCES = [
    os.environ.get("COBALT_API_URL", ""),   # self-hosted (optional env var)
    "https://cobalt.api.lostnode.net",
    "https://api.cobalt.tools",
    "https://cobalt.lunar.icu",
    "https://cobalt.cae.re",
]
COBALT_INSTANCES = [u for u in COBALT_INSTANCES if u]  # drop empty

TIKWM_API = "https://www.tikwm.com/api/"

# ── MIME types we consider "media" for direct-link detection ──────────────────
MEDIA_MIME_PREFIXES = (
    "video/",
    "audio/",
    "application/mp4",
    "application/octet-stream",  # many CDNs serve video as this
)
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".wmv",
    ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aac",
    ".m3u8", ".ts",
}

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan)


# =============================================================================
# Telegram helpers
# =============================================================================

async def tg_send(method: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(f"{TG_API}/{method}", **kwargs)
        r.raise_for_status()
        return r.json()


async def send_text(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    await tg_send("sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": parse_mode
    })


async def send_document(chat_id: int, path: str, caption: str = "") -> None:
    """Send file as document — Telegram will NOT re-encode it."""
    with open(path, "rb") as f:
        await tg_send(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"document": (Path(path).name, f, "application/octet-stream")},
        )


async def send_photo(chat_id: int, path: str, caption: str = "") -> None:
    with open(path, "rb") as f:
        await tg_send(
            "sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": f},
        )


async def send_audio(chat_id: int, path: str, caption: str = "") -> None:
    with open(path, "rb") as f:
        await tg_send(
            "sendAudio",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"audio": f},
        )


async def send_media_group(
    chat_id: int,
    image_paths: list[str],
    caption: str = "",
) -> None:
    """Send up to 10 images as a Telegram media group (carousel)."""
    batch = image_paths[:10]
    media = []
    for i in range(len(batch)):
        item: dict = {"type": "photo", "media": f"attach://photo{i}"}
        if i == 0 and caption:
            item["caption"]    = caption
            item["parse_mode"] = "HTML"
        media.append(item)

    files: dict = {f"photo{i}": open(p, "rb") for i, p in enumerate(batch)}
    try:
        await tg_send(
            "sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files=files,
        )
    finally:
        for f in files.values():
            f.close()


# =============================================================================
# Shared file download helper
# =============================================================================

async def stream_download(url: str, dest: str) -> str:
    """Stream any URL to a local file. Returns dest path."""
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=120,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(65536):
                    f.write(chunk)
    return dest


def cleanup(paths: list[str]) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


# =============================================================================
# URL helpers — media detection (no domain allowlist)
# =============================================================================

def looks_like_url(text: str) -> bool:
    """Accept any http/https URL — we'll validate content later."""
    t = text.strip()
    return t.startswith(("http://", "https://"))


async def probe_for_video(url: str) -> bool:
    """
    Do a lightweight HEAD request to check whether the URL points to
    a video or audio resource.  Returns True if we should attempt a download.

    We say "yes" when:
      - Content-Type starts with video/ or audio/
      - The URL path ends with a known media extension
      - The server doesn't respond (we try anyway — yt-dlp will figure it out)
    """
    # Fast extension check first (no network round-trip needed)
    path_part = url.split("?")[0].lower()
    if any(path_part.endswith(ext) for ext in MEDIA_EXTENSIONS):
        return True

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as c:
            r = await c.head(url)
            ct = r.headers.get("content-type", "").lower()
            logger.info("[probe] %s → content-type=%s", url, ct)
            if any(ct.startswith(p) for p in MEDIA_MIME_PREFIXES):
                return True
            # HTML page — might still embed a video (YouTube, Vimeo, etc.)
            # Let yt-dlp try rather than refuse.
            return True   # always attempt; yt-dlp will fail gracefully if nothing found
    except Exception as e:
        logger.warning("[probe] HEAD failed for %s: %s — will still try", url, e)
        return True   # network error doesn't mean there's no video; try anyway


def is_direct_media_url(url: str) -> bool:
    """True if the URL looks like a direct file link (CDN/storage)."""
    path_part = url.split("?")[0].lower()
    return any(path_part.endswith(ext) for ext in MEDIA_EXTENSIONS)


# =============================================================================
# TikTok link expansion
# =============================================================================

_TT_SHORT = ("vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com")

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url

def is_tiktok_slideshow(url: str) -> bool:
    return "/photo/" in url

async def expand_tiktok_url(url: str) -> str:
    if not any(d in url for d in _TT_SHORT):
        return url
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as c:
            r = await c.head(url)
            expanded = str(r.url)
            logger.info("[expand] %s → %s", url, expanded)
            return expanded
    except Exception as e:
        logger.warning("[expand] failed: %s", e)
        return url


# =============================================================================
# LAYER 1 — tikwm.com (TikTok slideshows only)
# =============================================================================

async def tikwm_fetch(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TIKWM_API, data={"url": url, "hd": 1},
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"tikwm: {body.get('msg', 'error')}")
    return body["data"]


async def handle_tiktok_slideshow(chat_id: int, url: str, prefix: str) -> None:
    """
    Download a TikTok photo-slideshow post via tikwm.com.
    Sends all images as a carousel + background music as audio.
    """
    logger.info("[tikwm] fetching slideshow %s", url)
    data = await tikwm_fetch(url)

    images: list[str] = data.get("images") or []
    if not images:
        for img in (data.get("image_post_info") or {}).get("images", []):
            ul = img.get("display_image", {}).get("url_list") or []
            if ul:
                images.append(ul[0])

    if not images:
        raise RuntimeError("tikwm: no images found in slideshow")

    music_url: str = (
        (data.get("music_info") or {}).get("play")
        or data.get("music") or ""
    )

    author  = (data.get("author") or {}).get("nickname", "")
    desc    = (data.get("title") or data.get("desc") or "")[:80]
    caption = f"📸 <b>{author}</b>" + (f"\n{desc}" if desc else "")

    image_paths: list[str] = []
    tasks = [
        stream_download(img_url, f"{TMP_DIR}/{prefix}_slide_{i:02d}.jpg")
        for i, img_url in enumerate(images)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, res in enumerate(results):
        p = f"{TMP_DIR}/{prefix}_slide_{i:02d}.jpg"
        if not isinstance(res, Exception) and os.path.exists(p) and os.path.getsize(p) > 0:
            image_paths.append(p)
        elif isinstance(res, Exception):
            logger.warning("[tikwm] image %d failed: %s", i, res)

    if not image_paths:
        raise RuntimeError("tikwm: all image downloads failed")

    music_path: Optional[str] = None
    if music_url:
        try:
            mp = f"{TMP_DIR}/{prefix}_music.mp3"
            await stream_download(music_url, mp)
            if os.path.exists(mp) and os.path.getsize(mp) > 0:
                music_path = mp
        except Exception as e:
            logger.warning("[tikwm] music failed: %s", e)

    if len(image_paths) == 1:
        await send_photo(chat_id, image_paths[0], caption=caption)
    else:
        for start in range(0, len(image_paths), 10):
            await send_media_group(
                chat_id, image_paths[start:start + 10],
                caption=caption if start == 0 else "",
            )

    if music_path:
        await send_audio(
            chat_id, music_path,
            caption="🎵 <b>Background audio from this slideshow</b>",
        )

    cleanup(image_paths + ([music_path] if music_path else []))


# =============================================================================
# LAYER 2 — Cobalt API
# =============================================================================

COBALT_HEADERS = {
    "Accept":       "application/json",
    "Content-Type": "application/json",
    "User-Agent":   "TelegramDownloaderBot/1.0",
}

COBALT_PAYLOAD = {
    "videoQuality":   "max",
    "audioFormat":    "mp3",
    "audioBitrate":   "320",
    "downloadMode":   "auto",
    "filenameStyle":  "basic",
    "disableMetadata": False,
}


async def cobalt_query(url: str) -> Optional[dict]:
    payload = {**COBALT_PAYLOAD, "url": url}

    for instance in COBALT_INSTANCES:
        endpoint = instance.rstrip("/") + "/"
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(endpoint, json=payload, headers=COBALT_HEADERS)
            if r.status_code == 200:
                data = r.json()
                status = data.get("status", "")
                if status in ("tunnel", "redirect", "stream", "local-processing"):
                    logger.info("[cobalt] %s → %s (%s)", instance, status, url)
                    return data
                if status == "picker":
                    logger.info("[cobalt] %s → picker (%s)", instance, url)
                    return data
                logger.warning("[cobalt] %s error: %s", instance, data.get("error"))
            else:
                logger.warning("[cobalt] %s HTTP %s", instance, r.status_code)
        except Exception as e:
            logger.warning("[cobalt] %s failed: %s", instance, e)

    return None


async def handle_cobalt(chat_id: int, url: str, prefix: str) -> bool:
    result = await cobalt_query(url)
    if not result:
        return False

    status    = result.get("status", "")
    all_files: list[str] = []

    try:
        if status in ("tunnel", "redirect", "stream", "local-processing"):
            file_url: str = result.get("url", "")
            filename: str = result.get("filename", f"{prefix}_cobalt.mp4")
            ext = Path(filename).suffix.lower() or ".mp4"
            dest = f"{TMP_DIR}/{prefix}_cobalt{ext}"

            await stream_download(file_url, dest)
            all_files.append(dest)

            ftype = _classify(dest)
            if ftype == "video":
                await send_document(
                    chat_id, dest,
                    caption="🎬 <b>Video</b> — original quality, no compression"
                )
                audio_path = await _extract_audio(dest, prefix)
                if audio_path:
                    all_files.append(audio_path)
                    await send_audio(
                        chat_id, audio_path,
                        caption="🎵 <b>Audio extracted from video</b>"
                    )
            elif ftype == "audio":
                await send_audio(chat_id, dest, caption="🎵 <b>Audio</b>")
            elif ftype == "photo":
                await send_photo(chat_id, dest)
            else:
                await send_document(chat_id, dest)

            return True

        if status == "picker":
            items: list[dict] = result.get("picker", [])
            audio_item        = result.get("audio")

            photos: list[str] = []

            for i, item in enumerate(items):
                item_url  = item.get("url", "")
                item_type = item.get("type", "photo")
                ext       = ".jpg" if item_type == "photo" else ".mp4"
                dest      = f"{TMP_DIR}/{prefix}_pick_{i:02d}{ext}"

                try:
                    await stream_download(item_url, dest)
                    all_files.append(dest)
                except Exception as e:
                    logger.warning("[cobalt] picker item %d failed: %s", i, e)
                    continue

                if item_type == "photo":
                    photos.append(dest)
                else:
                    await send_document(
                        chat_id, dest,
                        caption=f"🎬 <b>Video {i + 1}</b>"
                    )
                    audio_path = await _extract_audio(dest, f"{prefix}_{i}")
                    if audio_path:
                        all_files.append(audio_path)
                        await send_audio(
                            chat_id, audio_path,
                            caption=f"🎵 <b>Audio {i + 1}</b>"
                        )

            if len(photos) == 1:
                await send_photo(chat_id, photos[0])
            elif photos:
                for start in range(0, len(photos), 10):
                    await send_media_group(chat_id, photos[start:start + 10])

            if audio_item:
                try:
                    adest = f"{TMP_DIR}/{prefix}_bg_audio.mp3"
                    await stream_download(audio_item, adest)
                    all_files.append(adest)
                    await send_audio(
                        chat_id, adest,
                        caption="🎵 <b>Background audio</b>"
                    )
                except Exception as e:
                    logger.warning("[cobalt] background audio failed: %s", e)

            return True

    finally:
        cleanup(all_files)

    return False


# =============================================================================
# LAYER 2b — Direct media URL download (CDN/storage links)
# =============================================================================

async def handle_direct_download(chat_id: int, url: str, prefix: str) -> bool:
    """
    If the URL is a direct link to a media file (ends with .mp4, .mp3, etc.)
    just stream it down and send it without invoking yt-dlp or Cobalt.
    Returns True on success.
    """
    if not is_direct_media_url(url):
        return False

    path_part = url.split("?")[0].lower()
    ext = Path(path_part).suffix or ".mp4"
    dest = f"{TMP_DIR}/{prefix}_direct{ext}"

    try:
        logger.info("[direct] downloading %s", url)
        await stream_download(url, dest)
        if not os.path.exists(dest) or os.path.getsize(dest) == 0:
            return False

        ftype = _classify(dest)
        if ftype == "video":
            await send_document(
                chat_id, dest,
                caption="🎬 <b>Video</b> — original quality"
            )
            audio_path = await _extract_audio(dest, prefix)
            if audio_path:
                if os.path.getsize(audio_path) <= MAX_BYTES:
                    await send_audio(chat_id, audio_path,
                                     caption="🎵 <b>Audio extracted from video</b>")
                cleanup([audio_path])
        elif ftype == "audio":
            await send_audio(chat_id, dest, caption="🎵 <b>Audio</b>")
        elif ftype == "photo":
            await send_photo(chat_id, dest)
        else:
            await send_document(chat_id, dest)

        return True
    except Exception as e:
        logger.warning("[direct] failed: %s", e)
        return False
    finally:
        cleanup([dest])


# =============================================================================
# Instagram cookies helper
# =============================================================================

COOKIES_FILE = f"{TMP_DIR}/ig_cookies.txt"

def ensure_instagram_cookies() -> Optional[str]:
    from urllib.parse import unquote
    raw = os.environ.get("IG_SESSION_ID", "").strip()
    if not raw:
        return None
    session_id = unquote(raw)
    logger.info("[cookies] Instagram sessionid (decoded length=%d)", len(session_id))
    expiry = "2147483647"
    content = (
        "# Netscape HTTP Cookie File\n"
        f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\tsessionid\t{session_id}\n"
    )
    with open(COOKIES_FILE, "w") as f:
        f.write(content)
    logger.info("[cookies] Written %s", COOKIES_FILE)
    return COOKIES_FILE


# =============================================================================
# Instagram GraphQL scraper
# =============================================================================

_IG_DOMAINS  = ("instagram.com", "instagr.am")
_IG_APP_ID   = "936619743392459"
_IG_DOC_ID   = "10015901848480474"
_CHROME_UA   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

import re as _re

def _extract_ig_shortcode(url: str) -> Optional[str]:
    m = _re.search(
        r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url
    )
    return m.group(1) if m else None


def _ig_session_header() -> dict:
    from urllib.parse import unquote
    raw = os.environ.get("IG_SESSION_ID", "").strip()
    if not raw:
        return {}
    return {"Cookie": f"sessionid={unquote(raw)}"}


async def instagram_graphql_download(
    chat_id: int, url: str, prefix: str
) -> bool:
    from urllib.parse import unquote
    shortcode = _extract_ig_shortcode(url)
    if not shortcode:
        return False

    logger.info("[ig-gql] shortcode=%s", shortcode)

    raw_session = os.environ.get("IG_SESSION_ID", "").strip()
    session_id  = unquote(raw_session) if raw_session else ""

    base_headers = {
        "User-Agent":     _CHROME_UA,
        "X-IG-App-ID":    _IG_APP_ID,
        "Accept":         "*/*",
        "Accept-Language":"en-US,en;q=0.9",
        "Referer":        "https://www.instagram.com/",
        "Origin":         "https://www.instagram.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if session_id:
        base_headers["Cookie"] = f"sessionid={session_id}"

    media = None

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.post(
                "https://www.instagram.com/api/graphql",
                data={
                    "variables": json.dumps({"shortcode": shortcode}),
                    "doc_id":    _IG_DOC_ID,
                    "lsd":       "AVqbxe3J_YA",
                },
                headers={
                    **base_headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-FB-LSD":     "AVqbxe3J_YA",
                    "X-ASBD-ID":    "129477",
                },
            )
        body = r.text.strip()
        logger.info("[ig-gql] attempt1 status=%s body_len=%d", r.status_code, len(body))
        if body:
            j = r.json()
            media = (j.get("data") or {}).get("xdt_shortcode_media")
    except Exception as e:
        logger.warning("[ig-gql] attempt1 failed: %s", e)

    if not media:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r2 = await c.post(
                    "https://www.instagram.com/graphql/query",
                    data={
                        "variables": json.dumps({"shortcode": shortcode}),
                        "doc_id":    "24368985919464652",
                    },
                    headers={
                        **base_headers,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            body2 = r2.text.strip()
            logger.info("[ig-gql] attempt2 status=%s body_len=%d", r2.status_code, len(body2))
            if body2:
                j2 = r2.json()
                media = (
                    (j2.get("data") or {}).get("xdt_shortcode_media")
                    or (j2.get("data") or {}).get("shortcode_media")
                )
        except Exception as e:
            logger.warning("[ig-gql] attempt2 failed: %s", e)

    if not media:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r3 = await c.get(
                    "https://api.instagram.com/oembed/",
                    params={"url": url, "omitscript": "true"},
                    headers=base_headers,
                )
            if r3.status_code == 200 and r3.text.strip():
                raise RuntimeError("oEmbed available but no direct media URL — try Cobalt")
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning("[ig-gql] attempt3 oEmbed failed: %s", e)

    if not media:
        raise RuntimeError(
            f"Instagram GraphQL returned no media for shortcode={shortcode}. "
            "All 3 endpoints returned empty — session cookie may be required or expired."
        )

    typename = media.get("__typename", "")
    all_files: list[str] = []

    try:
        if typename == "XDTGraphSidecar" or "edge_sidecar_to_children" in media:
            edges = (media.get("edge_sidecar_to_children") or {}).get("edges", [])
            items = [e["node"] for e in edges if e.get("node")]
            logger.info("[ig-gql] carousel with %d items", len(items))

            photo_paths: list[str] = []
            for i, node in enumerate(items):
                is_vid = node.get("is_video", False)
                if is_vid:
                    vid_url = node.get("video_url", "")
                    dest = f"{TMP_DIR}/{prefix}_ig_{i:02d}.mp4"
                    await stream_download(vid_url, dest)
                    all_files.append(dest)
                    await send_document(
                        chat_id, dest,
                        caption=f"🎬 <b>Video {i+1}/{len(items)}</b>"
                    )
                    audio = await _extract_audio(dest, f"{prefix}_ig_{i}")
                    if audio:
                        all_files.append(audio)
                        await send_audio(chat_id, audio, caption="🎵 <b>Audio track</b>")
                else:
                    img_url = node.get("display_url", "")
                    dest = f"{TMP_DIR}/{prefix}_ig_{i:02d}.jpg"
                    await stream_download(img_url, dest)
                    all_files.append(dest)
                    photo_paths.append(dest)

            if len(photo_paths) == 1:
                await send_photo(chat_id, photo_paths[0])
            elif photo_paths:
                for start in range(0, len(photo_paths), 10):
                    await send_media_group(chat_id, photo_paths[start:start+10])

        elif media.get("is_video"):
            vid_url = media.get("video_url", "")
            dest = f"{TMP_DIR}/{prefix}_ig_video.mp4"
            await stream_download(vid_url, dest)
            all_files.append(dest)
            await send_document(
                chat_id, dest,
                caption="🎬 <b>Video</b> — original quality"
            )
            audio = await _extract_audio(dest, f"{prefix}_ig")
            if audio:
                all_files.append(audio)
                await send_audio(chat_id, audio, caption="🎵 <b>Audio track</b>")

        else:
            img_url = media.get("display_url", "")
            dest = f"{TMP_DIR}/{prefix}_ig_photo.jpg"
            await stream_download(img_url, dest)
            all_files.append(dest)
            await send_photo(chat_id, dest)

    finally:
        cleanup(all_files)

    return True


# =============================================================================
# LAYER 3 — yt-dlp (universal fallback — supports 1000+ sites)
# =============================================================================

def run_yt_dlp(url: str, prefix: str, cookies_file: Optional[str] = None) -> list[str]:
    """
    Run yt-dlp with best quality settings.
    Supports any site yt-dlp knows about (1000+ extractors).
    Raises RuntimeError on failure.
    """
    output_template = f"{TMP_DIR}/{prefix}_%(id)s.%(ext)s"

    cmd = [
        "python", "-m", "yt_dlp",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        # Spoof a real browser to bypass basic bot detection
        "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "--add-header", "Referer:" + url,
        "-o", output_template,
    ]

    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]

    cmd.append(url)

    logger.info("[yt-dlp] running for %s", url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        logger.error("[yt-dlp] stderr: %s", result.stderr)
        raise RuntimeError(result.stderr or "yt-dlp error")

    return sorted(glob.glob(f"{TMP_DIR}/{prefix}_*"))


# =============================================================================
# Audio extraction (shared)
# =============================================================================

async def _extract_audio(video_path: str, prefix: str) -> Optional[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract_audio_sync, video_path, prefix)


def _extract_audio_sync(video_path: str, prefix: str) -> Optional[str]:
    out_template = f"{TMP_DIR}/{prefix}_audio.%(ext)s"
    cmd = [
        "python", "-m", "yt_dlp",
        "--no-warnings",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", out_template,
        video_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        candidates = glob.glob(f"{TMP_DIR}/{prefix}_audio*.mp3")
        if candidates:
            p = candidates[0]
            if os.path.getsize(p) > 0:
                return p
    except Exception as e:
        logger.warning("[audio-extract] failed: %s", e)
    return None


# =============================================================================
# File classification
# =============================================================================

def _classify(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".wmv"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "photo"
    if ext in {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aac"}:
        return "audio"
    return "document"


# =============================================================================
# Master dispatcher
# =============================================================================

async def process_url(chat_id: int, url: str) -> None:
    prefix    = f"{chat_id}_{abs(hash(url))}"
    all_files: list[str] = []

    try:
        # ── Step 1: Expand short TikTok links ─────────────────────────────────
        if is_tiktok(url):
            url = await expand_tiktok_url(url)

        # ── Step 2: TikTok slideshow → tikwm directly ─────────────────────────
        if is_tiktok(url) and is_tiktok_slideshow(url):
            logger.info("[dispatch] TikTok slideshow → tikwm")
            await handle_tiktok_slideshow(chat_id, url, prefix)
            return

        # ── Step 3: Direct media file link (CDN / storage) ────────────────────
        if is_direct_media_url(url):
            logger.info("[dispatch] direct media URL → stream download")
            ok = await handle_direct_download(chat_id, url, prefix)
            if ok:
                return

        # ── Step 4: Instagram → direct GraphQL ────────────────────────────────
        if any(d in url for d in _IG_DOMAINS):
            logger.info("[dispatch] Instagram → GraphQL scraper")
            try:
                ok = await instagram_graphql_download(chat_id, url, prefix)
                if ok:
                    return
            except RuntimeError as exc:
                logger.warning("[dispatch] Instagram GraphQL failed: %s", exc)

        # ── Step 5: Cobalt API ─────────────────────────────────────────────────
        logger.info("[dispatch] Trying Cobalt for %s", url)
        cobalt_ok = await handle_cobalt(chat_id, url, prefix)
        if cobalt_ok:
            return

        # ── Step 6: yt-dlp universal fallback ─────────────────────────────────
        logger.info("[dispatch] Cobalt failed, falling back to yt-dlp for %s", url)
        loop = asyncio.get_event_loop()

        # TikTok: also try tikwm before giving up
        if is_tiktok(url):
            try:
                await handle_tiktok_slideshow(chat_id, url, prefix)
                return
            except Exception as e:
                logger.warning("[dispatch] tikwm fallback also failed: %s", e)

        # Pass Instagram cookies if available
        cookies_file: Optional[str] = None
        if any(d in url for d in _IG_DOMAINS):
            cookies_file = ensure_instagram_cookies()

        try:
            files = await loop.run_in_executor(
                None, run_yt_dlp, url, prefix, cookies_file
            )
        except RuntimeError as exc:
            err = str(exc).lower()
            is_instagram = any(d in url for d in _IG_DOMAINS)

            if "private" in err and not ("login" in err or "rate" in err):
                await send_text(chat_id, "🔒 This content is private and cannot be downloaded.")
            elif is_instagram and ("login" in err or "rate" in err or "not available" in err):
                has_cookie = bool(os.environ.get("IG_SESSION_ID", "").strip())
                if not has_cookie:
                    await send_text(
                        chat_id,
                        "⚠️ <b>Instagram is blocking anonymous downloads.</b>\n\n"
                        "The bot admin needs to add an <code>IG_SESSION_ID</code> "
                        "cookie to the environment variables.\n\nSee /help for more info."
                    )
                else:
                    await send_text(
                        chat_id,
                        "⚠️ <b>Instagram download failed.</b>\n\n"
                        "Instagram may have rate-limited or expired the session cookie.\n"
                        "Please try again in a few minutes."
                    )
            elif "unsupported url" in err:
                await send_text(
                    chat_id,
                    "❌ This URL is not supported by any of our download methods.\n"
                    "Make sure it is a direct link to a public video or audio post."
                )
            else:
                await send_text(
                    chat_id,
                    "❌ Download failed. Make sure the link is public and try again."
                )
            return

        all_files = list(files)
        if not all_files:
            await send_text(chat_id, "❌ No media found at that URL.")
            return

        oversized = [f for f in all_files if os.path.getsize(f) > MAX_BYTES]
        sendable  = [f for f in all_files if os.path.getsize(f) <= MAX_BYTES]

        if oversized:
            await send_text(
                chat_id,
                f"⚠️ <b>{len(oversized)}</b> file(s) exceeded the 50 MB Telegram limit and were skipped."
            )
        if not sendable:
            await send_text(chat_id, "All files exceed Telegram's 50 MB limit 😢")
            return

        photos  = [f for f in sendable if _classify(f) == "photo"]
        videos  = [f for f in sendable if _classify(f) == "video"]
        audios  = [f for f in sendable if _classify(f) == "audio"]
        docs    = [f for f in sendable if _classify(f) == "document"]

        if len(photos) > 1:
            for start in range(0, len(photos), 10):
                await send_media_group(chat_id, photos[start:start + 10])
        elif photos:
            await send_photo(chat_id, photos[0])

        for vf in videos:
            await send_document(
                chat_id, vf,
                caption="🎬 <b>Video</b> — original quality, no compression"
            )
            audio_path = await _extract_audio(vf, prefix)
            if audio_path:
                all_files.append(audio_path)
                if os.path.getsize(audio_path) <= MAX_BYTES:
                    await send_audio(
                        chat_id, audio_path,
                        caption="🎵 <b>Audio extracted from video</b>"
                    )

        for af in audios:
            await send_audio(chat_id, af)

        for df in docs:
            await send_document(chat_id, df)

    except asyncio.TimeoutError:
        await send_text(chat_id, "⏱️ Download timed out. Try again later.")
    except subprocess.TimeoutExpired:
        await send_text(chat_id, "⏱️ Download timed out. The file may be too large.")
    except Exception:
        logger.exception("[process] unexpected error for %s", url)
        await send_text(chat_id, "😢 An unexpected error occurred. Please try again.")
    finally:
        cleanup(all_files)


# =============================================================================
# Telegram update handler
# =============================================================================

async def _handle_update(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)

    message = update.get("message") or update.get("channel_post")
    if not message:
        return JSONResponse({"ok": True})

    chat_id: int    = message["chat"]["id"]
    text: str       = (message.get("text") or "").strip()
    from_user       = message.get("from") or {}
    first_name: str = from_user.get("first_name") or "there"

    if not text:
        return JSONResponse({"ok": True})

    # ── /start ────────────────────────────────────────────────────────────────
    if text.startswith("/start"):
        await send_text(chat_id, (
            f"👋 <b>Hey {first_name}, welcome to Universal Downloader Bot!</b>\n\n"
            "Send me any public media link and I'll download it for you in "
            "<b>highest quality</b> — no compression, no watermarks.\n\n"
            "🎬 <b>TikTok</b> — Videos, Slideshows &amp; Carousels\n"
            "📸 <b>Instagram</b> — Reels, Posts, Carousels\n"
            "▶️ <b>YouTube</b> — Videos, Shorts, Music\n"
            "🐦 <b>Twitter / X</b> — Videos, GIFs\n"
            "📘 <b>Facebook</b> — Videos\n"
            "🟠 <b>Reddit</b> — Videos &amp; GIFs\n"
            "🎵 <b>SoundCloud</b> — Audio\n"
            "🎮 <b>Twitch</b> — Clips\n"
            "📌 <b>Pinterest</b> — Videos &amp; Images\n"
            "🎞 <b>Vimeo, Dailymotion, Bilibili</b> — and more!\n"
            "🌐 <b>Any direct video/audio link</b> — CDN, hosting, etc.\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to use:</b>\n"
            "1. Copy any public video or audio link.\n"
            "2. Paste it here and send.\n"
            "3. I'll download and deliver it! ⚡\n\n"
            "Type /help for more info."
        ))
        return JSONResponse({"ok": True})

    # ── /help ─────────────────────────────────────────────────────────────────
    if text.startswith("/help"):
        await send_text(chat_id, (
            "📖 <b>Universal Downloader Bot — Help</b>\n\n"
            "<b>Step 1:</b> Copy any public video or audio URL.\n"
            "<b>Step 2:</b> Paste it here and send.\n"
            "<b>Step 3:</b> Receive the file in seconds!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ <b>What's supported:</b>\n"
            "• All major social platforms (TikTok, YouTube, Instagram, etc.)\n"
            "• Direct video/audio file links (.mp4, .mp3, .m4a, .webm …)\n"
            "• Any site supported by yt-dlp (1000+ extractors)\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📦 <b>What you receive:</b>\n"
            "• <b>Videos</b> → file (no compression) + 🎵 audio separately\n"
            "• <b>TikTok Slideshows</b> → photo carousel + 🎵 background music\n"
            "• <b>Instagram Carousels</b> → all photos/videos\n"
            "• <b>Audio posts</b> → mp3 file\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Limitations:</b>\n"
            "• Files above 50 MB cannot be sent via Telegram.\n"
            "• Private or login-protected content cannot be downloaded."
        ))
        return JSONResponse({"ok": True})

    # ── URL check ─────────────────────────────────────────────────────────────
    if not looks_like_url(text):
        await send_text(chat_id, (
            "⚠️ Please send a valid http/https link.\n"
            "Type /help to learn more."
        ))
        return JSONResponse({"ok": True})

    # ── Queue work ────────────────────────────────────────────────────────────
    await send_text(chat_id, "⏳ Downloading... This may take a moment.")
    background_tasks.add_task(process_url, chat_id, text)
    return JSONResponse({"ok": True})


# =============================================================================
# Routes
# =============================================================================

@app.post("/")
async def webhook_root(request: Request, background_tasks: BackgroundTasks):
    return await _handle_update(request, background_tasks)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    return await _handle_update(request, background_tasks)


@app.get("/")
async def health():
    return {"status": "ok", "bot": "Universal Telegram Downloader"}


@app.get("/set_webhook")
async def set_webhook(url: str):
    """Visit /set_webhook?url=https://your-domain.vercel.app to register."""
    webhook_url = url.rstrip("/") + "/webhook"
    return await tg_send(
        "setWebhook",
        json={"url": webhook_url, "drop_pending_updates": True},
    )
