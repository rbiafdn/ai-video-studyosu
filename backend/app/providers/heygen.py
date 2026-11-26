"""HeyGen: karakter görseli + ses dosyası -> konuşan video (v3 API).

Doküman: https://developers.heygen.com/llms.txt (26 Kasım 2026 okuması; v1/v2 31 Ekim 2026'da kapandı)
- Kimlik: X-Api-Key
- Yükleme: POST /v3/assets (multipart, alan adı "file") -> data.asset_id
- Avatar 6:  POST /v3/models/avatars            -> sorgu GET /v3/models/videos/{id}
- Avatar IV: POST /v3/videos (type: image)      -> sorgu GET /v3/videos/{id}
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .. import media
from .base import AuthFailure, Cost, PermanentError, Polled, Provider, Submitted

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp3": "audio/mpeg",
        ".wav": "audio/wav", ".m4a": "audio/mp4"}


class HeyGenAvatar(Provider):
    key = "heygen_avatar"
    service = "heygen"
    kind = "video"
    label = "HeyGen"
    poll_interval = 10.0

    def _secret_values(self):
        return [self.ctx.secrets("heygen_api_key")]

    def headers(self, **extra) -> dict:
        return {"X-Api-Key": self.ctx.secrets("heygen_api_key"), **extra}

    def url(self, path: str) -> str:
        return self.ctx.config["base_url"].rstrip("/") + path

    def error_message(self, r: httpx.Response) -> str:
        try:
            e = r.json().get("error") or {}
            return f"HTTP {r.status_code}: {e.get('message') or e.get('code') or r.text[:200]} [{e.get('code', '')}]"
        except Exception:
            return super().error_message(r)

    def classify(self, r: httpx.Response) -> None:
        # 409 resource_not_ready / request_in_progress geçicidir; kredi ve kota hataları kalıcı
        if r.status_code == 429:
            try:
                if (r.json().get("error") or {}).get("code") == "quota_exceeded":
                    raise AuthFailure(f"HeyGen: {self.error_message(r)}", "credit")
            except ValueError:
                pass
        super().classify(r)  # 401/403 -> anahtar geçersiz, 402 -> kredi yok

    async def upload(self, path: Path) -> str:
        with open(path, "rb") as f:
            files = {"file": (path.name, f.read(), MIME.get(path.suffix.lower(), "application/octet-stream"))}
        r = await self.request("POST", self.url("/v3/assets"), files=files, headers=self.headers())
        aid = (r.json().get("data") or {}).get("asset_id")
        if not aid:
            raise PermanentError("HeyGen yükleme yanıtında asset_id yok")
        return aid

    async def _prepared_image(self, rel: str) -> Path:
        """Görsel sınırı aşıyorsa küçültülmüş JPEG kopyası hazırlar."""
        p = self.ctx.path(rel)
        if not p.exists():
            raise PermanentError(f"Görsel dosyası yok: {rel}")
        side = self.ctx.config.get("max_image_side", 1920)
        i = await asyncio.to_thread(media.info, p)
        if max(i["width"], i["height"]) <= side and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
            return p
        out = self.ctx.tmp_dir / f"hg_{self.ctx.job_id}.jpg"
        await media.affmpeg(["-i", p, "-vf", f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease",
                             "-frames:v", "1", "-q:v", "2", out])
        return out

    def build_body(self, inp: dict, image_id: str, audio_id: str) -> tuple[str, dict]:
        route = self.ctx.model_cfg().get("route", "models_avatars")
        res = self.ctx.config.get("resolution", "1080p")
        if route == "models_avatars":
            body = {"model": self.ctx.model,
                    "reference_images": [{"type": "asset_id", "asset_id": image_id}],
                    "audio": {"type": "asset_id", "asset_id": audio_id},
                    "resolution": res}
            if inp.get("prompt"):
                body["motion_prompt"] = inp["prompt"][:1200]
            return "/v3/models/avatars", body
        body = {"type": "image", "image": {"type": "asset_id", "asset_id": image_id},
                "audio_asset_id": audio_id, "resolution": res,
                "aspect_ratio": inp.get("aspect_ratio") or "16:9", "fit": "cover",
                "title": inp.get("title") or f"studyo-{self.ctx.job_id}"}
        if inp.get("prompt"):
            body["motion_prompt"] = inp["prompt"][:1200]
        return "/v3/videos", body

    async def submit(self, inp: dict) -> Submitted:
        if not inp.get("image_path") or not inp.get("audio_path"):
            raise PermanentError("HeyGen için onaylı görsel ve onaylı ses gerekli")
        limit = self.ctx.config.get("max_audio_seconds", 600)
        if (inp.get("audio_duration") or 0) > limit:
            raise PermanentError(f"Ses {inp['audio_duration']:.0f} sn; HeyGen sınırı {limit} sn")
        audio = self.ctx.path(inp["audio_path"])
        if not audio.exists():
            raise PermanentError(f"Ses dosyası yok: {inp['audio_path']}")
        image_id = await self.upload(await self._prepared_image(inp["image_path"]))
        audio_id = await self.upload(audio)
        path, body = self.build_body(inp, image_id, audio_id)
        # aynı iş yeniden gönderilirse çift ücret kesilmesin
        if inp.get("callback_url"):
            body["callback_url"] = inp["callback_url"]
        headers = self.headers(**({"Idempotency-Key": inp["idempotency_key"]} if inp.get("idempotency_key") else {}))
        r = await self.request("POST", self.url(path), json=body, headers=headers)
        vid = (r.json().get("data") or {}).get("video_id")
        if not vid:
            raise PermanentError("HeyGen yanıtında video_id yok")
        return Submitted(f"{'m' if path.endswith('avatars') else 'v'}:{vid}")

    async def _status(self, external_id: str) -> dict:
        route, vid = external_id.split(":", 1)
        path = f"/v3/models/videos/{vid}" if route == "m" else f"/v3/videos/{vid}"
        r = await self.request("GET", self.url(path), headers=self.headers())
        return r.json().get("data") or {}

    async def poll(self, external_id: str) -> Polled:
        d = await self._status(external_id)
        st = d.get("status")
        if st == "completed":
            return Polled("succeeded", 1.0)
        if st in ("failed", "cancelled"):
            return Polled("failed", error=f"HeyGen: {d.get('failure_message') or st} [{d.get('failure_code') or ''}]")
        return Polled("running", 0.5 if st == "processing" else 0.1)

    async def fetch(self, external_id: str, dest: Path) -> Path:
        # video_url imzalı ve süreli: her seferinde taze adres alınır
        d = await self._status(external_id)
        if not d.get("video_url"):
            raise PermanentError("HeyGen işi bitti ama video adresi yok")
        await self.download(d["video_url"], dest.with_suffix(".mp4"))
        self.result_meta = {"source_url": d["video_url"], "remote_id": external_id.split(":", 1)[1]}
        return dest.with_suffix(".mp4")

    @classmethod
    def estimate_cost(cls, inp, config, model) -> Cost:
        mc = next((m for m in config.get("models", []) if m["id"] == model), {})
        d = float(inp.get("audio_duration") or 0)
        return Cost(usd=round(d * mc.get("usd_per_second", 0.0), 4), units=round(d, 2), unit="saniye")
