import os
import re
import json
import random
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

# Расширения видеофайлов для Discord
VIDEO_EXTENSIONS = [".mp4", ".mov", ".webm", ".mkv", ".m4v"]

# Случайные фразы на случай неудач
FAIL_MESSAGES = [
    "чет не получилось...",
    "сори, не в этот раз...",
    "не вышло скачать, сорян :(",
    "чет пошло не так, не смог достать ролик...",
    "увы, в этот раз не судьба..."
]

def twitter_fallback_download(url: str, output_filepath: str) -> str | None:
    """Резервное скачивание из Twitter/X через API в обход ограничений."""
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
            
            # Проверяем видео в посте или цитате
            videos = tweet.get("media", {}).get("videos", [])
            if not videos and "quote" in tweet:
                videos = tweet["quote"].get("media", {}).get("videos", [])
                
            if videos and "url" in videos[0]:
                direct_video_url = videos[0]["url"]
                dl_req = urllib.request.Request(direct_video_url, headers=headers)
                with urllib.request.urlopen(dl_req, timeout=30) as dl_resp:
                    with open(output_filepath, "wb") as f:
                        f.write(dl_resp.read())
                    return output_filepath
    except Exception as e:
        logging.error(f"Ошибка Twitter fallback: {e}")
    return None

def extract_valid_url(text: str) -> str | None:
    """Ищет только ссылки на TikTok, Twitter/X и ролики из Discord."""
    if not text:
        return None
        
    links = re.findall(r"https?://[^\s]+", text)
    for link in links:
        clean_link = link.rstrip(".,!?;:)\"'>»")
        lower_link = clean_link.lower()

        # 1. TikTok
        if "tiktok.com" in lower_link or "douyin.com" in lower_link:
            return clean_link

        # 2. Twitter / X
        if any(tw in lower_link for tw in ["twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com"]):
            return clean_link

        # 3. Discord (видеовложения и клипы)
        is_discord = any(d in lower_link for d in [
            "cdn.discordapp.com",
            "media.discordapp.net",
            "cdn.discord.com",
            "discord.com"
        ])
        if is_discord:
            clean_path = lower_link.split("?")[0]
            if any(clean_path.endswith(ext) for ext in VIDEO_EXTENSIONS) or "/clips/" in lower_link:
                return clean_link

    return None

def download_media(url: str) -> tuple[str | None, str | None]:
    """Скачивание медиа."""
    out_template = "downloads/%(id)s.%(ext)s"
    is_twitter = any(tw_domain in url.lower() for tw_domain in ["twitter.com", "x.com"])

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": out_template,
        "max_filesize": 49 * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }
    
    # Сначала пробуем через yt-dlp
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info), None
    except Exception as e:
        err_msg = str(e)
        logging.warning(f"Основной метод не сработал ({err_msg}).")

        # Если это Твиттер, пробуем резервный метод
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
        except Exception:
            await status_msg.edit_text(random.choice(FAIL_MESSAGES))
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
    else:
        err_str = str(error) if error else ""
        # Если файл просто слишком большой для лимитов телеграма
        if "File is larger than" in err_str or "max_filesize" in err_str:
            await status_msg.edit_text("ролик слишком жирный (больше 50 МБ), телега не пустит сори")
        else:
            await status_msg.edit_text(random.choice(FAIL_MESSAGES))

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

# Веб-сервер для фоновой работы на Render
async def health_check(request):
    return web.Response(text="Bot is running OK!")

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
