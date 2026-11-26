"""Sağlayıcı katmanının ortak arayüzü.

Her adaptör üç çağrıyı uygular:
    submit(girdi) -> iş kimliği      poll(iş kimliği) -> durum      fetch(iş kimliği, hedef) -> dosya

Girdi sözlüğündeki dosyalar storage köküne göre göreli yoldur (image_path, audio_path, video_path,
ref_image_paths). Geçici hatalar TransientError, kalıcı hatalar PermanentError olarak yükseltilir;
yeniden deneme (üstel bekleme) işçi tarafında yapılır.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx


class ProviderError(Exception):
    pass


class TransientError(ProviderError):
    """Hız sınırı, zaman aşımı, 5xx: üstel bekleme ile yeniden denenir."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(ProviderError):
    """Yeniden denemenin anlamı olmayan hata: iş 'hata' olur."""


class AuthFailure(PermanentError):
    """Anahtar geçersiz ya da kredisi bitmiş: iş 'bağlantı bekliyor' durumuna alınır; kullanıcı bağlantıyı
    yenileyince kaldığı yerden sürer. kind: 'invalid' | 'credit'."""

    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Submitted:
    external_id: str
    manual: bool = False
    # sağlayıcının bildirdiği gerçek tüketim (varsa tahminin yerine yazılır)
    cost_units: float | None = None


@dataclass
class Polled:
    # running | succeeded | failed | lost (iş kimliği artık geçersiz: yeniden gönderilir)
    status: str
    progress: float = 0.0
    error: str = ""


@dataclass
class Cost:
    usd: float = 0.0
    units: float = 0.0
    unit: str = ""


@dataclass
class Ctx:
    """Adaptöre verilen çalışma bağlamı. Testler transport ve secrets'ı değiştirir."""
    storage_dir: Path
    tmp_dir: Path
    config: dict                      # models.yaml'daki sağlayıcı bölümü
    model: str                        # seçili model kimliği
    secrets: Callable[[str], str] = lambda k: ""
    job_id: int = 0
    kind: str = ""                    # iş türü (voice, scene_image, ...)
    mock_delay: float = 0.0
    transport: httpx.AsyncBaseTransport | None = None
    extra: dict = field(default_factory=dict)

    def path(self, rel: str) -> Path:
        return self.storage_dir / rel

    def model_cfg(self) -> dict:
        for m in self.config.get("models", []):
            if m["id"] == self.model:
                return m
        return {}


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", text)


class Provider:
    key: str = ""
    service: str = ""        # anahtar grubu: elevenlabs | kling | heygen
    kind: str = ""           # audio | image | video
    label: str = ""
    poll_interval: float = 5.0
    timeout: float = 3600.0  # bir işin azami bekleme süresi (sn)

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.inp: dict = {}          # işçi her çağrıdan önce işin girdisini koyar
        self.result_meta: dict = {}  # fetch sonrası varlığa eklenecek bilgiler (örn. source_url)

    # --- uygulanacak üç çağrı -------------------------------------------------
    async def submit(self, inp: dict) -> Submitted:
        raise NotImplementedError

    async def poll(self, external_id: str) -> Polled:
        raise NotImplementedError

    async def fetch(self, external_id: str, dest: Path) -> Path:
        """Sonucu dest'e (uzantısı değişebilir) yazar, gerçek yolu döndürür."""
        raise NotImplementedError

    @classmethod
    def estimate_cost(cls, inp: dict, config: dict, model: str) -> Cost:
        return Cost()

    # --- http yardımcıları ----------------------------------------------------
    def _secret_values(self) -> list[str]:
        return []

    def client(self, **kw) -> httpx.AsyncClient:
        kw.setdefault("timeout", httpx.Timeout(120.0, connect=20.0))
        if self.ctx.transport is not None:
            kw["transport"] = self.ctx.transport
        return httpx.AsyncClient(**kw)

    def error_message(self, r: httpx.Response) -> str:
        try:
            body: Any = r.json()
        except Exception:
            body = r.text[:300]
        return f"HTTP {r.status_code}: {str(body)[:400]}"

    def classify(self, r: httpx.Response) -> None:
        """Yanıtı hata sınıfına çevirir. Servise özel kurallar için ezilir."""
        if r.status_code < 400:
            return
        msg = redact(f"{self.label}: {self.error_message(r)}", self._secret_values())
        if r.status_code in (408, 409, 425, 429) or r.status_code >= 500:
            ra = r.headers.get("retry-after")
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = None
            raise TransientError(msg, retry_after)
        if r.status_code in (401, 403):
            raise AuthFailure(msg, "invalid")
        if r.status_code == 402:
            raise AuthFailure(msg, "credit")
        raise PermanentError(msg)

    async def request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            async with self.client() as c:
                r = await c.request(method, url, **kw)
        except httpx.TimeoutException as e:
            raise TransientError(f"{self.label}: zaman aşımı ({type(e).__name__})") from e
        except httpx.TransportError as e:
            raise TransientError(f"{self.label}: ağ hatası ({type(e).__name__})") from e
        self.classify(r)
        return r

    async def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self.client(follow_redirects=True) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code >= 500 or r.status_code == 429:
                        raise TransientError(f"{self.label}: indirme HTTP {r.status_code}")
                    if r.status_code >= 400:
                        raise PermanentError(f"{self.label}: indirme HTTP {r.status_code}")
                    with open(dest, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
        except httpx.TimeoutException as e:
            raise TransientError(f"{self.label}: indirme zaman aşımı") from e
        except httpx.TransportError as e:
            raise TransientError(f"{self.label}: indirme ağ hatası") from e
        return dest
