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

# Список поддерживаемых сервисов (ловит любые поддомены: vm.tiktok, vt.tiktok, m.youtube и т.д.)
SUPPORTED_DOMAINS = ["tiktok.com", "youtube.com", "youtu.be", "instagram.com", "vk.com"]

def download_media(url: str) -> tuple[str | None, str | None]:
    """Скачивание медиа. Возвращает (путь_к_файлу, текст_ошибки)."""
    out_template = "downloads/%(id)s.%(ext)s"
    ydl_opts = {
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_template,
        "max_filesize": 49 * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Обход блокировки дата-центров YouTube (маскируемся под Android/iOS клиенты)
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
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
    status_msg = await message.reply("⏳ Начинаю скачивание...")
    
    loop = asyncio.get_running_loop()
    file_path, error = await loop.run_in_executor(None, download_media, url)

    if file_path and os.path.exists(file_path):
        try:
            video = FSInputFile(file_path)
            await message.reply_video(video=video)
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"⚠️ Ошибка при отправке в Telegram: {e}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
    else:
        # Выводим понятную ошибку прямо в чат
        if "Sign in to confirm" in str(error):
            msg = "❌ YouTube заблокировал доступ к этому видео без авторизации."
        elif "File is larger than" in str(error) or "max_filesize" in str(error):
            msg = "❌ Видео весит больше 50 МБ (лимит Telegram бота)."
        else:
            msg = f"❌ Не удалось скачать видео. Ошибка: {error[:120] if error else 'неизвестно'}"
        await status_msg.edit_text(msg)

def extract_valid_url(text: str) -> str | None:
    # Ищем любую ссылку в сообщении
    links = re.findall(r"https?://\S+", text or "")
    for link in links:
        if any(domain in link.lower() for domain in SUPPORTED_DOMAINS):
            return link
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

# Фоновый веб-сервер для поддержания активности на Render
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
