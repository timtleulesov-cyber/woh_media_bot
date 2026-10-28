import os
import re
import asyncio
import logging
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

# Список всех поддерживаемых сервисов и их зеркал
SUPPORTED_DOMAINS = [
    # TikTok
    "tiktok.com",
    "douyin.com",
    # Instagram
    "instagram.com",
    "instagr.am",
    "ddinstagram.com",
    # Twitter / X
    "twitter.com",
    "x.com",
    "fxtwitter.com",
    "vxtwitter.com",
    # YouTube
    "youtube.com",
    "youtu.be",
    # VK
    "vk.com",
    "vkvideo.ru"
]

def download_media(url: str) -> tuple[str | None, str | None]:
    """Скачивание медиафайла через yt-dlp."""
    out_template = "downloads/%(id)s.%(ext)s"
    ydl_opts = {
        # Склейка лучшего видео до 1080p со звуком или взятие лучшего готового mp4
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_template,
        "max_filesize": 49 * 1024 * 1024,  # лимит 49 МБ для Telegram
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Обход блокировок YouTube и ограничений соцсетей
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info), None
        except Exception as e:
            err_msg = str(e)
            logging.error(f"Ошибка загрузки: {err_msg}")
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
        # Понятные пояснения частых ошибок
        err_str = str(error)
        if "Sign in to confirm" in err_str:
            msg = "❌ Сервис заблокировал скачивание без авторизации."
        elif "File is larger than" in err_str or "max_filesize" in err_str:
            msg = "❌ Видео весит больше 50 МБ (максимум для Telegram бота)."
        elif "Private video" in err_str or "login" in err_str.lower():
            msg = "❌ Этот аккаунт или видео закрыты настройками приватности."
        else:
            msg = f"❌ Не удалось скачать. Ошибка: {error[:100] if error else 'неизвестно'}"
        await status_msg.edit_text(msg)

def extract_valid_url(text: str) -> str | None:
    """Извлекает и очищает первую поддерживаемую ссылку из текста."""
    if not text:
        return None
        
    links = re.findall(r"https?://[^\s]+", text)
    for link in links:
        # Убираем случайные точки, скобки и запятые на конце ссылок
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

# Веб-сервер для поддержания жизни на Render
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
