FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Python печатает логи сразу, а не пачками — удобнее смотреть в Render
ENV PYTHONUNBUFFERED=1

# TikTok/X регулярно ломают yt-dlp, поэтому при каждом старте подтягиваем свежую версию
CMD ["sh", "-c", "pip install --no-cache-dir -U yt-dlp || true; python main.py"]
