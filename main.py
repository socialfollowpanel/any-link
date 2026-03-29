import os
import re
import glob
import json
import time
import asyncio
import subprocess
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
TG_API     = f"https://api.telegram.org/bot{BOT_TOKEN}"
TMP_DIR    = "/tmp"
MAX_BYTES  = 50 * 1024 * 1024          # 50 MB – Telegram Bot API hard cap
TIKWM_API  = "https://www.tikwm.com/api/"

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan)


# =============================================================================
# Telegram helpers
# =============================================================================

async def tg_send(method: str, **kwargs) -> dict:
    """Generic async Telegram API call."""
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(f"{TG_API}/{method}", **kwargs)
        r.raise_for_status()
        return r.json()


async def send_text(chat_id: int, text: str, parse_mode: str = "HTML") -> dict:
    """Send text and return the response (useful for getting message_id)."""
    return await tg_send("sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": parse_mode
    })


async def edit_message_text(chat_id: int, message_id: int, text: str, parse_mode: str = "HTML") -> None:
    """Edit an existing message to create a loading 'animation'."""
    try:
        await tg_send("editMessageText", json={
            "chat_id": chat_id, "message_id": message_id, 
            "text": text, "parse_mode": parse_mode
        })
    except Exception as e:
        logger.warning(f"[tg_edit] Failed to edit message: {e}")


async def delete_message(chat_id: int, message_id: int) -> None:
    """Delete the loading message once done."""
    try:
        await tg_send("deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


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
    for i, _ in enumerate(batch):
        item: dict = {"type": "photo", "media": f"attach://photo{i}"}
        if i == 0 and caption:
            item["caption"]    = caption
            item["parse_mode"] = "HTML"
        media.append(item)

    files: dict = {}
    for i, path in enumerate(batch):
        files[f"photo{i}"] = open(path, "rb")
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
# URL Detection & Expansion
# =============================================================================

TIKTOK_SHORT_DOMAINS = ("vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com")

def is_tiktok_url(url: str) -> bool:
    return any(d in url for d in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"))

def is_tiktok_slideshow(url: str) -> bool:
    return "/photo/" in url

def is_instagram_url(url: str) -> bool:
    return any(d in url for d in ("instagram.com", "instagr.am"))

def is_facebook_url(url: str) -> bool:
    return any(d in url for d in ("facebook.com", "fb.watch", "fb.com"))

def is_youtube_url(url: str) -> bool:
    return any(d in url for d in ("youtube.com", "youtu.be"))

async def expand_tiktok_url(url: str) -> str:
    if not any(d in url for d in TIKTOK_SHORT_DOMAINS):
        return url
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            r = await client.head(url)
            expanded = str(r.url)
            logger.info("[expand] %s -> %s", url, expanded)
            return expanded
    except Exception as exc:
        logger.warning("[expand] Failed for %s: %s", url, exc)
        return url


# =============================================================================
# Downloader Core & Fallback APIs
# =============================================================================

async def download_url_to_file(url: str, dest: str, chat_id: int = None, msg_id: int = None) -> str:
    """Stream-download a single URL to dest with animated progress bar."""
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            
            total_length = r.headers.get('Content-Length')
            total = int(total_length) if total_length else None
            downloaded = 0
            last_update = time.time()
            
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    if total and chat_id and msg_id:
                        now = time.time()
                        # Update animation every 2 seconds to avoid Telegram rate limits
                        if now - last_update > 2.0:
                            pct = (downloaded / total) * 100
                            filled = int(pct / 10)
                            bar = "█" * filled + "▒" * (10 - filled)
                            asyncio.create_task(edit_message_text(
                                chat_id, msg_id, 
                                f"📥 <b>Downloading...</b>\n\n[{bar}] {pct:.1f}%"
                            ))
                            last_update = now
    return dest


# ── TikTok Fallback API (tikwm) ──
async def tikwm_fetch(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            TIKWM_API,
            data={"url": url, "hd": 1},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        body = r.json()

    if body.get("code") != 0:
        raise RuntimeError(f"tikwm error: {body.get('msg', 'unknown')}")

    return body["data"]

async def handle_tiktok_fallback(chat_id: int, msg_id: int, url: str, prefix: str) -> list[str]:
    """Handles both TikTok videos and slideshows via tikwm."""
    logger.info("[fallback] tikwm fetch for %s", url)
    data = await tikwm_fetch(url)
    
    # Check if it's a slideshow
    images_raw: list[str] = data.get("images") or []
    if not images_raw:
        image_post_info = data.get("image_post_info") or {}
        for img in image_post_info.get("images", []):
            url_list = img.get("display_image", {}).get("url_list") or []
            if url_list:
                images_raw.append(url_list[0])

    if images_raw:
        # It's a slideshow
        image_paths: list[str] = []
        dl_tasks = [
            download_url_to_file(img_url, f"{TMP_DIR}/{prefix}_slide_{i:02d}.jpg")
            for i, img_url in enumerate(images_raw)
        ]
        results = await asyncio.gather(*dl_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            p = f"{TMP_DIR}/{prefix}_slide_{i:02d}.jpg"
            if not isinstance(res, Exception) and os.path.exists(p) and os.path.getsize(p) > 0:
                image_paths.append(p)
                
        # Get music
        music_url = data.get("music_info", {}).get("play") or data.get("music") or ""
        if music_url:
            mp = f"{TMP_DIR}/{prefix}_music.mp3"
            try:
                await download_url_to_file(music_url, mp)
                if os.path.exists(mp): image_paths.append(mp)
            except Exception:
                pass
        return image_paths
    else:
        # It's a standard video
        play_url = data.get("play") or data.get("wmplay")
        if not play_url:
            raise RuntimeError("tikwm returned no video URL")
        dest = f"{TMP_DIR}/{prefix}_tiktok.mp4"
        await download_url_to_file(play_url, dest, chat_id, msg_id)
        return [dest]


# ── Universal Fallback API (Cobalt) for IG, FB, YT ──
async def universal_platform_fallback(url: str, prefix: str, chat_id: int = None, msg_id: int = None) -> list[str]:
    """
    Uses the Cobalt API as a fallback for Instagram (Reels/Posts/Carousels), 
    Facebook, and YouTube to guarantee highest quality delivery.
    """
    logger.info("[fallback] universal API fetch for %s", url)
    api_url = "https://api.cobalt.tools/api/json"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    payload = {
        "url": url,
        "vQuality": "max",
        "filenamePattern": "basic"
    }
    
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(api_url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()

    if data.get("status") == "error":
        raise RuntimeError(data.get("text", "Fallback API returned an error"))

    paths = []
    
    # Handle Carousels / Multiple items (Instagram posts with multiple slides)
    if data.get("status") == "picker":
        items = data.get("picker", [])
        dl_tasks = []
        for i, item in enumerate(items):
            item_url = item.get("url")
            if item_url:
                ext = "mp4" if item.get("type") == "video" else "jpg"
                dest = f"{TMP_DIR}/{prefix}_fallback_{i:02d}.{ext}"
                dl_tasks.append(download_url_to_file(item_url, dest))
        
        results = await asyncio.gather(*dl_tasks, return_exceptions=True)
        for res in results:
            if not isinstance(res, Exception):
                paths.append(res)
    else:
        # Single Video/Audio/Photo
        url_to_download = data.get("url")
        if not url_to_download:
            raise RuntimeError("No media URL returned from fallback")
        dest = f"{TMP_DIR}/{prefix}_fallback.mp4"
        await download_url_to_file(url_to_download, dest, chat_id, msg_id)
        paths.append(dest)

    return paths


# =============================================================================
# yt-dlp helpers
# =============================================================================

def build_yt_dlp_cmd(url: str, output_template: str) -> list[str]:
    cmd = [
        "python", "-m", "yt_dlp",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
    ]
    # Always pass cookies if the file exists (crucial for Instagram & age-restricted YT)
    if os.path.exists("cookies.txt"):
        cmd.extend(["--cookies", "cookies.txt"])
        
    cmd.extend(["-o", output_template, url])
    return cmd


async def run_yt_dlp_async(url: str, prefix: str, chat_id: int, msg_id: int) -> list[str]:
    """Download via yt-dlp asynchronously and update Telegram progress bar."""
    output_template = f"{TMP_DIR}/{prefix}_%(id)s.%(ext)s"
    cmd = build_yt_dlp_cmd(url, output_template)
    cmd.append("--newline")  # Force newlines so we can read progress line-by-line
    
    logger.info("[yt-dlp async] %s", " ".join(cmd))

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    last_update = time.time()
    
    # Read output line-by-line to animate progress
    while True:
        line = await process.stdout.readline()
        if not line:
            break
            
        line_str = line.decode('utf-8', errors='ignore')
        
        # Look for percentages like " 45.0% "
        if "[download]" in line_str and "%" in line_str:
            match = re.search(r'(\d+\.\d+)%', line_str)
            if match:
                pct = float(match.group(1))
                now = time.time()
                
                # Update animation every 2 seconds
                if now - last_update > 2.0:
                    filled = int(pct / 10)
                    bar = "█" * filled + "▒" * (10 - filled)
                    asyncio.create_task(edit_message_text(
                        chat_id, msg_id, 
                        f"📥 <b>Downloading...</b>\n\n[{bar}] {pct:.1f}%"
                    ))
                    last_update = now

    await process.wait()

    if process.returncode != 0:
        stderr_bytes = await process.stderr.read()
        stderr_text = stderr_bytes.decode('utf-8', errors='ignore')
        logger.error("[yt-dlp] stderr: %s", stderr_text)
        raise RuntimeError(stderr_text or "yt-dlp exited with error")

    return sorted(glob.glob(f"{TMP_DIR}/{prefix}_*"))


def extract_audio_from_video(video_path: str, prefix: str) -> str | None:
    """Extract the audio track from a video file as mp3."""
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
        if candidates and os.path.getsize(candidates[0]) > 0:
            return candidates[0]
    except Exception as exc:
        logger.warning("[audio-extract] Failed: %s", exc)
    return None


def classify_file(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "photo"
    if ext in {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav"}:
        return "audio"
    return "document"


def cleanup(paths: list[str]) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


# =============================================================================
# Master process_url dispatcher
# =============================================================================

async def process_url(chat_id: int, msg_id: int, url: str) -> None:
    all_files: list[str] = []
    prefix = f"{chat_id}_{abs(hash(url))}"

    try:
        # ── 1. Expand short TikTok links ──────────────────────────────────────
        await edit_message_text(chat_id, msg_id, "🔍 <b>Analyzing URL...</b>")
        if is_tiktok_url(url):
            url = await expand_tiktok_url(url)

        # ── 2. Primary Downloader: yt-dlp ─────────────────────────────────────
        loop = asyncio.get_event_loop()
        await edit_message_text(chat_id, msg_id, "📥 <b>Initializing download stream...</b>")
        
        try:
            # Enforce slideshows directly to fallback since yt-dlp handles carousels poorly
            if is_tiktok_url(url) and is_tiktok_slideshow(url):
                raise RuntimeError("TikTok Slideshow bypassed yt-dlp")
                
            files = await run_yt_dlp_async(url, prefix, chat_id, msg_id)
            all_files = list(files)
            
        except RuntimeError as exc:
            err = str(exc).lower()
            logger.info(f"[dispatch] yt-dlp failed: {err}")
            await edit_message_text(chat_id, msg_id, "⚠️ <b>yt-dlp failed, trying platform-specific fallback...</b>")
            
            # ── 3. Hybrid Fallback Routing ────────────────────────────────────
            try:
                if is_tiktok_url(url):
                    all_files = await handle_tiktok_fallback(chat_id, msg_id, url, prefix)
                elif is_instagram_url(url) or is_facebook_url(url) or is_youtube_url(url):
                    all_files = await universal_platform_fallback(url, prefix, chat_id, msg_id)
                else:
                    if "private" in err or "login" in err:
                        await edit_message_text(chat_id, msg_id, "🔒 <b>This content is private or requires login.</b>")
                    else:
                        await edit_message_text(chat_id, msg_id, "😢 <b>Failed to download this content. Make sure the link is public.</b>")
                    return
            except Exception as fallback_exc:
                logger.error(f"[fallback] API failed: {fallback_exc}")
                await edit_message_text(chat_id, msg_id, "😢 <b>All download methods failed for this link.</b>")
                return

        if not all_files:
            await edit_message_text(chat_id, msg_id, "😢 <b>Failed to download this content. No media found.</b>")
            return

        # ── 4. Size filter ────────────────────────────────────────────────────
        oversized = [f for f in all_files if os.path.getsize(f) > MAX_BYTES]
        sendable  = [f for f in all_files if os.path.getsize(f) <= MAX_BYTES]

        if oversized:
            await send_text(
                chat_id,
                f"⚠️ <b>{len(oversized)}</b> file(s) exceeded the 50 MB Telegram limit and were skipped."
            )
        if not sendable:
            await edit_message_text(chat_id, msg_id, "😢 <b>All downloaded files exceed Telegram's 50 MB limit.</b>")
            return

        # ── 5. Classify files ─────────────────────────────────────────────────
        photos = [f for f in sendable if classify_file(f) == "photo"]
        videos = [f for f in sendable if classify_file(f) == "video"]
        audios = [f for f in sendable if classify_file(f) == "audio"]
        docs   = [f for f in sendable if classify_file(f) == "document"]

        await edit_message_text(chat_id, msg_id, "📤 <b>Uploading to Telegram...</b>")

        # ── 6. Photos (Carousels handled perfectly here) ──────────────────────
        if len(photos) > 1:
            for start in range(0, len(photos), 10):
                await send_media_group(chat_id, photos[start : start + 10])
        elif photos:
            await send_photo(chat_id, photos[0])

        # ── 7. Videos → document (no compression) + extracted audio ──────────
        for vf in videos:
            # Send video file — sendDocument prevents Telegram re-encoding
            await send_document(
                chat_id, vf,
                caption="🎬 <b>Video</b> — original quality, no compression"
            )
            # Extract and send audio track separately
            audio_path = await loop.run_in_executor(
                None, extract_audio_from_video, vf, prefix
            )
            if audio_path:
                all_files.append(audio_path)
                if os.path.getsize(audio_path) <= MAX_BYTES:
                    await send_audio(
                        chat_id, audio_path,
                        caption="🎵 <b>Audio track extracted from video</b>"
                    )

        # ── 8. Standalone audio files (Background music for carousels) ────────
        for af in audios:
            await send_audio(chat_id, af)

        # ── 9. Other documents ────────────────────────────────────────────────
        for df in docs:
            await send_document(chat_id, df)
            
        # Cleanup loading message on success
        await delete_message(chat_id, msg_id)

    except asyncio.TimeoutError:
        logger.exception("[process] Timeout for %s", url)
        await edit_message_text(chat_id, msg_id, "⏱️ <b>Download timed out.\nThe file may be too large or the platform is slow.</b>")
    except subprocess.TimeoutExpired:
        logger.exception("[process] yt-dlp timeout for %s", url)
        await edit_message_text(chat_id, msg_id, "⏱️ <b>Download timed out.\nThe file may be too large or the platform is slow.</b>")
    except Exception:
        logger.exception("[process] Unexpected error for %s", url)
        await edit_message_text(chat_id, msg_id, "😢 <b>An unexpected error occurred.\nPlease try again.</b>")
    finally:
        cleanup(all_files)


# =============================================================================
# Supported domains
# =============================================================================

SUPPORTED_DOMAINS = (
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "instagr.am",
    "youtube.com", "youtu.be",
    "twitter.com", "x.com", "t.co",
    "facebook.com", "fb.watch", "fb.com",
    "reddit.com", "redd.it",
    "twitch.tv",
    "vimeo.com",
    "pinterest.com",
    "snapchat.com",
)


def looks_like_url(text: str) -> bool:
    text = text.strip()
    return (
        text.startswith(("http://", "https://"))
        and any(d in text for d in SUPPORTED_DOMAINS)
    )


# =============================================================================
# Telegram update handler (shared by all routes)
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
            "I download media from any platform and deliver it in the "
            "<b>highest available quality</b> — no compression, ever.\n\n"
            "🎬 <b>TikTok</b> — Videos, Slideshows &amp; Carousels\n"
            "📸 <b>Instagram</b> — Reels, Posts, Carousels\n"
            "▶️ <b>YouTube</b>\n"
            "🐦 <b>Twitter / X</b>\n"
            "📘 <b>Facebook</b>\n"
            "🟠 <b>Reddit</b>\n"
            "🎮 <b>Twitch</b>\n"
            "🎞 <b>Vimeo</b> — and more!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to use:</b>\n"
            "1. Copy a link from any supported platform.\n"
            "2. Paste it here and send.\n"
            "3. I'll download and deliver it instantly! ⚡\n\n"
            "Type /help for detailed info."
        ))
        return JSONResponse({"ok": True})

    # ── /help ─────────────────────────────────────────────────────────────────
    if text.startswith("/help"):
        await send_text(chat_id, (
            "📖 <b>Universal Downloader Bot — Help</b>\n\n"
            "<b>Step 1:</b> Copy a media URL from a supported platform.\n"
            "<b>Step 2:</b> Paste it here and send.\n"
            "<b>Step 3:</b> Wait a moment — I'll send it right back!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ <b>Supported platforms:</b>\n"
            "TikTok · Instagram · YouTube · Twitter/X\n"
            "Facebook · Reddit · Vimeo · Twitch · Pinterest · Snapchat\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📦 <b>What you receive:</b>\n"
            "• <b>Videos</b> → sent as document (no Telegram compression)\n"
            "  + audio track extracted &amp; sent separately 🎵\n"
            "• <b>TikTok Slideshows / IG Carousels</b> → all photos as carousel\n"
            "  + background music sent separately 🎵\n"
            "• <b>Photos</b> → single photo or carousel\n\n"
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

    # ── Queue download & Trigger Animation ────────────────────────────────────
    msg_response = await send_text(chat_id, "⏳ <b>Initializing download...</b>")
    status_msg_id = msg_response.get("result", {}).get("message_id")
    
    # Process url in background, passing the message ID so it can animate status updates
    background_tasks.add_task(process_url, chat_id, status_msg_id, text)

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
    """Visit /set_webhook?url=https://your-domain.vercel.app"""
    webhook_url = url.rstrip("/") + "/webhook"
    return await tg_send(
        "setWebhook",
        json={"url": webhook_url, "drop_pending_updates": True},
    )
