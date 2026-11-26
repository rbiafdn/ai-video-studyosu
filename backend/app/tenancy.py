"""Geçerli kullanıcı bağlamı.

Her istek, her arka plan işi ve her ajan çalışması kendi kullanıcısının bağlamında yürür. Kimlik
istemcinin gönderdiği veriden değil sunucu tarafındaki oturumdan / iş kaydından / sohbet kaydından alınır
ve buraya yazılır; anahtarlar, tercihler ve sınırlar bu bağlamdan okunur.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

USER_ID: ContextVar[int | None] = ContextVar("user_id", default=None)


def uid() -> int | None:
    return USER_ID.get()


def require_uid() -> int:
    u = USER_ID.get()
    if u is None:
        raise RuntimeError("Kullanıcı bağlamı yok: bu işlem bir kullanıcı adına yapılmalı")
    return u


@contextmanager
def as_user(user_id: int | None):
    tok = USER_ID.set(user_id)
    try:
        yield
    finally:
        USER_ID.reset(tok)
