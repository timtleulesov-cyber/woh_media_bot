import os
import re
import json
import asyncio
import logging
import urllib.request
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
import yt_dlp

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Поддерживаемые домены
SUPPORTED_DOMAINS = [
    # TikTok
    "tiktok.com", "douyin.com",
    # Instagram
    "instagram.com", "instagr.am", "ddinstagram.com",
    # Twitter / X
    "twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com",
    # YouTube
    "youtube.com", "youtu.be",
    # VK
    "vk.com", "vkvideo.ru"
]

def twitter_fallback_download(url: str, output_filepath: str) -> str | None:
    """Запасной метод для Twitter/X при блокировках или 18+ ограничениях."""
    match = re.search(r"(?:twitter\.com|x\.com)/([^/?]+)/status/(\d+)", url)
    if not match:
        return None
        
    user, tweet_id = match.group(1), match.group(2)
    api_url = f"https://api.fxtwitter.com/{user}/status/{tweet_id}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            tweet = data.get("tweet", {})
            
            # Проверяем видео в самом твите или в цитате (quote tweet)
            videos = tweet.get("media", {}).get("videos", [])
            if not videos and "quote" in tweet:
                videos = tweet["quote"].get("media", {}).get("videos", [])
                
            if videos and "url" in videos[0]:
                direct_video_url = videos[0]["url"]
                # Скачиваем прямое MP4 видео
                dl_req = urllib.request.Request(direct_video_url, headers=headers)
                with urllib.request.urlopen(dl_req, timeout=30) as dl_resp:
                    with open(output_filepath, "wb") as f:
                        f.write(dl_resp.read())
                    return output_filepath
    except Exception as e:
        logging.error(f"Ошибка Twitter fallback: {e}")
    return None

def download_media(url: str) -> tuple[str | None, str | None]:
    """Основная функция загрузки медиа."""
    out_template = "downloads/%(id)s.%(ext)s"
    
    is_youtube = any(yt_domain in url.lower() for yt_domain in ["youtube.com", "youtu.be"])
    is_twitter = any(tw_domain in url.lower() for tw_domain in ["twitter.com", "x.com"])

    # Для YouTube склеиваем потоки. Для TikTok/Instagram/Twitter берем цельный mp4
    # чтобы не перезаписывать оригинальный звук на фоновую музыку.
    format_rule = (
        "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        if is_youtube else
        "best[ext=mp4]/best"
    )

    ydl_opts = {
        "format": format_rule,
        "outtmpl": out_template,
        "max_filesize": 49 * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }
    
    # Попытка 1: Скачивание через стандартный yt-dlp
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info), None
    except Exception as e:
        err_msg = str(e)
        logging.warning(f"Основной метод не сработал ({err_msg}). Проверяем резервные методы...")

        # Попытка 2: Если это Twitter/X, подключаем резервный FxTwitter шлюз
        if is_twitter:
            fallback_filename = f"downloads/tw_{os.urandom(4).hex()}.mp4"
            fallback_res = twitter_fallback_download(url, fallback_filename)
            if fallback_res:
                return fallback_res, None

        return None, err_msg

async def process_url(message: Message, url: str):
    status_msg = await message.reply("⏳ Загружаю ролик...")
    
    loop = asyncio.get_running_loop()
    file_path, error = await loop.run_in_executor(None, download_media, url)

    if file_path and os.path.exists(file_path):
        try:
            video = FSInputFile(file_path)
            await message.reply_video(video=video)
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"⚠️ Не удалось отправить видео в Telegram: {e}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
    else:
        err_str = str(error)
        if "File is larger than" in err_str or "max_filesize" in err_str:
            msg = "❌ Видео весит больше 50 МБ (максимум для Telegram бота)."
        elif "Private video" in err_str or "login" in err_str.lower():
            msg = "❌ Видео или аккаунт закрыты настройками приватности."
        else:
            msg = f"❌ Не удалось скачать. Ошибка: {error[:100] if error else 'нет доступного видео в посте'}"
        await status_msg.edit_text(msg)

def extract_valid_url(text: str) -> str | None:
    if not text:
        return None
    links = re.findall(r"https?://[^\s]+", text)
    for link in links:
        clean_link = link.rstrip(".,!?;:)\"'>»")
        lower_link = clean_link.lower()
        if any(domain in lower_link for domain in SUPPORTED_DOMAINS):
            return clean_link
    return None

@dp.channel_post()
async def handle_channel_post(message: Message):
    url = extract_valid_url(message.text or message.caption)
    if url:
        await process_url(message, url)

@dp.message()
async def handle_chat_message(message: Message):
    url = extract_valid_url(message.text or message.caption)
    if url:
        await process_url(message, url)

# Веб-сервер для поддержания активности на бесплатном тарифе Render
async def health_check(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    os.makedirs("downloads", exist_ok=True)
    await start_web_server()
    logging.info("Бот успешно запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
