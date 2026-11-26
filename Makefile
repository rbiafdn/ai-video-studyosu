# AI Video Prodüksiyon Stüdyosu (çok kullanıcılı)
# Gereken: uv (Python 3.11+ kendisi kurar), Node 20+, ffmpeg/ffprobe
.PHONY: init dev install backend frontend build start test docker docker-https

install:
	cd backend && uv sync
	cd frontend && npm install

# Zorunlu sistem anahtarlarını (APP_SECRET, ENCRYPTION_KEY) üretip .env dosyasına yazar. Var olanlara dokunmaz.
init:
	cd backend && uv sync && uv run python -m app.init_env

# Geliştirme: backend (otomatik yeniden yükleme) + Vite arayüzü -> http://127.0.0.1:5273
dev: install init
	$(MAKE) -j2 backend frontend

backend:
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload --reload-dir app

frontend:
	cd frontend && npm run dev

build:
	cd frontend && npm install && npm run build

# Tek süreç: backend derlenmiş arayüzü de sunar -> http://127.0.0.1:8765
start: build init
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8765

test:
	cd backend && uv run pytest -q

# Yerel Docker: http://127.0.0.1:8765  (önce make init)
docker: init
	docker compose up --build

# Sunucu: Caddy ile otomatik HTTPS + PostgreSQL. .env içinde PUBLIC_BASE_URL, SITE_DOMAIN, POSTGRES_PASSWORD gerekir.
docker-https: init
	docker compose --profile https --profile postgres up --build -d
