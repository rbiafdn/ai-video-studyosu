"""Gizli değerlerin şifrelenmesi, maskelenmesi ve belirteç (token) üretimi."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def _fernet() -> Fernet:
    key = get_settings().encryption_key.strip()
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        # Fernet biçiminde değilse paroladan türet
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()


def decrypt_json(token: str) -> dict | None:
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, ValueError):
        return None  # ENCRYPTION_KEY değişmiş ya da kayıt bozuk


def last4(value: str) -> str:
    """Arayüzde gösterilen tek parça: anahtarın son 4 karakteri."""
    value = (value or "").strip()
    return value[-4:] if len(value) >= 8 else ""


def mask(value: str) -> str:
    l4 = last4(value)
    return f"•••• {l4}" if l4 else ("••••" if value else "")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Belirteçler veritabanında özet olarak saklanır; sızan veritabanı oturum çalmaya yetmez."""
    return hmac.new(get_settings().app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def sign(value: str, ttl: int = 300) -> str:
    """Kısa ömürlü imzalı değer: '<bitiş>.<imza>'."""
    exp = int(time.time()) + ttl
    sig = hmac.new(get_settings().app_secret.encode(), f"{value}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def verify(value: str, signed: str) -> bool:
    try:
        exp_s, sig = signed.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    good = hmac.new(get_settings().app_secret.encode(), f"{value}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, sig)


def jwt_hs256(claims: dict, secret: str) -> str:
    """Kling'in eski (/v1) uçları için AccessKey/SecretKey ile imzalı JWT."""
    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"
