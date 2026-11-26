# 1) arayüzü derle
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# 2) backend + ffmpeg
FROM python:3.12-slim
# fonts-noto-*: altyazı gömmede Arapça, İbranice, Hintçe ve CJK yazı tipleri
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --no-dev --frozen
COPY backend/ ./
COPY config/ /app/config/
COPY prompts/ /app/prompts/
COPY examples/ /app/examples/
COPY --from=web /web/dist /app/frontend/dist
EXPOSE 8765
# Kapsayıcı içinde 0.0.0.0 dinlenir; dışarıya yalnızca 127.0.0.1 ya da HTTPS vekili (Caddy) açılır.
# --proxy-headers: gerçek istemci IP'si (hız sınırı ve denetim günlüğü için) vekilin başlıklarından alınır.
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*"]
