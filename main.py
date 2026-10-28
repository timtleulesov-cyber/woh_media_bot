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

URL_REGEX = r"(https?://(?:www\.)?(?:tiktok\.com|youtube\.com|youtu\.be|instagram\.com|vk\.com/clip)[\w\-\._~:/?#[\]@!\$&'\(\)\*\+,;=.]+)"

def download_media(url: str) -> str | None:
    out_template = "downloads/%(id)s.%(ext)s"
    ydl_opts = {
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_template,
        "max_filesize": 49 * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
        except Exception as e:
            logging.error(f"Ошибка загрузки: {e}")
            return None

async def process_url(message: Message, url: str):
    loop = asyncio.get_running_loop()
    file_path = await loop.run_in_executor(None, download_media, url)

    if file_path and os.path.exists(file_path):
        try:
            video = FSInputFile(file_path)
            await message.reply_video(video=video)
        except Exception as e:
            logging.error(f"Ошибка отправки: {e}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

@dp.channel_post(F.text)
async def handle_channel_post(message: Message):
    match = re.search(URL_REGEX, message.text)
    if match:
        await process_url(message, match.group(0))

@dp.message(F.text)
async def handle_chat_message(message: Message):
    match = re.search(URL_REGEX, message.text)
    if match:
        await process_url(message, match.group(0))

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
    logging.info(f"Веб-сервер запущен на порту {port}")

async def main():
    os.makedirs("downloads", exist_ok=True)
    await start_web_server()
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
