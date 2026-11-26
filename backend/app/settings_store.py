"""Ayarlar: sistem ayarları (yönetici), kullanıcı tercihleri ve kullanıcının servis anahtarlarına erişim.

Anahtarlar `connections` tablosunda şifreli durur ve her zaman GEÇERLİ KULLANICI bağlamından
(tenancy.USER_ID) okunur: bir iş ya da ajan çalışması yalnızca sahibinin anahtarını görebilir.
"""
from __future__ import annotations

import json
from typing import Any

from .config import get_models, get_settings
from .db import session_scope
from .models import Setting, User
from . import tenancy

PROVIDER_KEYS = ("elevenlabs_tts", "kling_image", "kling_video", "kling_omni", "kling_lipsync", "heygen_avatar")
SERVICES = ("anthropic", "elevenlabs", "kling", "heygen")
# eski anahtar adı -> (servis, alan)
SECRET_FIELDS = {
    "anthropic_api_key": ("anthropic", "api_key"),
    "elevenlabs_api_key": ("elevenlabs", "api_key"),
    "kling_api_key": ("kling", "api_key"),
    "kling_webhook_secret": ("kling", "webhook_secret"),   # isteğe bağlı: geri çağrı imzası
    "heygen_api_key": ("heygen", "api_key"),
}
SECRET_KEYS = tuple(SECRET_FIELDS)
SERVICE_KEYS = {"anthropic": ("anthropic_api_key",), "elevenlabs": ("elevenlabs_api_key",),
                "kling": ("kling_api_key",), "heygen": ("heygen_api_key",)}


def system_defaults() -> dict[str, Any]:
    return {
        "registration_mode": "invite",          # open | invite
        "demo_mode": get_settings().demo_mode,  # Anthropic anahtarı olmayan kullanıcıya sahte yönetmen
        "mock_mode": False,                     # true: hiçbir servise istek gitmez (bakım/deneme)
        "max_attempts": 4,
        "retry_base_seconds": 5.0,
        # tüm kullanıcılar için sağlayıcı başına toplam eşzamanlı iş tavanı
        "global_concurrency": {"elevenlabs_tts": 6, "kling_image": 6, "kling_video": 6, "kling_omni": 3,
                               "kling_lipsync": 3, "heygen_avatar": 6, "local": 2},
        # ortak (kurum) anahtarı kullanan bir kullanıcının aylık harcama sınırı (USD)
        "shared_monthly_usd": 50.0,
    }


def pref_defaults() -> dict[str, Any]:
    return {
        "default_language": "tr",
        "budget_usd": 25.0,          # çalışma başına bütçe sınırı
        "plan_approval": False,      # üretimden önce planı onayla
        "output_format": "16:9",     # 16:9 | 9:16
        "models": {},                # sağlayıcı -> seçili model
        "claude_model": "",
        # kullanıcının sağlayıcı başına eşzamanlı iş sınırı
        "concurrency": {"elevenlabs_tts": 2, "kling_image": 2, "kling_video": 2, "kling_omni": 1,
                        "kling_lipsync": 1, "heygen_avatar": 2, "local": 1},
    }


def _merge(default: Any, value: Any) -> Any:
    return {**default, **value} if isinstance(default, dict) and isinstance(value, dict) else value


# --- sistem ayarları -----------------------------------------------------------
def sys_get(key: str) -> Any:
    d = system_defaults().get(key)
    with session_scope() as s:
        row = s.get(Setting, key)
        if row is None:
            return d
        return _merge(d, json.loads(row.value))


def sys_put(key: str, value: Any) -> None:
    with session_scope() as s:
        row = s.get(Setting, key) or Setting(key=key)
        row.value, row.secret = json.dumps(value), False
        s.add(row)


def system_view() -> dict:
    return {k: sys_get(k) for k in system_defaults()}


# --- kullanıcı tercihleri -------------------------------------------------------
def prefs(user_id: int | None = None) -> dict:
    user_id = user_id if user_id is not None else tenancy.uid()
    out = pref_defaults()
    if user_id is None:
        return out
    with session_scope() as s:
        u = s.get(User, user_id)
        saved = dict(u.prefs or {}) if u else {}
    for k, v in saved.items():
        if k in out:
            out[k] = _merge(out[k], v)
    return out


def put_prefs(user_id: int, patch: dict) -> dict:
    with session_scope() as s:
        u = s.get(User, user_id)
        cur = dict(u.prefs or {})
        for k, v in patch.items():
            if k in pref_defaults() and v is not None:
                cur[k] = v
        u.prefs = cur
    return prefs(user_id)


def get(key: str) -> Any:
    """Tercih anahtarıysa geçerli kullanıcının tercihi, değilse sistem ayarı."""
    if key in pref_defaults():
        return prefs()[key]
    return sys_get(key)


def put(key: str, value: Any) -> None:
    if key in pref_defaults():
        put_prefs(tenancy.require_uid(), {key: value})
    else:
        sys_put(key, value)


# --- anahtarlar (geçerli kullanıcının) -------------------------------------------
def get_secret(key: str, user_id: int | None = None) -> str:
    from . import connections
    service, fld = SECRET_FIELDS[key]
    creds, _ = connections.resolve(service, user_id if user_id is not None else tenancy.uid())
    return (creds or {}).get(fld, "") or ""


def has_keys(service: str, user_id: int | None = None) -> bool:
    from . import connections
    creds, _ = connections.resolve(service, user_id if user_id is not None else tenancy.uid())
    return bool(creds)


def cred_source(service: str, user_id: int | None = None) -> str:
    from . import connections
    return connections.resolve(service, user_id if user_id is not None else tenancy.uid())[1]


def all_secrets() -> list[str]:
    """Hata iletilerinde maskelemek için geçerli kullanıcının görebildiği bütün gizli değerler."""
    from . import connections
    out = []
    for service in SERVICES:
        creds, _ = connections.resolve(service, tenancy.uid(), check_limit=False)
        out += [v for v in (creds or {}).values() if isinstance(v, str) and len(v) >= 6]
    return out


def model_for(provider: str) -> str:
    cfg = get_models()["providers"][provider]
    chosen = (get("models") or {}).get(provider)
    ids = [m["id"] for m in cfg.get("models", [])]
    return chosen if chosen in ids else cfg["default"]


def claude_model() -> str:
    cfg = get_models()["claude"]
    chosen = get("claude_model")
    return chosen if chosen in [m["id"] for m in cfg["models"]] else cfg["default"]
