"""Süreç içi olay yolu: SSE ile arayüze canlı durum gönderir.

Her olay bir kullanıcıya aittir ve yalnızca o kullanıcının açık bağlantılarına iletilir; sahibi
belli olmayan olay hiç kimseye gönderilmez.
"""
from __future__ import annotations

import asyncio
import json


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[asyncio.Queue, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, user_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs[q] = user_id
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.pop(q, None)

    def _put(self, user_id: int, data: str) -> None:
        for q, uid in list(self._subs.items()):
            if uid != user_id:
                continue
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def publish(self, type_: str, user_id: int | None = None, **payload) -> None:
        """Her iş parçacığından güvenle çağrılabilir. user_id verilmezse geçerli kullanıcı bağlamı kullanılır."""
        if user_id is None:
            from . import tenancy
            user_id = tenancy.uid()
        if user_id is None:
            return
        data = json.dumps({"type": type_, **payload}, default=str, ensure_ascii=False)
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._put(user_id, data)
        else:
            loop.call_soon_threadsafe(self._put, user_id, data)


bus = EventBus()
