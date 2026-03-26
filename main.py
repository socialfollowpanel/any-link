import os
import glob
import asyncio
import subprocess
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]          # set in Vercel env vars
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"
TMP_DIR   = "/tmp"
MAX_BYTES = 50 * 1024 * 1024                 # 50 MB – standard Bot API cap

# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan)


# ═════════════════════════════════════════════════════════════════════════════
# Telegram helpers
# ═════════════════════════════════════════════════════════════════════════════

async def tg_send(method: str, **kwargs) -> dict:
    """Generic async Telegram API call."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{TG_API}/{method}", **kwargs)
        r.raise_for_status()
        return r.json()


async def send_text(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    await tg_send("sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})


async def send_document(chat_id: int, file_path: str, caption: str = "") -> None:
    """Send any file as a document (no compression)."""
    with open(file_path, "rb") as f:
        await tg_send(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (Path(file_path).name, f, "application/octet-stream")},
        )


async def send_photo(chat_id: int, file_path: str) -> None:
    with open(file_path, "rb") as f:
        await tg_send(
            "sendPhoto",
            data={"chat_id": chat_id},
            files={"photo": f},
        )


async def send_audio(chat_id: int, file_path: str) -> None:
    with open(file_path, "rb") as f:
        await tg_send(
            "sendAudio",
            data={"chat_id": chat_id},
            files={"audio": f},
        )


async def send_media_group(chat_id: int, image_paths: list[str]) -> None:
    """Send up to 10 images as a media group (carousel)."""
    # Build the media JSON array
    media = [{"type": "photo", "media": f"attach://photo{i}"} for i in range(len(image_paths))]
    files = {}
    for i, path in enumerate(image_paths[:10]):
        files[f"photo{i}"] = open(path, "rb")
    try:
        await tg_send(
            "sendMediaGroup",
            data={"chat_id": chat_id, "media": __import__("json").dumps(media)},
            files=files,
        )
    finally:
        for f in files.values():
            f.close()


# ═════════════════════════════════════════════════════════════════════════════
# Download logic
# ═════════════════════════════════════════════════════════════════════════════

def build_yt_dlp_cmd(url: str, output_template: str) -> list[str]:
    return [
        "yt-dlp",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "-o", output_template,
        url,
    ]


def download_media(url: str, chat_id: int) -> list[str]:
    """
    Run yt-dlp and return list of downloaded file paths.
    Raises subprocess.CalledProcessError on failure.
    """
    # Use chat_id+url hash as prefix so concurrent downloads don't collide
    prefix = f"{chat_id}_{abs(hash(url))}"
    output_template = f"{TMP_DIR}/{prefix}_%(id)s.%(ext)s"

    cmd = build_yt_dlp_cmd(url, output_template)
    logger.info("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=240,       # 4-minute hard cap; adjust to your worker limits
    )

    if result.returncode != 0:
        logger.error("yt-dlp stderr: %s", result.stderr)
        raise RuntimeError(result.stderr or "yt-dlp exited with error")

    # Collect files that match our prefix
    files = sorted(glob.glob(f"{TMP_DIR}/{prefix}_*"))
    return files


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


# ═════════════════════════════════════════════════════════════════════════════
# Core worker
# ═════════════════════════════════════════════════════════════════════════════

async def process_url(chat_id: int, url: str) -> None:
    files: list[str] = []
    try:
        # 1. Download
        loop = asyncio.get_event_loop()
        files = await loop.run_in_executor(None, download_media, url, chat_id)

        if not files:
            await send_text(chat_id, "Failed to download this content 😢\nNo media was found at that URL.")
            return

        # 2. Check sizes
        oversized = [f for f in files if os.path.getsize(f) > MAX_BYTES]
        sendable  = [f for f in files if os.path.getsize(f) <= MAX_BYTES]

        # Warn about oversized files
        if oversized:
            names = ", ".join(Path(f).name for f in oversized)
            await send_text(
                chat_id,
                f"⚠️ {len(oversized)} file(s) exceed the 50 MB Telegram limit and were skipped:\n{names}"
            )

        if not sendable:
            await send_text(chat_id, "All downloaded files exceed Telegram's 50 MB limit 😢")
            return

        # 3. Dispatch by type
        photos = [f for f in sendable if classify_file(f) == "photo"]
        videos = [f for f in sendable if classify_file(f) == "video"]
        audios = [f for f in sendable if classify_file(f) == "audio"]
        docs   = [f for f in sendable if classify_file(f) == "document"]

        # Photos → media group if plural, single photo otherwise
        if len(photos) > 1:
            await send_media_group(chat_id, photos)
        elif photos:
            await send_photo(chat_id, photos[0])

        # Videos sent as document to prevent Telegram re-encoding
        for vf in videos:
            await send_document(chat_id, vf, caption="🎬 Highest quality – no compression")

        for af in audios:
            await send_audio(chat_id, af)

        for df in docs:
            await send_document(chat_id, df)

    except subprocess.TimeoutExpired:
        logger.exception("yt-dlp timed out for %s", url)
        await send_text(chat_id, "Download timed out ⏱️\nThe media may be too large or the platform is slow.")

    except RuntimeError as exc:
        logger.exception("yt-dlp error for %s", url)
        msg = str(exc)
        if "Private" in msg or "private" in msg or "login" in msg.lower():
            await send_text(chat_id, "This content is private or requires login 🔒")
        else:
            await send_text(chat_id, "Failed to download this content 😢")

    except Exception:
        logger.exception("Unexpected error for %s", url)
        await send_text(chat_id, "An unexpected error occurred 😢")

    finally:
        cleanup(files)


# ═════════════════════════════════════════════════════════════════════════════
# Webhook endpoint
# ═════════════════════════════════════════════════════════════════════════════

SUPPORTED_DOMAINS = (
    "tiktok.com", "vm.tiktok.com",
    "instagram.com", "instagr.am",
    "youtube.com", "youtu.be",
    "twitter.com", "x.com", "t.co",
    "facebook.com", "fb.watch",
    "reddit.com", "redd.it",
    "twitch.tv",
    "vimeo.com",
    "pinterest.com",
    "snapchat.com",
)


def looks_like_url(text: str) -> bool:
    text = text.strip()
    return text.startswith(("http://", "https://")) and any(d in text for d in SUPPORTED_DOMAINS)


# ═════════════════════════════════════════════════════════════════════════════
# Shared update handler (called by both POST / and POST /webhook)
# ═════════════════════════════════════════════════════════════════════════════

async def _handle_update(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
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

    # ── Commands ──────────────────────────────────────────────────────────────
    if text.startswith("/start"):
        welcome = (
            f"👋 <b>Hey {first_name}, welcome to Universal Downloader Bot!</b>\n\n"
            "I can download media from any of these platforms and send it back "
            "to you in the <b>highest available quality</b> — no compression.\n\n"
            "🎬 <b>TikTok</b>\n"
            "📸 <b>Instagram</b> — Reels, Posts, Carousels\n"
            "▶️ <b>YouTube</b>\n"
            "🐦 <b>Twitter / X</b>\n"
            "📘 <b>Facebook</b>\n"
            "🟠 <b>Reddit</b>\n"
            "🎮 <b>Twitch</b>\n"
            "🎞 <b>Vimeo</b> — and more!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to use:</b>\n"
            "1. Copy a media link from any supported platform.\n"
            "2. Paste it here and hit send.\n"
            "3. I'll download and deliver it right back! ⚡\n\n"
            "Need help? Just type /help"
        )
        await send_text(chat_id, welcome)
        return JSONResponse({"ok": True})

    if text.startswith("/help"):
        help_msg = (
            "📖 <b>How to use Universal Downloader Bot</b>\n\n"
            "<b>Step 1:</b> Copy a media URL from a supported platform.\n"
            "<b>Step 2:</b> Paste it here.\n"
            "<b>Step 3:</b> Wait a moment — I'll download and send it back!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ <b>Supported platforms:</b>\n"
            "TikTok · Instagram · YouTube · Twitter/X\n"
            "Facebook · Reddit · Vimeo · Twitch · Pinterest · Snapchat\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Limitations:</b>\n"
            "• Files above 50 MB cannot be sent via Telegram.\n"
            "• Private or login-protected content cannot be downloaded.\n\n"
            "If something fails, make sure the link is public and try again! 🔁"
        )
        await send_text(chat_id, help_msg)
        return JSONResponse({"ok": True})

    if not looks_like_url(text):
        await send_text(
            chat_id,
            "⚠️ Please send a valid URL from a supported platform.\n"
            "Use /help to see the list of supported platforms.",
        )
        return JSONResponse({"ok": True})

    # Acknowledge immediately so Telegram doesn't retry
    await send_text(chat_id, "Downloading... ⏳ This may take a moment.")

    # Heavy work in background
    background_tasks.add_task(process_url, chat_id, text)

    return JSONResponse({"ok": True})


# ═════════════════════════════════════════════════════════════════════════════
# Routes — accept Telegram updates on both / and /webhook
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/")
async def webhook_root(request: Request, background_tasks: BackgroundTasks):
    return await _handle_update(request, background_tasks)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    return await _handle_update(request, background_tasks)


# ═════════════════════════════════════════════════════════════════════════════
# Health check & webhook setup
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/")
async def health():
    return {"status": "ok", "bot": "Universal Telegram Downloader"}


@app.get("/set_webhook")
async def set_webhook(url: str):
    """
    Visit /set_webhook?url=https://your-domain.vercel.app/webhook
    This registers the correct /webhook path with Telegram.
    """
    # Always point Telegram to the /webhook path
    webhook_url = url.rstrip("/") + "/webhook"
    result = await tg_send("setWebhook", json={"url": webhook_url, "drop_pending_updates": True})
    return result
