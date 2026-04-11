"""
Universal Telegram Downloader Bot
──────────────────────────────────
Download pipeline (in priority order):

  1. TikTok slideshow  → tikwm.com API  (images + music)
  2. Everything else   → Cobalt API     (Instagram, Twitter/X, YouTube,
                                         Facebook, Reddit, TikTok videos,
                                         Twitch, Pinterest, SoundCloud …)
  3. Cobalt fallback   → yt-dlp         (any URL Cobalt can't handle)
     • Instagram URLs: injects IG_SESSION_ID cookie automatically
     • All other URLs: standard yt-dlp best quality

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
# These are well-known public Cobalt instances with open CORS & no auth.
# Add your own self-hosted instance first for best reliability.
COBALT_INSTANCES = [
    os.environ.get("COBALT_API_URL", ""),   # self-hosted (optional env var)
    "https://cobalt.api.lostnode.net",
    "https://api.cobalt.tools",             # official – may rate-limit bots
    "https://cobalt.lunar.icu",
    "https://cobalt.cae.re",
]
COBALT_INSTANCES = [u for u in COBALT_INSTANCES if u]  # drop empty

TIKWM_API = "https://www.tikwm.com/api/"

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

    # Collect image URLs
    images: list[str] = data.get("images") or []
    if not images:
        for img in (data.get("image_post_info") or {}).get("images", []):
            ul = img.get("display_image", {}).get("url_list") or []
            if ul:
                images.append(ul[0])

    if not images:
        raise RuntimeError("tikwm: no images found in slideshow")

    # Collect music URL
    music_url: str = (
        (data.get("music_info") or {}).get("play")
        or data.get("music") or ""
    )

    # Caption
    author  = (data.get("author") or {}).get("nickname", "")
    desc    = (data.get("title") or data.get("desc") or "")[:80]
    caption = f"📸 <b>{author}</b>" + (f"\n{desc}" if desc else "")

    # Download images concurrently
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

    # Download music
    music_path: Optional[str] = None
    if music_url:
        try:
            mp = f"{TMP_DIR}/{prefix}_music.mp3"
            await stream_download(music_url, mp)
            if os.path.exists(mp) and os.path.getsize(mp) > 0:
                music_path = mp
        except Exception as e:
            logger.warning("[tikwm] music failed: %s", e)

    # Send images
    if len(image_paths) == 1:
        await send_photo(chat_id, image_paths[0], caption=caption)
    else:
        for start in range(0, len(image_paths), 10):
            await send_media_group(
                chat_id, image_paths[start:start + 10],
                caption=caption if start == 0 else "",
            )

    # Send music
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
    """
    Try each Cobalt instance in order.
    Returns the parsed JSON response or None if all instances fail.
    Response statuses:
      tunnel / redirect → single file download URL
      picker            → list of items (carousel / multi-track)
      error             → failed
    """
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
                # error status — try next instance
                logger.warning("[cobalt] %s error: %s", instance, data.get("error"))
            else:
                logger.warning("[cobalt] %s HTTP %s", instance, r.status_code)
        except Exception as e:
            logger.warning("[cobalt] %s failed: %s", instance, e)

    return None


async def handle_cobalt(chat_id: int, url: str, prefix: str) -> bool:
    """
    Download via Cobalt API. Returns True on success, False if Cobalt couldn't handle it.

    Delivery rules:
      • Single video  → sendDocument (no compression) + extracted audio
      • Picker items  → each video as document + audio; each image as photo
      • Audio only    → sendAudio
    """
    result = await cobalt_query(url)
    if not result:
        return False

    status    = result.get("status", "")
    all_files: list[str] = []

    try:
        # ── Single file ───────────────────────────────────────────────────────
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

        # ── Picker (carousel / multi-item) ────────────────────────────────────
        if status == "picker":
            items: list[dict] = result.get("picker", [])
            audio_item        = result.get("audio")  # background audio URL

            photos: list[str] = []

            for i, item in enumerate(items):
                item_url  = item.get("url", "")
                item_type = item.get("type", "photo")   # "photo" | "video"
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
                    # video item — send as document + extract audio
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

            # Send photo batch as media group
            if len(photos) == 1:
                await send_photo(chat_id, photos[0])
            elif photos:
                for start in range(0, len(photos), 10):
                    await send_media_group(chat_id, photos[start:start + 10])

            # Send background audio (e.g. TikTok slideshow music from Cobalt)
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
# Instagram cookies helper
# =============================================================================

# Instagram now blocks all anonymous requests (2025).
# Set IG_SESSION_ID in your Vercel env vars to a real Instagram sessionid cookie.
# How to get it: open instagram.com in browser → DevTools → Application →
# Cookies → copy the value of "sessionid"
#
# The value may be URL-encoded (contains %3A etc.) — we decode it automatically.

COOKIES_FILE = f"{TMP_DIR}/ig_cookies.txt"

def ensure_instagram_cookies() -> Optional[str]:
    """
    Write a Netscape-format cookies.txt file from IG_SESSION_ID env var.
    URL-decodes the value automatically (handles %3A → : etc.)
    Returns the path to the cookies file, or None if env var not set.
    """
    from urllib.parse import unquote

    raw = os.environ.get("IG_SESSION_ID", "").strip()
    if not raw:
        return None

    # Decode URL-encoding — e.g. 56481615479%3Azm18... → 56481615479:zm18...
    session_id = unquote(raw)
    logger.info("[cookies] Instagram sessionid (decoded length=%d)", len(session_id))

    # Always re-write — /tmp is ephemeral on Vercel between cold starts
    # STRICT Netscape format rules:
    #   - File MUST start with "# Netscape HTTP Cookie File"
    #   - Domain MUST start with a dot (.) for domain_specified == initial_dot
    #   - www.instagram.com (no dot) causes AssertionError in Python's cookiejar
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
# yt-dlp cannot handle Instagram image/carousel posts — it only finds video
# streams and crashes on photo posts. We use Instagram's internal GraphQL
# API directly instead, which works for all post types.

_IG_DOMAINS  = ("instagram.com", "instagr.am")
_IG_APP_ID   = "936619743392459"   # stable desktop web app ID
_IG_DOC_ID   = "10015901848480474" # shortcode media query (2025)
_CHROME_UA   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

import re as _re

def _extract_ig_shortcode(url: str) -> Optional[str]:
    """Extract shortcode from any Instagram post/reel/tv URL."""
    m = _re.search(
        r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url
    )
    return m.group(1) if m else None


def _ig_session_header() -> dict:
    """Return Cookie header with decoded session ID, or empty dict."""
    from urllib.parse import unquote
    raw = os.environ.get("IG_SESSION_ID", "").strip()
    if not raw:
        return {}
    return {"Cookie": f"sessionid={unquote(raw)}"}


async def instagram_graphql_download(
    chat_id: int, url: str, prefix: str
) -> bool:
    """
    Download any public Instagram post via GraphQL API.
    Handles: single image, single video, carousel (mixed images+videos).
    Returns True on success, False if shortcode can't be extracted.
    Raises RuntimeError on API / download failure.
    """
    shortcode = _extract_ig_shortcode(url)
    if not shortcode:
        return False

    logger.info("[ig-gql] shortcode=%s", shortcode)

    headers = {
        "User-Agent":        _CHROME_UA,
        "X-IG-App-ID":       _IG_APP_ID,
        "X-FB-LSD":          "AVqbxe3J_YA",
        "X-ASBD-ID":         "129477",
        "Content-Type":      "application/x-www-form-urlencoded",
        "Accept":            "*/*",
        "Referer":           "https://www.instagram.com/",
        "Sec-Fetch-Site":    "same-origin",
        "Sec-Fetch-Mode":    "cors",
        "Origin":            "https://www.instagram.com",
        **_ig_session_header(),
    }

    payload = (
        f"variables=%7B%22shortcode%22%3A%22{shortcode}%22%7D"
        f"&doc_id={_IG_DOC_ID}"
        f"&lsd=AVqbxe3J_YA"
    )

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.post(
            "https://www.instagram.com/api/graphql",
            content=payload.encode(),
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()

    media = (data.get("data") or {}).get("xdt_shortcode_media")
    if not media:
        # Fallback: try older graphql/query endpoint
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r2 = await c.post(
                "https://www.instagram.com/graphql/query",
                content=(
                    f"variables=%7B%22shortcode%22%3A%22{shortcode}%22%7D"
                    f"&doc_id=24368985919464652"
                ).encode(),
                headers=headers,
            )
            r2.raise_for_status()
            data2 = r2.json()
        media = (
            (data2.get("data") or {}).get("xdt_shortcode_media")
            or (data2.get("data") or {}).get("shortcode_media")
        )

    if not media:
        raise RuntimeError("Instagram GraphQL returned no media data")

    typename = media.get("__typename", "")
    all_files: list[str] = []

    try:
        # ── Carousel (mixed images + videos) ──────────────────────────────────
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

            # Send all photos as media group
            if len(photo_paths) == 1:
                await send_photo(chat_id, photo_paths[0])
            elif photo_paths:
                for start in range(0, len(photo_paths), 10):
                    await send_media_group(chat_id, photo_paths[start:start+10])

        # ── Single video (Reel / video post) ──────────────────────────────────
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

        # ── Single image ──────────────────────────────────────────────────────
        else:
            img_url = media.get("display_url", "")
            dest = f"{TMP_DIR}/{prefix}_ig_photo.jpg"
            await stream_download(img_url, dest)
            all_files.append(dest)
            await send_photo(chat_id, dest)

    finally:
        cleanup(all_files)

    return True

def run_yt_dlp(url: str, prefix: str) -> list[str]:
    """
    Run yt-dlp with smart per-platform options.
    - Everything else: bestvideo+bestaudio/best
    Raises RuntimeError on failure.
    """
    output_template = f"{TMP_DIR}/{prefix}_%(id)s.%(ext)s"

    # All platforms (Instagram is handled by GraphQL scraper above, not here)
    cmd = [
        "python", "-m", "yt_dlp",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "-o", output_template,
        url,
    ]

    logger.info("[yt-dlp] running for %s", url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)

    if result.returncode != 0:
        logger.error("[yt-dlp] stderr: %s", result.stderr)
        raise RuntimeError(result.stderr or "yt-dlp error")

    return sorted(glob.glob(f"{TMP_DIR}/{prefix}_*"))


# =============================================================================
# Audio extraction (shared)
# =============================================================================

async def _extract_audio(video_path: str, prefix: str) -> Optional[str]:
    """
    Extract audio track from a video file as 320 kbps mp3.
    Runs in a thread executor so it doesn't block the event loop.
    Returns the mp3 path on success, None on failure.
    """
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
        # yt-dlp may append ext — find actual output
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
    if ext in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "photo"
    if ext in {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav"}:
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

        # ── Step 3: Instagram → direct GraphQL (handles all post types) ─────────
        if any(d in url for d in _IG_DOMAINS):
            logger.info("[dispatch] Instagram → GraphQL scraper")
            try:
                ok = await instagram_graphql_download(chat_id, url, prefix)
                if ok:
                    return
                # shortcode not found — fall through to Cobalt/yt-dlp
            except RuntimeError as exc:
                logger.warning("[dispatch] Instagram GraphQL failed: %s", exc)
                # Fall through to Cobalt then yt-dlp

        # ── Step 4: Cobalt API ─────────────────────────────────────────────────
        logger.info("[dispatch] Trying Cobalt for %s", url)
        cobalt_ok = await handle_cobalt(chat_id, url, prefix)
        if cobalt_ok:
            return

        # ── Step 4: yt-dlp fallback ────────────────────────────────────────────
        logger.info("[dispatch] Cobalt failed, falling back to yt-dlp for %s", url)
        loop = asyncio.get_event_loop()

        # TikTok: also try tikwm before yt-dlp gives up
        if is_tiktok(url):
            try:
                await handle_tiktok_slideshow(chat_id, url, prefix)
                return
            except Exception as e:
                logger.warning("[dispatch] tikwm fallback also failed: %s", e)

        try:
            files = await loop.run_in_executor(None, run_yt_dlp, url, prefix)
        except RuntimeError as exc:
            err = str(exc).lower()
            is_instagram = any(d in url for d in _IG_DOMAINS)

            if "private" in err and not ("login" in err or "rate" in err):
                # Genuinely private post
                await send_text(chat_id, "🔒 This content is private and cannot be downloaded.")
            elif is_instagram and ("login" in err or "rate" in err or "not available" in err):
                # Instagram blocked us — guide user on the cookie fix
                has_cookie = bool(os.environ.get("IG_SESSION_ID", "").strip())
                if not has_cookie:
                    await send_text(
                        chat_id,
                        "⚠️ <b>Instagram is blocking anonymous downloads.</b>\n\n"
                        "This is a known Instagram restriction in 2025.\n"
                        "The bot admin needs to add an <code>IG_SESSION_ID</code> "
                        "cookie to the Vercel environment variables.\n\n"
                        "See /help for more info."
                    )
                else:
                    await send_text(
                        chat_id,
                        "⚠️ <b>Instagram download failed.</b>\n\n"
                        "Instagram may have rate-limited or expired the session cookie.\n"
                        "Please try again in a few minutes, or the admin may need to "
                        "refresh the <code>IG_SESSION_ID</code> cookie."
                    )
            elif "unsupported url" in err:
                await send_text(
                    chat_id,
                    "❌ This URL is not supported.\n"
                    "Make sure it's a direct link to a public post or video."
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

        # Size filter
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

        # Photos
        if len(photos) > 1:
            for start in range(0, len(photos), 10):
                await send_media_group(chat_id, photos[start:start + 10])
        elif photos:
            await send_photo(chat_id, photos[0])

        # Videos + audio extraction
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
# Supported domains (for URL validation)
# =============================================================================

SUPPORTED_DOMAINS = (
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "instagr.am",
    "youtube.com", "youtu.be",
    "twitter.com", "x.com", "t.co",
    "facebook.com", "fb.watch",
    "reddit.com", "redd.it",
    "twitch.tv",
    "vimeo.com",
    "pinterest.com",
    "snapchat.com",
    "soundcloud.com",
    "bilibili.com", "b23.tv",
    "dailymotion.com",
    "streamable.com",
    "tumblr.com",
    "bluesky.app", "bsky.app",
)


def looks_like_url(text: str) -> bool:
    t = text.strip()
    return t.startswith(("http://", "https://")) and any(d in t for d in SUPPORTED_DOMAINS)


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
            "🎞 <b>Vimeo, Dailymotion, Bilibili</b> — and more!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to use:</b>\n"
            "1. Copy a link from any supported platform.\n"
            "2. Paste it here and send.\n"
            "3. I'll download and deliver it! ⚡\n\n"
            "Type /help for more info."
        ))
        return JSONResponse({"ok": True})

    # ── /help ─────────────────────────────────────────────────────────────────
    if text.startswith("/help"):
        await send_text(chat_id, (
            "📖 <b>Universal Downloader Bot — Help</b>\n\n"
            "<b>Step 1:</b> Copy a media URL from any supported platform.\n"
            "<b>Step 2:</b> Paste it here and send.\n"
            "<b>Step 3:</b> Receive the file in seconds!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ <b>Supported platforms:</b>\n"
            "TikTok · Instagram · YouTube · Twitter/X\n"
            "Facebook · Reddit · SoundCloud · Twitch\n"
            "Pinterest · Vimeo · Dailymotion · Bilibili\n"
            "Streamable · Tumblr · Bluesky · and more!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📦 <b>What you receive:</b>\n"
            "• <b>Videos</b> → file (no compression) + 🎵 audio separately\n"
            "• <b>TikTok Slideshows</b> → photo carousel + 🎵 background music\n"
            "• <b>Instagram Carousels</b> → all photos/videos\n"
            "• <b>Audio posts</b> → mp3 file\n"
            "• <b>Photos</b> → image or carousel\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Limitations:</b>\n"
            "• Files above 50 MB cannot be sent via Telegram.\n"
            "• Private or login-protected content cannot be downloaded."
        ))
        return JSONResponse({"ok": True})

    # ── URL check ─────────────────────────────────────────────────────────────
    if not looks_like_url(text):
        await send_text(chat_id, (
            "⚠️ Please send a valid URL from a supported platform.\n"
            "Type /help to see the full list."
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
