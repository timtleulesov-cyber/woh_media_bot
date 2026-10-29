import os
import re
import json
import html
import glob
import random
import shutil
import asyncio
import logging
import tempfile
import subprocess
import urllib.request
from urllib.parse import urlparse
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message, FSInputFile
import yt_dlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

# Большие ролики грузятся в телегу долго — дефолтных 60 сек не хватает
bot = Bot(token=TOKEN, session=AiohttpSession(timeout=300))
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
# Лимит Bot API на отправку файла — 50 МБ, берём с запасом
MAX_FILESIZE = 49 * 1024 * 1024
# На бесплатном Render 512 МБ RAM — больше 2 загрузок одновременно не тянем
download_semaphore = asyncio.Semaphore(int(os.getenv("MAX_PARALLEL_DOWNLOADS", 2)))

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Расширения видеофайлов для Discord
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".m4v")

TIKTOK_DOMAINS = ("tiktok.com", "douyin.com")
TWITTER_DOMAINS = ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com")
DISCORD_DOMAINS = ("cdn.discordapp.com", "media.discordapp.net", "cdn.discord.com", "discord.com")
# Прямые ссылки на файлы Discord — качаем их сами, без yt-dlp
DISCORD_CDN_DOMAINS = ("cdn.discordapp.com", "media.discordapp.net", "cdn.discord.com")
INSTAGRAM_DOMAINS = ("instagram.com", "instagr.am", "ddinstagram.com", "kkinstagram.com")
INSTAGRAM_PATHS = ("/reel/", "/reels/", "/p/", "/tv/", "/share/", "/stories/")

# Инстаграм с серверов Render часто требует логин. Если положить в переменную
# INSTAGRAM_COOKIES содержимое cookies.txt (формат Netscape) от левого аккаунта — будет качать стабильно
INSTAGRAM_COOKIES_FILE = None
if os.getenv("INSTAGRAM_COOKIES"):
    INSTAGRAM_COOKIES_FILE = os.path.join(tempfile.gettempdir(), "instagram_cookies.txt")
    with open(INSTAGRAM_COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(os.environ["INSTAGRAM_COOKIES"].replace("\\n", "\n"))

# Случайные фразы на случай неудач
FAIL_MESSAGES = [
    "чет не получилось...",
    "сори, не в этот раз...",
    "не вышло скачать, сорян :(",
    "чет пошло не так, не смог достать ролик...",
    "увы, в этот раз не судьба..."
]
TOO_BIG_MESSAGE = "ролик слишком жирный (больше 50 МБ), телега не пустит сори"
INSTAGRAM_LOGIN_MESSAGE = "инста не отдаёт ролик без логина, сори :("

# --- Перлы по нику ---

PEARLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pearls.json")
# Шанс, что к ролику прилетит перл (1.0 — всегда)
PEARL_CHANCE = float(os.getenv("PEARL_CHANCE", 1.0))


def load_pearls() -> dict:
    try:
        with open(PEARLS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # Ники храним в нижнем регистре и без @, чтобы не путаться
        data["users"] = {k.lower().lstrip("@"): v for k, v in data.get("users", {}).items()}
        return data
    except Exception as e:
        logging.error(f"Не смог прочитать {PEARLS_FILE}: {e}")
        return {"common": [], "users": {}}


class PearlDeck:
    """Колода перлов: тасуем и выдаём по одному, пока не кончится весь список, потом тасуем заново."""

    def __init__(self, pearls: list[str]):
        self.pearls = list(dict.fromkeys(pearls))
        self.deck: list[str] = []
        self.last: str | None = None

    def draw(self) -> str | None:
        if not self.pearls:
            return None
        if not self.deck:
            self.deck = random.sample(self.pearls, len(self.pearls))
            # На стыке колод не даём выпасть тому же перлу второй раз подряд
            if len(self.deck) > 1 and self.deck[-1] == self.last:
                self.deck[0], self.deck[-1] = self.deck[-1], self.deck[0]
        self.last = self.deck.pop()
        return self.last


PEARLS = load_pearls()
common_deck = PearlDeck(PEARLS["common"])
# У каждого, для кого есть личные перлы, своя колода
personal_decks = {key: PearlDeck(pearls) for key, pearls in PEARLS["users"].items()}


def pick_pearl(message: Message) -> str | None:
    user = message.from_user
    if not user or random.random() > PEARL_CHANCE:
        return None

    name = user.first_name or user.username or "аноним"
    # Можно задавать перлы и по нику, и по числовому id (если ника нет)
    personal = personal_decks.get((user.username or "").lower()) or personal_decks.get(str(user.id))

    # Личные перлы выпадают чаще, но общие тоже иногда
    if personal and random.random() < 0.5:
        template = personal.draw()
    else:
        template = common_deck.draw() or (personal.draw() if personal else None)
    if not template:
        return None

    return html.escape(template).replace("{name}", f"<b>{html.escape(name)}</b>")


# --- Поиск ссылок ---

def host_matches(host: str, domains: tuple[str, ...]) -> bool:
    """Точное совпадение домена или его поддомена (чтобы netflix.com не считался x.com)."""
    return any(host == d or host.endswith("." + d) for d in domains)


def extract_valid_url(text: str) -> str | None:
    """Ищет только ссылки на TikTok, Twitter/X, Instagram и ролики из Discord."""
    if not text:
        return None

    links = re.findall(r"https?://[^\s]+", text)
    for link in links:
        clean_link = link.rstrip(".,!?;:)\"'>»")
        try:
            parsed = urlparse(clean_link)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()

        # 1. TikTok
        if host_matches(host, TIKTOK_DOMAINS):
            return clean_link

        # 2. Twitter / X — только ссылки на посты
        if host_matches(host, TWITTER_DOMAINS) and "/status/" in path:
            return clean_link

        # 3. Instagram — рилсы, посты, сторис
        if host_matches(host, INSTAGRAM_DOMAINS) and any(p in path + "/" for p in INSTAGRAM_PATHS):
            return clean_link

        # 4. Discord (видеовложения и клипы)
        if host_matches(host, DISCORD_DOMAINS):
            if path.endswith(VIDEO_EXTENSIONS) or "/clips/" in path:
                return clean_link

    return None


# --- Скачивание ---

class TooBig(Exception):
    pass


def http_download(url: str, file_path: str, timeout: int = 30):
    """Качает файл кусками, а не целиком в память, и бросает, если он не влезет в телегу."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if int(resp.headers.get("Content-Length") or 0) > MAX_FILESIZE:
            raise TooBig()
        with open(file_path, "wb") as f:
            size = 0
            while chunk := resp.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILESIZE:
                    raise TooBig()
                f.write(chunk)


def probe_video(file_path: str) -> dict:
    """Достаёт размеры и длительность через ffprobe, если источник их не сообщил."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", file_path],
            capture_output=True, text=True, timeout=20,
        ).stdout
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        duration = data.get("format", {}).get("duration")
        return {
            "width": stream.get("width"),
            "height": stream.get("height"),
            "duration": float(duration) if duration else None,
        }
    except Exception as e:
        logging.warning(f"ffprobe не сработал: {e}")
        return {}


def discord_download(url: str, work_dir: str) -> dict | None:
    """Прямое скачивание вложения из Discord CDN (ссылка должна быть с параметрами ex/is/hm)."""
    name = os.path.basename(urlparse(url).path) or "video.mp4"
    file_path = os.path.join(work_dir, name)
    try:
        http_download(url, file_path)
        return {"path": file_path}
    except TooBig:
        return {"error": "too_big"}
    except Exception as e:
        logging.warning(f"Не смог скачать из Discord {url}: {e}")
        return None

def twitter_fallback_download(url: str, work_dir: str) -> dict | None:
    """Резервное скачивание из Twitter/X через API в обход ограничений."""
    match = re.search(r"/([^/?]+)/status/(\d+)", url)
    if not match:
        return None

    user, tweet_id = match.group(1), match.group(2)
    api_url = f"https://api.fxtwitter.com/{user}/status/{tweet_id}"
    headers = {"User-Agent": USER_AGENT}

    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            tweet = json.loads(response.read().decode()).get("tweet", {})

        # Проверяем видео в посте или цитате
        videos = tweet.get("media", {}).get("videos", [])
        if not videos and "quote" in tweet:
            videos = tweet["quote"].get("media", {}).get("videos", [])
        if not videos or "url" not in videos[0]:
            return None

        video = videos[0]
        file_path = os.path.join(work_dir, f"tw_{tweet_id}.mp4")
        http_download(video["url"], file_path)

        return {
            "path": file_path,
            "width": video.get("width"),
            "height": video.get("height"),
            "duration": video.get("duration"),
        }
    except TooBig:
        return {"error": "too_big"}
    except Exception as e:
        logging.error(f"Ошибка Twitter fallback: {e}")
    return None


def download_media(url: str, work_dir: str) -> dict:
    """Скачивание медиа. Возвращает {path, width, height, duration} или {error}."""
    host = (urlparse(url).hostname or "").lower()
    is_twitter = host_matches(host, TWITTER_DOMAINS)
    is_instagram = host_matches(host, INSTAGRAM_DOMAINS)

    if host_matches(host, DISCORD_CDN_DOMAINS):
        result = discord_download(url, work_dir)
        if result:
            return result

    ydl_opts = {
        # H.264 в приоритете: HEVC с тиктока у части клиентов телеги показывается чёрным экраном
        "format": "best[ext=mp4][vcodec~='^(avc|h264)']/best[ext=mp4]/best",
        "outtmpl": os.path.join(work_dir, "%(id)s.%(ext)s"),
        "max_filesize": MAX_FILESIZE,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
        "http_headers": {"User-Agent": USER_AGENT},
    }
    if is_instagram:
        # Зеркала инсты yt-dlp не знает — переписываем на оригинальный домен
        url = re.sub(r"https?://(?:www\.)?(?:dd|kk)instagram\.com", "https://www.instagram.com", url)
        if INSTAGRAM_COOKIES_FILE:
            ydl_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE

    error = None
    # Сначала пробуем через yt-dlp
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [f for f in glob.glob(os.path.join(work_dir, "*")) if not f.endswith(".part")]
        if files:
            return {
                "path": files[0],
                "width": info.get("width"),
                "height": info.get("height"),
                "duration": info.get("duration"),
            }
        # yt-dlp молча пропускает файлы больше max_filesize, без исключения
        size = info.get("filesize") or info.get("filesize_approx") or 0
        error = "too_big" if size > MAX_FILESIZE else "yt-dlp не скачал файл"
    except Exception as e:
        error = str(e)
        logging.warning(f"yt-dlp не справился с {url}: {error}")

    if is_instagram and error and re.search(r"login|rate-limit|cookies", error, re.I):
        return {"error": "instagram_login"}

    # Если это Твиттер, пробуем резервный метод
    if is_twitter and error != "too_big":
        fallback = twitter_fallback_download(url, work_dir)
        if fallback:
            return fallback

    return {"error": error}


async def process_url(message: Message, url: str):
    status_msg = await message.reply("⏳ Загружаю ролик...")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    # Своя папка на каждую загрузку — параллельные запросы не мешают друг другу, мусор не копится
    work_dir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    try:
        async with download_semaphore:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, download_media, url, work_dir)

        if "path" not in result:
            if result.get("error") == "too_big":
                await status_msg.edit_text(TOO_BIG_MESSAGE)
            elif result.get("error") == "instagram_login":
                logging.error(f"Инстаграм требует логин для {url} — задай INSTAGRAM_COOKIES")
                await status_msg.edit_text(INSTAGRAM_LOGIN_MESSAGE)
            else:
                logging.error(f"Не скачалось {url}: {result.get('error')}")
                await status_msg.edit_text(random.choice(FAIL_MESSAGES))
            return

        if os.path.getsize(result["path"]) > MAX_FILESIZE:
            await status_msg.edit_text(TOO_BIG_MESSAGE)
            return

        if not all(result.get(k) for k in ("width", "height", "duration")):
            result.update({k: v for k, v in probe_video(result["path"]).items() if v})

        try:
            await message.reply_video(
                video=FSInputFile(result["path"]),
                caption=pick_pearl(message),
                parse_mode="HTML",
                # Без размеров телега рисует ролик квадратом и без превью
                width=int(result["width"]) if result.get("width") else None,
                height=int(result["height"]) if result.get("height") else None,
                duration=int(result["duration"]) if result.get("duration") else None,
                supports_streaming=True,
            )
        except Exception as e:
            logging.error(f"Не смог отправить видео {url}: {e}")
            await status_msg.edit_text(random.choice(FAIL_MESSAGES))
            return

        try:
            await status_msg.delete()
        except Exception:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


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
    # Чистим остатки от прошлого запуска
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    await start_web_server()
    logging.info(f"yt-dlp {yt_dlp.version.__version__}, перлов: {len(PEARLS.get('common', []))} общих, {len(PEARLS['users'])} юзеров")
    logging.info("Бот успешно запущен...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
