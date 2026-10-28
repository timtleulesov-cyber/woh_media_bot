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

# Расширения видеофайлов для Discord
VIDEO_EXTENSIONS = [".mp4", ".mov", ".webm", ".mkv", ".m4v"]

def extract_valid_url(text: str) -> str | None:
    """Ищет только ссылки на TikTok и видеофайлы Discord."""
    if not text:
        return None
        
    links = re.findall(r"https?://[^\s]+", text)
    for link in links:
        clean_link = link.rstrip(".,!?;:)\"'>»")
        lower_link = clean_link.lower()

        # 1. Проверяем TikTok (любые поддомены и формат)
        if "tiktok.com" in lower_link or "douyin.com" in lower_link:
            return clean_link

        # 2. Проверяем Discord (CDN, вложения и клипы)
        is_discord = any(d in lower_link for d in [
            "cdn.discordapp.com",
            "media.discordapp.net",
            "cdn.discord.com",
            "discord.com"
        ])
        if is_discord:
            # Отрезаем параметры ?ex=... для проверки расширения
            clean_path = lower_link.split("?")[0]
            if any(clean_path.endswith(ext) for ext in VIDEO_EXTENSIONS) or "/clips/" in lower_link:
                return clean_link

    return None

def download_media(url: str) -> tuple[str | None, str | None]:
    """Скачивание ролика цельным потоком (сохраняет оригинальный звук)."""
    out_template = "downloads/%(id)s.%(ext)s"
    
    ydl_opts = {
        # Берем готовое mp4 видео с оригинальной дорожкой автора
        "format": "best[ext=mp4]/best",
        "outtmpl": out_template,
        "max_filesize": 49 * 1024 * 1024,  # лимит Telegram 50 МБ
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
            await status_msg.edit_text(f"⚠️ Ошибка отправки в Telegram: {e}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
    else:
        err_str = str(error)
        if "File is larger than" in err_str or "max_filesize" in err_str:
            msg = "❌ Видео весит больше 50 МБ (лимит Telegram бота)."
        else:
            msg = f"❌ Не удалось скачать. Ошибка: {error[:100] if error else 'файл недоступен'}"
        await status_msg.edit_text(msg)

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

# Веб-сервер для поддержания активности на Render
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
