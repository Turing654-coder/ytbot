"""
YouTube Downloader Telegram Bot
--------------------------------
Flow:
  1. User sends a YouTube link.
  2. Bot replies with [Video] [Audio] buttons.
  3. Bot replies with quality buttons depending on the choice
     (e.g. 1080p/720p/480p/360p for video, 320/192/128 kbps for audio).
  4. Bot downloads the chosen format with yt-dlp and sends the file back.

Requirements: python-telegram-bot>=21, yt-dlp, ffmpeg (system binary)
Run: python bot.py   (set BOT_TOKEN env var first)
"""

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Telegram Bot API upload limit for regular bots is 50MB.
# If you self-host a local Bot API server, this can go up to 2000MB.
MAX_UPLOAD_MB = 50

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/\S+"
)

# imageio-ffmpeg ships a self-contained ffmpeg binary, so no system install
# is required — it downloads a prebuilt binary the first time it's imported.
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

VIDEO_HEIGHT_LADDER = [1080, 720, 480, 360, 240, 144]
AUDIO_BITRATE_LADDER = [320, 192, 128, 64]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory session store: short_id -> {"url": str, "title": str, "heights": [...], "abrs": [...]}
# For a production bot swap this for redis / sqlite so it survives restarts.
SESSIONS: dict[str, dict] = {}


# --------------------------------------------------------------------------
# yt-dlp helpers (these are blocking, so always call via asyncio.to_thread)
# --------------------------------------------------------------------------

def probe_video(url: str) -> dict:
    """Fetch metadata + available formats without downloading."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    heights = sorted(
        {f.get("height") for f in info.get("formats", []) if f.get("vcodec") != "none" and f.get("height")},
        reverse=True,
    )
    available_heights = [h for h in VIDEO_HEIGHT_LADDER if h in heights] or heights[:4]

    return {
        "title": info.get("title", "video"),
        "duration": info.get("duration"),
        "heights": available_heights,
    }


def download_video(url: str, height: int, out_path: Path) -> Path:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
        "merge_output_format": "mp4",
        "outtmpl": str(out_path.with_suffix("")) + ".%(ext)s",
        "ffmpeg_location": FFMPEG_PATH,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4")


def download_audio(url: str, bitrate: int, out_path: Path) -> Path:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(out_path.with_suffix("")) + ".%(ext)s",
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(bitrate),
            }
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)
    return out_path.with_suffix(".mp3")


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a YouTube link and I'll let you pick video or audio, "
        "then a quality, then download it for you."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if not YOUTUBE_URL_RE.search(text):
        await update.message.reply_text("That doesn't look like a YouTube link.")
        return

    msg = await update.message.reply_text("Fetching video info...")

    try:
        info = await asyncio.to_thread(probe_video, text)
    except Exception as e:
        logger.exception("probe failed")
        await msg.edit_text(f"Couldn't read that link: {e}")
        return

    sid = uuid.uuid4().hex[:10]
    SESSIONS[sid] = {"url": text, "title": info["title"], "heights": info["heights"]}

    keyboard = [
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"kind:{sid}:video"),
            InlineKeyboardButton("🎵 Audio", callback_data=f"kind:{sid}:audio"),
        ]
    ]
    await msg.edit_text(
        f"<b>{info['title']}</b>\nChoose a format:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_kind_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, sid, kind = query.data.split(":")

    session = SESSIONS.get(sid)
    if not session:
        await query.edit_message_text("This session expired, send the link again.")
        return

    if kind == "video":
        buttons = [
            InlineKeyboardButton(f"{h}p", callback_data=f"q:{sid}:video:{h}")
            for h in session["heights"]
        ]
    else:
        buttons = [
            InlineKeyboardButton(f"{b} kbps", callback_data=f"q:{sid}:audio:{b}")
            for b in AUDIO_BITRATE_LADDER
        ]

    # 2 buttons per row
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    await query.edit_message_text(
        f"<b>{session['title']}</b>\nChoose {kind} quality:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, sid, kind, quality = query.data.split(":")

    session = SESSIONS.get(sid)
    if not session:
        await query.edit_message_text("This session expired, send the link again.")
        return

    await query.edit_message_text(f"Downloading {kind} ({quality})... this can take a bit.")

    out_path = DOWNLOAD_DIR / f"{sid}"
    try:
        if kind == "video":
            file_path = await asyncio.to_thread(download_video, session["url"], int(quality), out_path)
        else:
            file_path = await asyncio.to_thread(download_audio, session["url"], int(quality), out_path)
    except Exception as e:
        logger.exception("download failed")
        await query.edit_message_text(f"Download failed: {e}")
        return

    size_mb = file_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        await query.edit_message_text(
            f"File is {size_mb:.1f}MB, which is over Telegram's {MAX_UPLOAD_MB}MB bot upload limit. "
            "Try a lower quality, or run a local Bot API server to raise the limit to 2GB."
        )
        file_path.unlink(missing_ok=True)
        return

    try:
        with open(file_path, "rb") as f:
            if kind == "video":
                await context.bot.send_video(chat_id=query.message.chat_id, video=f, caption=session["title"], supports_streaming=True)
            else:
                await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, caption=session["title"])
        await query.edit_message_text("Done ✅")
    finally:
        file_path.unlink(missing_ok=True)
        SESSIONS.pop(sid, None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit("Set the BOT_TOKEN environment variable before running.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_kind_choice, pattern=r"^kind:"))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^q:"))
    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
