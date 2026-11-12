import os
import glob
import time
import uuid
import shutil
import asyncio
import logging
import subprocess
from threading import Thread

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ------------------------------------------------------------------
# الإعدادات
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
raw_channel_id = os.environ.get("CHANNEL_ID")
CHANNEL_ID = int(raw_channel_id) if raw_channel_id else 0
CHANNEL_INVITE_LINK = os.environ.get("CHANNEL_INVITE_LINK")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "bot_downloads")

# الكوكيز: لو مرفوعة على Render كـ Secret File بتتنسخ لمكان قابل للكتابة
SECRET_COOKIES = "/etc/secrets/cookies.txt"
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
if os.path.exists(SECRET_COOKIES):
    try:
        shutil.copy(SECRET_COOKIES, COOKIES_FILE)
    except Exception as e:
        print(f"Could not copy cookies: {e}")

MAX_TG_SIZE = 50 * 1024 * 1024  # حد تليجرام للبوتات
MAX_CONCURRENT_DOWNLOADS = 2     # عدد التحميلات المتزامنة (مناسب لـ Render المجاني)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

download_semaphore = None  # بيتعمل جوه الـ event loop

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ------------------------------------------------------------------
# سيرفر Flask لـ UptimeRobot
# ------------------------------------------------------------------
web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Bot is alive and running!", 200


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()


# ------------------------------------------------------------------
# دوال مساعدة
# ------------------------------------------------------------------
def subscribe_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 انضم للقناة الآن", url=CHANNEL_INVITE_LINK)]]
    )


async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        if member.status in ("member", "administrator", "creator"):
            return True
        if member.status == "restricted" and getattr(member, "is_member", False):
            return True
        return False
    except Exception as e:
        logging.error(f"Error checking channel membership: {e}")
        return False


def cleanup_files(job_id: str):
    """مسح كل الملفات المؤقتة الخاصة بالعملية دي."""
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}*")):
        try:
            os.remove(path)
        except OSError:
            pass


def save_stream(url: str, path: str, headers: dict):
    """تنزيل ملف بالـ stream من غير ما يتحمل كله في الذاكرة."""
    with requests.get(url, headers=headers, timeout=60, stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)


def resolve_url(url: str) -> str:
    """فك اللينكات القصيرة زي vt.tiktok.com."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
            timeout=10,
        )
        return r.url.split("?")[0]
    except Exception as e:
        logging.warning(f"resolve_url failed: {e}")
        return url


def download_tiktok_sync(video_url: str, v_path: str, a_path: str) -> bool:
    """تحميل من تيك توك عبر tikwm API (3 محاولات). بترجع True لو نجح."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    full_url = resolve_url(video_url)

    for attempt in range(1, 4):
        try:
            response = requests.get(
                "https://www.tikwm.com/api/",
                params={"url": full_url},
                headers=headers,
                timeout=15,
            )
            res = response.json()
            if res.get("code") != 0:
                logging.warning(f"tikwm attempt {attempt}: {res.get('msg')}")
                time.sleep(1.5)
                continue

            data = res.get("data", {})
            play_url = data.get("play")
            if not play_url:
                logging.warning(f"tikwm attempt {attempt}: no play url")
                return False

            save_stream(play_url, v_path, headers)

            music_url = data.get("music")
            if music_url:
                try:
                    save_stream(music_url, a_path, headers)
                except Exception as e:
                    logging.warning(f"TikTok audio download failed: {e}")

            return os.path.exists(v_path) and os.path.getsize(v_path) > 0
        except Exception as e:
            logging.warning(f"tikwm attempt {attempt} failed: {e}")
            time.sleep(1.5)

    return False


def download_video_sync(video_url: str, job_id: str) -> str:
    """تحميل الفيديو بـ yt-dlp وبترجع مسار الملف."""
    if "youtube.com" in video_url or "youtu.be" in video_url:
        # نفضّل ملف مدمج mp4 تحت 50MB، وبعدين جودة 480 كحد أقصى
        format_setting = (
            "best[ext=mp4][filesize<50M]/"
            "best[ext=mp4][height<=480]/"
            "best[height<=480]/best"
        )
    else:
        format_setting = "best"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "noplaylist": True,
        "user_agent": USER_AGENT,
        "format": format_setting,
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{job_id}_video.%(ext)s"),
        "merge_output_format": "mp4",
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        v_path = None
        downloads = info.get("requested_downloads") if isinstance(info, dict) else None
        if downloads:
            v_path = downloads[0].get("filepath")
        if not v_path:
            v_path = ydl.prepare_filename(info)

    base = os.path.splitext(v_path)[0]
    if not os.path.exists(v_path) and os.path.exists(base + ".mp4"):
        v_path = base + ".mp4"
    return v_path


def extract_audio_sync(v_path: str, a_path: str) -> bool:
    """استخراج الصوت من الفيديو اللي نزل بدل تحميله مرة تانية."""
    if not shutil.which("ffmpeg"):
        logging.warning("ffmpeg not found, skipping audio extraction")
        return False
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", v_path,
                "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
                a_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=True,
        )
        return os.path.exists(a_path) and os.path.getsize(a_path) > 0
    except Exception as e:
        logging.warning(f"Audio extraction failed: {e}")
        return False


# ------------------------------------------------------------------
# الهاندلرز
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await update.message.reply_text(
            "⚠️ من فضلك انضم للقناه اولاً\n\nاشترك هنا وابعت لينك الفيديو تاني",
            reply_markup=subscribe_keyboard(),
        )
        return

    await update.message.reply_text("ابعت لينك الفيديو اللي حابب تنزلو 🎬")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id

    # فحص الاشتراك الإجباري
    if not await is_subscribed(user_id, context):
        await update.message.reply_text(
            "اشترك في القناه بس عشان تستخدم البوت براحتك 💖",
            reply_markup=subscribe_keyboard(),
        )
        return

    video_url = update.message.text.strip()
    if not (video_url.startswith("http://") or video_url.startswith("https://")):
        await update.message.reply_text("يرجى إرسال رابط صحيح.")
        return

    msg = await update.message.reply_text("جاري معالجة الرابط والتحميل... ⏳")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    job_id = f"{user_id}_{uuid.uuid4().hex[:8]}"
    v_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_video.mp4")
    a_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_audio.mp3")

    try:
        async with download_semaphore:
            tiktok_success = False

            # 1. تيك توك عبر الـ API أولاً (بيشمل اللينكات القصيرة vt/vm)
            if "tiktok.com" in video_url:
                tiktok_success = await asyncio.to_thread(
                    download_tiktok_sync, video_url, v_path, a_path
                )

            # 2. باقي المواقع (أو تيك توك لو الـ API فشل)
            if not tiktok_success:
                v_path = await asyncio.to_thread(download_video_sync, video_url, job_id)
                # الصوت بيتستخرج من الفيديو اللي نزل
                await asyncio.to_thread(extract_audio_sync, v_path, a_path)

        has_video = bool(v_path) and os.path.exists(v_path)
        has_audio = os.path.exists(a_path) and os.path.getsize(a_path) > 0

        if not has_video:
            await msg.edit_text("مقدرتش أحمل الفيديو من الرابط ده 😕")
            return

        # فحص الحجم قبل الإرسال
        video_too_big = os.path.getsize(v_path) > MAX_TG_SIZE
        audio_too_big = has_audio and os.path.getsize(a_path) > MAX_TG_SIZE

        if video_too_big:
            await update.message.reply_text(
                "الفيديو أكبر من 50MB وتليجرام مبيسمحش للبوتات بالحجم ده 😕"
            )
        else:
            with open(v_path, "rb") as vf:
                await update.message.reply_video(
                    video=vf,
                    caption="تم تحميل الفيديو بنجاح 🎉",
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=120,
                )
            await update.message.reply_text("متنساش تصلي ع النبي , وتدعي لأبويا بالرحمه 💖")

        if has_audio and not audio_too_big:
            with open(a_path, "rb") as af:
                await update.message.reply_audio(
                    audio=af,
                    caption="🎵 موسيقى الفيديو MP3",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=120,
                )

        try:
            await msg.delete()
        except Exception:
            pass

    except Exception as e:
        logging.error(f"Download error for {video_url}: {e}")
        error_text = str(e)
        if len(error_text) > 300:
            error_text = error_text[:300] + "..."
        try:
            await msg.edit_text(f"حدث خطأ أثناء التحميل:\n{error_text}")
        except Exception:
            pass

    finally:
        cleanup_files(job_id)


async def post_init(application):
    global download_semaphore
    download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# ------------------------------------------------------------------
# التشغيل
# ------------------------------------------------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN مش موجود في متغيرات البيئة")
    if not CHANNEL_ID:
        raise SystemExit("❌ CHANNEL_ID مش موجود أو غلط في متغيرات البيئة")
    if not CHANNEL_INVITE_LINK:
        raise SystemExit("❌ CHANNEL_INVITE_LINK مش موجود في متغيرات البيئة")

    keep_alive()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .write_timeout(120)
        .read_timeout(120)
        .connect_timeout(120)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 The Bot is running now ..")
    app.run_polling()
    