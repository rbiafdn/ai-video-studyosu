from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, crypto, seed as seed_mod, storage, tenancy
from .api import router
from .api_auth import router as auth_router
from .auth import current_user
from .config import get_settings
from .db import get_db, migrate, session_scope
from .events import bus
from .models import Asset, User
from .worker import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx INFO düzeyinde istek adreslerini yazar; anahtarlar başlıkta olsa da günlükleri sade tut
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("alembic").setLevel(logging.WARNING)
log = logging.getLogger("studyo")


class SecretFilter(logging.Filter):
    """Günlüklere yanlışlıkla düşebilecek anahtar biçimlerini maskeler (son savunma hattı)."""
    PAT = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{6,}|sk_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9+/=_\-]{8,}|"
                     r"(?i:bearer)\s+[A-Za-z0-9._\-]{12,}|(?i:(?:x-api-key|xi-api-key|api[_-]?key|authorization|password|token))"
                     r"[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{8,})")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = self.PAT.sub("***", msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(SecretFilter())


def ensure_demo_user() -> None:
    """Demo kullanıcı + örnek bölüm (SEED_DEMO=false ile kapatılır). Şifre üretilirse data/demo_sifre.txt'ye yazılır."""
    st = get_settings()
    with session_scope() as s:
        u = s.scalars(select(User).where(User.is_demo.is_(True))).first()
        if u is None:
            if s.scalars(select(User).where(User.email == st.demo_email)).first():
                return
            password = st.demo_password or crypto.new_token()[:16]
            u = auth.create_user(s, st.demo_email, password, verified=True, demo=True)
            u.setup_done = True
            s.flush()
            if not st.demo_password:
                f = st.data_dir / "demo_sifre.txt"
                f.write_text(f"Demo kullanıcı\nE-posta: {st.demo_email}\nŞifre: {password}\n", encoding="utf-8")
                log.info("Demo kullanıcı oluşturuldu; giriş bilgileri %s dosyasında", f)
        with tenancy.as_user(u.id):
            seed_mod.seed(s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    st = get_settings()
    st.require_secrets()
    migrate()
    bus.bind(asyncio.get_running_loop())
    if st.seed_demo:
        ensure_demo_user()
    await worker.start()
    yield
    await worker.stop()


CSP = ("default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app() -> FastAPI:
    st = get_settings()
    public = bool(st.public_base_url)
    app = FastAPI(title="AI Video Prodüksiyon Stüdyosu", lifespan=lifespan,
                  docs_url=None if public else "/docs", redoc_url=None, openapi_url=None if public else "/openapi.json")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # genel hız sınırı (IP başına); giriş, kayıt, sınama gibi uçların ayrıca kendi sınırı var
        path = request.url.path
        if path.startswith("/api/") and path != "/api/events":
            if not auth.limiter.hit(f"api:{auth.client_ip(request)}", 1200, 60):
                return JSONResponse({"detail": "Çok fazla istek. Biraz bekleyin."}, status_code=429, headers={"Retry-After": "30"})
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not path.startswith(("/docs", "/openapi")):
            h.setdefault("Content-Security-Policy", CSP)
        if path.startswith(("/api/", "/media/")):
            h.setdefault("Cache-Control", "private, no-store" if path.startswith("/api/") else "private, max-age=3600")
        if st.secure_cookies:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    # CORS: yalnızca kendi alan adı (ve yerel geliştirme sunucusu)
    origins = ["http://localhost:5173", "http://127.0.0.1:5173"] if not public else []
    if public:
        u = urlparse(st.public_base_url)
        origins.append(f"{u.scheme}://{u.netloc}")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["content-type", auth.CSRF_HEADER])
    # DNS rebinding'e karşı Host başlığı da sınırlanır
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=st.hosts)
    app.include_router(router)
    app.include_router(auth_router)

    @app.get("/media/{path:path}", include_in_schema=False)
    def media(path: str, s: Session = Depends(get_db), user: User = Depends(current_user)):
        """Medya herkese açık değildir: yalnızca sahibi, oturumuyla indirebilir. Başkasının dosyası 404 döner."""
        rel = path.replace("\\", "/")
        if not rel or rel.startswith("/") or any(part in ("..", "", ".") for part in rel.split("/")):
            raise HTTPException(404, "Dosya bulunamadı")
        own = rel.startswith(f"u{user.id}/")
        if not own:  # eski tek kullanıcılı düzenden kalan dosyalar: sahiplik varlık kaydından
            own = bool(s.scalars(select(Asset.id).where(Asset.path == rel, Asset.user_id == user.id)).first())
        if not own:
            raise HTTPException(404, "Dosya bulunamadı")
        try:
            f = storage.abs_path(rel)
        except ValueError:
            raise HTTPException(404, "Dosya bulunamadı")
        if not f.is_file():  # (S3 etkinse abs_path dosyayı kovadan geri getirmeyi zaten denedi)
            raise HTTPException(404, "Dosya bulunamadı")
        # yalnızca medya ve düz veri türleri tarayıcıda açılır; diğer her şey (html, svg, js...) indirme olarak verilir
        kind = mimetypes.guess_type(f.name)[0] or ""
        if f.suffix.lower() in (".srt", ".txt", ".csv", ".json"):
            kind = "text/plain; charset=utf-8"
        elif not kind.startswith(("audio/", "video/")) and kind not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            return FileResponse(f, media_type="application/octet-stream", filename=f.name)
        return FileResponse(f, media_type=kind)

    dist = st.frontend_dist
    if (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "media/")):
                raise HTTPException(404)
            f = (dist / path).resolve()
            if path and f.is_file() and dist.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")
    return app


app = create_app()
