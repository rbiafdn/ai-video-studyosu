"""Testlerin ortak yardımcıları: varsayılan test kullanıcısı, oturum açmış istemci, kullanıcıya anahtar tanımlama."""
from __future__ import annotations

import httpx

from app import auth, connections, settings_store as store
from app.db import session_scope
from app.models import Connection

EMAIL = "test@ornek.com"
PASSWORD = "Test-Sifresi-12345"
UID: int | None = None  # conftest oturum başında doldurur (ilk kullanıcı = yönetici)


def set_key(key: str, value: str, user_id: int | None = None) -> None:
    """Eski tek anahtar adıyla ('kling_api_key' gibi) bir kullanıcının bağlantısını kaydeder ya da siler."""
    service, field = store.SECRET_FIELDS[key]
    uid = user_id if user_id is not None else UID
    with session_scope() as s:
        row = connections._row(s, uid, service)
        cur = (connections.crypto.decrypt_json(row.secret) if row and row.secret else None) or {}
    if value:
        cur[field] = value
    else:
        cur.pop(field, None)
    if cur.get("api_key"):
        connections.save(uid, service, cur)
    else:
        connections.delete(uid, service)


def clear_connections() -> None:
    with session_scope() as s:
        for row in s.query(Connection).all():
            s.delete(row)


def make_user(email: str, password: str = PASSWORD, role: str = "user") -> int:
    with session_scope() as s:
        u = auth.create_user(s, email, password, role=role, verified=True)
        u.setup_done = True
        return u.id


def raw_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def login(c: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD) -> dict:
    """Giriş yapar ve CSRF başlığını istemciye yerleştirir (tarayıcıdaki arayüzün yaptığı gibi)."""
    r = await c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers[auth.CSRF_HEADER] = r.json()["csrf"]
    return r.json()
