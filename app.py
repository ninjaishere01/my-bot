import os
import time
import logging
import requests
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# --- إعداد سيرفر Flask لـ UptimeRobot ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Bot is alive and running!", 200

def run_flask():
    # Render يمرر البورت تلقائياً عبر متغير البيئة PORT
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
# ------------------------------------------

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

BOT_TOKEN = "8710810061:AAFFog3scVzNKJDFPM10xl79ju0_pcgfPpQ"
CHANNEL_ID = -1004249457655
CHANNEL_INVITE_LINK = "https://t.me/+C0nM4ztVTZpjNDdk"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logging.error(f"Error checking channel membership: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        keyboard = [
            [InlineKeyboardButton("📢 انضم للقناة الآن", url=CHANNEL_INVITE_LINK)],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚠️ من فضلك انضم للقناه اولاً  \n\n اشترك هنا وابعت للينك الفديو تاني",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        " ابعت للينك الفديو اللي حابب تنزلو 🎬"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # فحص الاشتراك الإجباري
    if not await is_subscribed(user_id, context):
        keyboard = [
            [InlineKeyboardButton("📢 انضم للقناة الآن", url=CHANNEL_INVITE_LINK)],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            " \n  اشترك في القناه بس عشان تستخدم البوت براحتك 💖",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    video_url = update.message.text.strip()
    
    if not (video_url.startswith("http://") or video_url.startswith("https://")):
        await update.message.reply_text("يرجى إرسال رابط صحيح.")
        return

    msg = await update.message.reply_text("جاري معالجة الرابط والتحميل... ⏳")
    
    timestamp = int(time.time())
    download_path = os.path.join(BASE_DIR, "bot_downloads")
    os.makedirs(download_path, exist_ok=True)

    v_path = ""
    a_path = ""

    try:
        tiktok_success = False

        # 1. محاولة التحميل من تيك توك عبر الـ API أولاً
        if 'tiktok.com' in video_url:
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'application/json'
                }
                api_url = f"https://www.tikwm.com/api/?url={video_url}"
                response = requests.get(api_url, headers=headers, timeout=15)
                
                # التأكد من أن الاستجابة جاءت بصيغة JSON فعلياً
                if response.status_code == 200 and 'application/json' in response.headers.get('Content-Type', ''):
                    res = response.json()
                    if res.get("code") == 0:
                        data = res.get("data", {})
                        v_path = os.path.join(download_path, f"{timestamp}_video.mp4")
                        a_path = os.path.join(download_path, f"{timestamp}_audio.mp3")

                        video_bytes = requests.get(data.get("play"), headers=headers, timeout=60).content
                        with open(v_path, 'wb') as f:
                            f.write(video_bytes)

                        music_url = data.get("music")
                        if music_url:
                            audio_bytes = requests.get(music_url, headers=headers, timeout=60).content
                            with open(a_path, 'wb') as f:
                                f.write(audio_bytes)

                        tiktok_success = True
            except Exception as e:
                logging.warning(f"TikTok API failed, shifting to yt-dlp: {e}")

        # 2. التحميل لجميع المواقع (أو التيك توك في حال فشل الـ API)
        if not tiktok_success:
            cookies_file = os.path.join(BASE_DIR, 'cookies.txt')
            common_opts = {
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            if os.path.exists(cookies_file):
                common_opts['cookiefile'] = cookies_file

            if 'youtube.com' in video_url or 'youtu.be' in video_url:
                format_setting = 'bestvideo+bestaudio/best'
            else:
                format_setting = 'best'

            ydl_opts_video = {
                **common_opts,
                'format': format_setting,
                'outtmpl': f'{download_path}/{timestamp}_video.%(ext)s',
                'merge_output_format': 'mp4',
            }

            ydl_opts_audio = {
                **common_opts,
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': f'{download_path}/{timestamp}_audio.%(ext)s',
            }

            # تنزيل الفيديو
            with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
                info_v = ydl.extract_info(video_url, download=True)
                v_path = ydl.prepare_filename(info_v)
                
                base_filepath = os.path.splitext(v_path)[0]
                if os.path.exists(f"{base_filepath}.mp4"):
                    v_path = f"{base_filepath}.mp4"

            # تنزيل الصوت منفصلاً
            try:
                with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl:
                    info_a = ydl.extract_info(video_url, download=True)
                    raw_a = ydl.prepare_filename(info_a)
                    a_path = raw_a.rsplit('.', 1)[0] + '.mp3'
            except Exception as audio_err:
                logging.warning(f"Could not extract audio separately: {audio_err}")

        # إرسال الفيديو للمستخدم
        if v_path and os.path.exists(v_path):
            with open(v_path, 'rb') as vf:
                await update.message.reply_video(
                    video=vf, 
                    caption="تم تحميل الفيديو بنجاح 🎉",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=120
                )
        await update.message.reply_text(" متنساش تصلي ع النبي , وتدعي لأبويا بالرحمه 💖 ")

        # إرسال الصوت للمستخدم
        if a_path and os.path.exists(a_path):
            with open(a_path, 'rb') as af:
                await update.message.reply_audio(
                    audio=af, 
                    caption="🎵 موسيقي الفديو MP3",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=120
                )

        try:
            await msg.delete()
        except:
            pass

    except Exception as e:
        await msg.edit_text(f"حدث خطأ أثناء التحميل: {str(e)}")

    finally:
        # مسح الملفات المؤقتة
        if v_path and os.path.exists(v_path):
            try: os.remove(v_path)
            except: pass
        if a_path and os.path.exists(a_path):
            try: os.remove(a_path)
            except: pass

if __name__ == '__main__':
    # تشغيل سيرفر Flask كي تستجيب الخدمة للـ Pings
    keep_alive()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .write_timeout(120)
        .read_timeout(120)
        .connect_timeout(120)
        .build()
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 The Bot is running now ..")
    app.run_polling()