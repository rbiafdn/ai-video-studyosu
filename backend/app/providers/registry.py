"""Sağlayıcı kaydı ve çalışma modu seçimi (real | mock | manual).

Yeni sağlayıcı eklemek için: base.Provider'dan türeyen sınıfı yazın, CLASSES'a ekleyin,
config/models.yaml'a bölümünü koyun (README'de adım adım anlatılıyor).
"""
from __future__ import annotations

import httpx

from .. import settings_store as store
from ..config import get_models, get_settings
from .. import storage
from .base import Cost, Ctx, Provider
from .elevenlabs import ElevenLabsTTS
from .heygen import HeyGenAvatar
from .kling import KlingImage, KlingLipSync, KlingOmni, KlingVideo
from .manual import ManualProvider
from .mock import MockProvider

CLASSES: dict[str, type[Provider]] = {
    c.key: c for c in (ElevenLabsTTS, KlingImage, KlingVideo, KlingOmni, KlingLipSync, HeyGenAvatar)
}

# testlerin sahte HTTP katmanı takabilmesi için
transport_override: httpx.AsyncBaseTransport | None = None


def cfg(key: str) -> dict:
    return get_models()["providers"][key]


def resolve_mode(key: str, inp: dict | None = None) -> str:
    cls, c = CLASSES[key], cfg(key)
    if store.get("mock_mode") or not store.has_keys(cls.service):
        return "mock"
    if not c.get("api_available", True):
        return "manual"
    if c.get("needs_remote_video") and not (inp or {}).get("video_url"):
        return "manual"
    return "real"


def build(key: str, mode: str, *, job_id: int = 0, kind: str = "") -> Provider:
    ctx = Ctx(storage_dir=storage.root(), tmp_dir=storage.tmp_dir(), config=cfg(key),
              model=store.model_for(key), secrets=store.get_secret, job_id=job_id, kind=kind,
              mock_delay=get_settings().mock_delay, transport=transport_override)
    if mode == "mock":
        p: Provider = MockProvider(ctx, CLASSES[key].kind)
    elif mode == "manual":
        p = ManualProvider(ctx, CLASSES[key].kind)
    else:
        p = CLASSES[key](ctx)
    return p


def estimate(key: str, inp: dict) -> Cost:
    return CLASSES[key].estimate_cost(inp, cfg(key), store.model_for(key))


def describe() -> list[dict]:
    """Ayarlar ekranı için sağlayıcı özeti."""
    out = []
    for key, cls in CLASSES.items():
        c = cfg(key)
        out.append({"key": key, "label": c.get("label", key), "service": cls.service, "kind": cls.kind,
                    "mode": resolve_mode(key, {"video_url": "x"}), "has_key": store.has_keys(cls.service),
                    "model": store.model_for(key), "default": c["default"],
                    "needs_remote_video": bool(c.get("needs_remote_video")),
                    "models": [{"id": m["id"], "label": m.get("label", m["id"])} for m in c.get("models", [])]})
    return out
