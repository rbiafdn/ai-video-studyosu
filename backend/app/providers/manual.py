"""'Elle adım' adaptörü: resmi API'nin yapamadığı işi kullanıcı sitede yapar, sonucu yükler.

submit işi 'elle bekliyor' durumuna alır. Arayüz talimatı, girdilerin indirme bağlantılarını ve
bir yükleme alanını gösterir. Yükleme gelince iş kimliği 'manual:<geçici dosya>' olur ve akış
diğer adaptörlerle aynı şekilde (poll -> fetch -> doğrulama) sürer.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import PermanentError, Polled, Provider, Submitted


class ManualProvider(Provider):
    label = "Elle adım"
    poll_interval = 0.2

    def __init__(self, ctx, kind: str = "video"):
        super().__init__(ctx)
        self.kind = kind

    async def submit(self, inp: dict) -> Submitted:
        return Submitted("manual", manual=True)

    def _file(self, external_id: str) -> Path | None:
        if ":" not in external_id:
            return None
        return self.ctx.tmp_dir / external_id.split(":", 1)[1]

    async def poll(self, external_id: str) -> Polled:
        f = self._file(external_id)
        if f is None:
            return Polled("running")
        return Polled("succeeded", 1.0) if f.exists() else Polled("failed", error="Yüklenen dosya bulunamadı")

    async def fetch(self, external_id: str, dest: Path) -> Path:
        f = self._file(external_id)
        if f is None or not f.exists():
            raise PermanentError("Yüklenen dosya bulunamadı")
        dest = dest.with_suffix(f.suffix or dest.suffix)
        shutil.move(str(f), dest)
        return dest
