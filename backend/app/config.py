"""Ortam (.env) ayarları: sistem düzeyi anahtarlar. Kullanıcıya ait ayarlar ve bağlantılar veritabanındadır."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Zorunlu sistem ayarı eksik: uygulama anlaşılır bir hatayla açılmaz."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = ROOT / "data"
    storage_dir: Path = ROOT / "storage"
    models_file: Path = ROOT / "config" / "models.yaml"
    frontend_dist: Path = ROOT / "frontend" / "dist"

    # --- zorunlu sistem anahtarları (make init üretir)
    app_secret: str = ""          # oturum ve bağlantı imzaları
    encryption_key: str = ""      # kullanıcı servis anahtarlarını şifreler (Fernet)

    # --- isteğe bağlı
    database_url: str = ""        # boşsa data/studyo.db (SQLite); postgresql+psycopg://...
    public_base_url: str = ""     # https://studyo.example.com : çerez güvenliği, CORS, e-posta bağlantıları, webhook
    allowed_hosts: str = ""       # virgüllü ek Host adları
    cookie_secure: bool | None = None   # boşsa public_base_url https ise true

    s3_endpoint: str = ""         # AWS için boş; R2/MinIO için uç nokta adresi
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "auto"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True

    # --- davranış
    demo_mode: bool = False       # sahte yönetmen (Anthropic anahtarı olmayan kullanıcılar için); yönetici panelinden de açılır
    mock_delay: float = 0.4       # mock işlerin yapay bekleme süresi (saniye)
    seed_demo: bool = True        # demo kullanıcı ve örnek bölüm (yalnız yerel/deneme kurulumları için)
    demo_email: str = "demo@studyo.local"
    demo_password: str = ""       # boşsa üretilir ve data/demo_sifre.txt dosyasına yazılır
    session_days: int = 14

    @property
    def db_url(self) -> str:
        if self.database_url:
            url = self.database_url
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            if url.startswith("postgresql://"):
                url = "postgresql+psycopg://" + url[len("postgresql://"):]
            return url
        return f"sqlite:///{(self.data_dir / 'studyo.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.public_base_url.lower().startswith("https://")

    @property
    def hosts(self) -> list[str]:
        out = ["localhost", "127.0.0.1", "[::1]", "test", "testserver"]
        if self.public_base_url:
            h = urlparse(self.public_base_url).hostname
            if h:
                out.append(h)
        out += [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        return out

    @property
    def s3_enabled(self) -> bool:
        return bool(self.s3_bucket and self.s3_access_key and self.s3_secret_key)

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def require_secrets(self) -> None:
        missing = [n for n, v in (("APP_SECRET", self.app_secret), ("ENCRYPTION_KEY", self.encryption_key)) if not v]
        if missing:
            raise ConfigError(
                "Zorunlu sistem anahtarları eksik: " + ", ".join(missing) + ".\n"
                "Üretmek için proje kökünde `make init` çalıştırın (Windows: baslat.cmd bunu kendisi yapar) ya da\n"
                "`cd backend && uv run python -m app.init_env` komutunu kullanın; değerler .env dosyasına yazılır.\n"
                "ENCRYPTION_KEY kaybolursa kayıtlı servis anahtarları çözülemez; yedeğini alın.")
        if len(self.app_secret) < 24:
            raise ConfigError("APP_SECRET en az 24 karakter olmalı (make init ile yeniden üretin).")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.storage_dir.mkdir(parents=True, exist_ok=True)
    return s


@lru_cache
def get_models() -> dict:
    """config/models.yaml: model kimlikleri, uç noktalar ve fiyatlar burada, kodda değil."""
    with open(get_settings().models_file, encoding="utf-8") as f:
        return yaml.safe_load(f)
