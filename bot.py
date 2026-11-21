"""
بات دانلودر اینستاگرام با RadRub (Rubika Bot API v3)
امکانات: دانلود ویدیو (با انتخاب کیفیت)، دانلود صدا، دانلود عکس از پست.
نصب:  python -m pip install -r requirements.txt
اجرا:  RUBIKA_BOT_TOKEN=... python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import instaloader
import yt_dlp
from radrub import AsyncBot, Dispatcher, Polling, Router, chat

TOKEN = os.environ.get("RUBIKA_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("❌ متغیر محیطی RUBIKA_BOT_TOKEN تنظیم نشده است")

MAX_FILE_BYTES = 100 * 1024 * 1024  # ۱۰۰ مگابایت

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("insta_bot")

router = Router(name="insta")
states: Dict[str, Dict[str, Any]] = {}

LINK_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?P<kind>p|reel|reels|tv)/(?P<code>[\w\-]+)/?",
    re.IGNORECASE,
)

MENU_KB = chat(
    [("dl_video", "🎬 دانلود ویدیو"), ("dl_audio", "🎵 دانلود صدا")],
    [("dl_photo", "🖼 دانلود عکس"), ("help", "📖 راهنما")],
    resize=True,
)
QUALITY_KB = chat([("q_best", "بهترین کیفیت"), ("q_480", "۴۸۰p (حجم کمتر)")], [("back", "🔙 بازگشت")], resize=True)
CONFIRM_KB = chat([("send_yes", "✅ بفرست"), ("send_no", "❌ نفرست")], resize=True)
BACK_KB = chat([("back", "🔙 بازگشت")], resize=True)

WELCOME = "سلام! 👋\nیکی از گزینه‌های زیر را انتخاب کنید:"
HELP = (
    "📖 راهنما\n"
    "🎬 دانلود ویدیو: ویدیو یا ریلز را با کیفیت دلخواه می‌گیرد.\n"
    "🎵 دانلود صدا: صدای ریلز یا ویدیو را می‌فرستد.\n"
    "🖼 دانلود عکس: عکس یک پست را می‌فرستد.\n"
    "فقط لینک‌های عمومی اینستاگرام پشتیبانی می‌شوند."
)


# ─── yt-dlp (بلاکینگ) ─────────────────────────────────────────────────────

def _probe(url: str) -> str:
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return (info or {}).get("title") or "بدون عنوان"


def _download_media(url: str, dest: Path, mode: str, quality: str) -> Path:
    if mode == "audio":
        fmt = "bestaudio[ext=m4a]/bestaudio"
    elif quality == "480":
        fmt = "best[height<=480][ext=mp4]/best[height<=480]/best"
    else:
        fmt = "mp4/best[ext=mp4]/best"
    opts = {
        "quiet": True,
        "noplaylist": True,
        "format": fmt,
        "outtmpl": str(dest / "media.%(ext)s"),
        "max_filesize": MAX_FILE_BYTES,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in dest.iterdir() if p.is_file()]
    if not files:
        raise RuntimeError("فایلی دانلود نشد")
    return files[0]


def _photo_urls(shortcode: str) -> list[str]:
    """آدرس عکس‌های یک پست عمومی را برمی‌گرداند (حداکثر ۵ تا)."""
    loader = instaloader.Instaloader(quiet=True, download_pictures=False, save_metadata=False)
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    if post.typename == "GraphSidecar":
        nodes = list(post.get_sidecar_nodes())[:5]
        return [n.display_url for n in nodes if not n.is_video]
    if post.is_video:
        raise RuntimeError("این پست ویدیو است؛ از گزینه ویدیو استفاده کنید")
    return [post.url]


# ─── کمک‌کننده‌ها ─────────────────────────────────────────────────────────

def _reset(chat_id: str) -> None:
    st = states.pop(chat_id, None)
    if st and st.get("tmp"):
        shutil.rmtree(st["tmp"], ignore_errors=True)


def _menu_reset(chat_id: str) -> None:
    _reset(chat_id)


async def _show_menu(bot, chat_id: str, text: str = WELCOME) -> None:
    _reset(chat_id)
    await bot.send_message(chat_id, text, chat_keypad=MENU_KB, chat_keypad_type="New")


async def _ask_link(bot, chat_id: str, mode: str, quality: str = "best") -> None:
    states[chat_id] = {"stage": "await_link", "mode": mode, "quality": quality}
    hint = {
        "video": "🔗 لینک ویدیو یا ریلز عمومی اینستاگرام را بفرستید.",
        "audio": "🔗 لینک ریلز یا ویدیو عمومی اینستاگرام را بفرستید تا صدایش را بفرستم.",
        "photo": "🔗 لینک پست عکس عمومی اینستاگرام را بفرستید.",
    }[mode]
    await bot.send_message(chat_id, hint, chat_keypad=BACK_KB, chat_keypad_type="New")


async def _handle_link(bot, chat_id: str, text: str) -> None:
    st = states[chat_id]
    mode = st["mode"]
    m = LINK_RE.search(text)
    if not m or (mode == "photo" and m.group("kind") != "p"):
        await bot.send_message(
            chat_id,
            "❌ لینک معتبر نیست. لینک عمومی اینستاگرام بفرستید"
            + (" (برای عکس، لینک پست /p/ لازم است)." if mode == "photo" else "."),
            chat_keypad=BACK_KB,
            chat_keypad_type="New",
        )
        return

    url = m.group(0)
    await bot.send_message(chat_id, "🔎 در حال بررسی...")
    try:
        if mode == "photo":
            urls = await asyncio.to_thread(_photo_urls, m.group("code"))
            title = f"{len(urls)} عکس از پست"
        else:
            title = await asyncio.to_thread(_probe, url)
    except Exception:
        log.exception("probe failed for %s", url)
        await _show_menu(bot, chat_id, "❌ محتوا پیدا نشد یا خصوصی است. فقط پست‌های عمومی پشتیبانی می‌شوند.")
        return

    st.update(
        stage="confirm",
        url=url,
        code=m.group("code"),
        title=title,
        tmp=tempfile.mkdtemp(prefix="insta_"),
    )
    label = {"video": "ویدیو", "audio": "صدا", "photo": "عکس"}[mode]
    await bot.send_message(
        chat_id,
        f"🎯 {label} پیدا شد:\n{title}\n\nبرایتان ارسال کنم؟",
        chat_keypad=CONFIRM_KB,
        chat_keypad_type="New",
    )


async def _send_photos(bot, chat_id: str, st: Dict[str, Any]) -> None:
    urls = await asyncio.to_thread(_photo_urls, st["code"])
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for i, u in enumerate(urls, 1):
            resp = await client.get(u)
            resp.raise_for_status()
            path = Path(st["tmp"]) / f"photo_{i}.jpg"
            path.write_bytes(resp.content)
            file_id = await bot.upload_file(path, file_type="Image")
            await bot.send_file(chat_id, file_id, text=f"🖼 {i}/{len(urls)}")


async def _handle_send(bot, chat_id: str) -> None:
    st = states.get(chat_id)
    if not st or st.get("stage") != "confirm":
        await _show_menu(bot, chat_id, "ابتدا یک گزینه انتخاب کنید.")
        return

    mode = st["mode"]
    await bot.send_message(chat_id, "⏳ در حال دانلود و ارسال...")
    try:
        if mode == "photo":
            await _send_photos(bot, chat_id, st)
        else:
            path = await asyncio.to_thread(_download_media, st["url"], Path(st["tmp"]), mode, st["quality"])
            file_type = "Video" if mode == "video" else "File"
            file_id = await bot.upload_file(path, file_type=file_type)
            await bot.send_file(chat_id, file_id, text=f"📥 {st['title']}")
        await _show_menu(bot, chat_id, "✅ انجام شد. گزینه بعدی را انتخاب کنید:")
    except Exception:
        log.exception("send failed for %s", st.get("url") or st.get("code"))
        await _show_menu(bot, chat_id, "❌ خطا در دانلود یا ارسال. دوباره تلاش کنید.")


# ─── هندلر اصلی ───────────────────────────────────────────────────────────

@router.message()
async def on_update(update, bot: AsyncBot) -> None:
    chat_id = update.chat_id
    button = update.button_id
    text = (update.text or "").strip()
    st = states.get(chat_id, {})

    if text in ("/start", "start", "شروع", "/menu", "منو") or button == "back" and not st:
        await _show_menu(bot, chat_id)
        return
    if button == "back":
        await _show_menu(bot, chat_id)
        return
    if button == "help" or text in ("/help", "راهنما"):
        await bot.send_message(chat_id, HELP, chat_keypad=MENU_KB, chat_keypad_type="New")
        return

    if button == "dl_video":
        states[chat_id] = {"stage": "quality"}
        await bot.send_message(chat_id, "کیفیت ویدیو را انتخاب کنید:", chat_keypad=QUALITY_KB, chat_keypad_type="New")
        return
    if button in ("q_best", "q_480"):
        await _ask_link(bot, chat_id, "video", "480" if button == "q_480" else "best")
        return
    if button == "dl_audio":
        await _ask_link(bot, chat_id, "audio")
        return
    if button == "dl_photo":
        await _ask_link(bot, chat_id, "photo")
        return

    if button == "send_yes":
        await _handle_send(bot, chat_id)
        return
    if button == "send_no":
        await _show_menu(bot, chat_id, "❌ لغو شد. گزینه دیگری را انتخاب کنید:")
        return

    if st.get("stage") == "await_link" and text:
        await _handle_link(bot, chat_id, text)
        return
    if st.get("stage") == "confirm":
        await bot.send_message(chat_id, "لطفاً «✅ بفرست» یا «❌ نفرست» را بزنید.", chat_keypad=CONFIRM_KB, chat_keypad_type="New")
        return

    await _show_menu(bot, chat_id)


async def main() -> None:
    async with AsyncBot(TOKEN) as bot:
        me = await bot.get_me()
        log.info("✅ بات آنلاین شد → @%s", me.username)
        await Polling(bot, Dispatcher(router), interval=2.0).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("بات متوقف شد.")
