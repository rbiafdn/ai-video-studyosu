"""Testler geçici bir veri/depo klasöründe, deneme (mock) modunda ve gerçek API anahtarı olmadan çalışır."""
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="studyo-test-"))
os.environ.update(
    DATA_DIR=str(_TMP / "data"), STORAGE_DIR=str(_TMP / "storage"), MOCK_DELAY="0.05", SEED_DEMO="false", DEMO_MODE="true",
    APP_SECRET="test-app-secret-0123456789abcdef", ENCRYPTION_KEY=Fernet.generate_key().decode(),
    DATABASE_URL=os.environ.get("TEST_DATABASE_URL", ""), PUBLIC_BASE_URL="", ALLOWED_HOSTS="", COOKIE_SECURE="",
    S3_BUCKET="", S3_ACCESS_KEY="", S3_SECRET_KEY="", SMTP_HOST="", SMTP_FROM="",
)
os.environ.pop("COOKIE_SECURE")

import httpx  # noqa: E402
import pytest  # noqa: E402

import helpers  # noqa: E402
from app import auth, connections, media, settings_store as store, storage, tenancy  # noqa: E402
from app.config import get_models, get_settings  # noqa: E402
from app.db import migrate, session_scope  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.base import Ctx  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    migrate()
    store.sys_put("retry_base_seconds", 0.01)
    store.sys_put("demo_mode", True)  # Anthropic anahtarı olmayan kullanıcıda sahte yönetmen
    with session_scope() as s:
        # ilk kayıt olan kullanıcı (davetsiz) yönetici olur
        u = auth.register(s, helpers.EMAIL, helpers.PASSWORD, None, "test")
        assert u.role == "admin"
        u.setup_done = True
        helpers.UID = u.id
    yield


@pytest.fixture(autouse=True)
def _reset():
    """Her test varsayılan kullanıcı bağlamında, anahtarsız ve sahte HTTP katmansız başlar."""
    token = tenancy.USER_ID.set(helpers.UID)
    auth.limiter.reset()
    yield
    tenancy.USER_ID.reset(token)
    registry.transport_override = None
    connections.transport_override = None
    connections.anthropic_http_client = None
    storage.s3_override = None
    helpers.clear_connections()
    store.sys_put("mock_mode", False)
    store.sys_put("demo_mode", True)
    store.sys_put("registration_mode", "invite")


@pytest.fixture
async def client():
    """Uygulama (lifespan ile) + varsayılan kullanıcıyla oturum açmış istemci."""
    from app.main import app
    async with app.router.lifespan_context(app):
        async with helpers.raw_client(app) as c:
            await helpers.login(c)
            yield c


@pytest.fixture(scope="session")
def samples() -> dict[str, Path]:
    """Sahte sağlayıcı yanıtlarında kullanılacak gerçek (ffmpeg üretimi) küçük dosyalar."""
    d = _TMP / "samples"
    d.mkdir(parents=True, exist_ok=True)
    media.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=220:duration=2", "-c:a", "libmp3lame", d / "a.mp3"])
    media.ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=320x180", "-frames:v", "1", d / "i.png"])
    media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=3", "-f", "lavfi", "-i", "sine=frequency=330:duration=3",
                  "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", d / "v.mp4"])
    return {"audio": d / "a.mp3", "image": d / "i.png", "video": d / "v.mp4"}


SECRETS = {"elevenlabs_api_key": "el-SECRET-123456", "kling_api_key": "kl-SECRET-123456", "heygen_api_key": "hg-SECRET-123456"}


@pytest.fixture
def make_ctx():
    def _make(provider: str, handler, model: str | None = None, **kw) -> Ctx:
        cfg = get_models()["providers"][provider]
        st = get_settings()
        (st.storage_dir / "_tmp").mkdir(parents=True, exist_ok=True)
        return Ctx(storage_dir=st.storage_dir, tmp_dir=st.storage_dir / "_tmp", config=cfg, model=model or cfg["default"],
                   secrets=lambda k: SECRETS.get(k, ""), transport=httpx.MockTransport(handler), **kw)
    return _make
