"""Kling adaptörleri: görsel, video, Omni (video düzeltme), dudak senkronu.

Doküman dizini: https://kling.ai/document-api/llms.txt (26 Kasım 2026 okuması)
- Kimlik: Authorization: Bearer <API anahtarı>
- Yeni tasarım: POST /image-generation/{model}, /image-to-video/{model}, /text-to-video/{model},
  /omni-video/{model}; gövde contents/settings/options; sorgu GET /tasks?task_ids=...
- Dudak senkronu hâlâ /v1: identify-face -> advanced-lip-sync (iki adım)
- Görsel ve ses base64 gönderilebilir; VİDEO girdisi yalnızca URL (yükleme ucu yok).
"""
from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import AuthFailure, Cost, PermanentError, Polled, Provider, Submitted, TransientError

# dokümana göre yeniden denenebilir kodlar: hız sınırı, eşzamanlılık, sunucu hataları
TRANSIENT_CODES = {1302, 1303, 5000, 5001, 5002}
CODE_HINTS = {
    1000: "kimlik doğrulama başarısız", 1001: "kimlik doğrulama başarısız", 1002: "API anahtarı geçersiz",
    1003: "anahtar henüz geçerli değil", 1004: "anahtarın süresi dolmuş", 1100: "hesap durumu anormal",
    1101: "hesapta borç var", 1102: "kaynak paketi bitmiş ya da süresi dolmuş", 1103: "bu işlem için yetki yok",
    1200: "parametre hatalı", 1201: "parametre hatalı", 1300: "platform politikası", 1301: "içerik güvenliği reddi",
    1302: "hız sınırı", 1303: "eşzamanlı iş sınırı", 1304: "IP beyaz listesi",
}


class KlingBase(Provider):
    service = "kling"
    label = "Kling"
    poll_interval = 6.0

    def _secret_values(self):
        return [self.ctx.secrets("kling_api_key")]

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.ctx.secrets('kling_api_key')}", "Content-Type": "application/json"}

    def url(self, path: str) -> str:
        return self.ctx.config["base_url"].rstrip("/") + path

    @staticmethod
    def with_callback(body: dict, inp: dict) -> dict:
        """Geri çağrı adresi varsa ekler (yoklama yine yedek olarak sürer)."""
        if inp.get("callback_url"):
            body["options"] = {**body.get("options", {}), "callback_url": inp["callback_url"]}
        return body

    def classify(self, r: httpx.Response) -> None:
        code = None
        message = ""
        try:
            body = r.json()
            code, message = body.get("code"), body.get("message") or ""
        except Exception:
            message = r.text[:200]
        if r.status_code < 400 and code in (0, None):
            return
        hint = CODE_HINTS.get(code, "")
        msg = f"Kling: {hint + ': ' if hint else ''}{message} (kod {code}, HTTP {r.status_code})"
        # 1100-1102 ve 1304 de HTTP 429 döner: duruma değil koda bakılır
        if code in TRANSIENT_CODES or (code is None and (r.status_code >= 500 or r.status_code == 429)):
            raise TransientError(msg)
        if code in (1000, 1001, 1002, 1003, 1004) or (code is None and r.status_code == 401):
            raise AuthFailure(msg, "invalid")
        if code in (1101, 1102):
            raise AuthFailure(msg, "credit")
        raise PermanentError(msg)

    def b64(self, rel: str, max_mb: float = 30) -> str:
        p = self.ctx.path(rel)
        if not p.exists():
            raise PermanentError(f"Girdi dosyası yok: {rel}")
        if p.stat().st_size > max_mb * 1024 * 1024:
            raise PermanentError(f"Girdi dosyası {max_mb:.0f} MB sınırını aşıyor: {p.name}")
        data = base64.b64encode(p.read_bytes()).decode()
        if self.ctx.config.get("base64_data_prefix"):
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(p.suffix.lower(), "application/octet-stream")
            return f"data:{mime};base64,{data}"
        return data

    @staticmethod
    def task_id(r: httpx.Response) -> str:
        data = r.json().get("data") or {}
        tid = data.get("id") or data.get("task_id")
        if not tid:
            raise PermanentError("Kling yanıtında iş kimliği yok")
        return str(tid)

    # --- yeni tasarım: GET /tasks -------------------------------------------
    async def _task(self, external_id: str) -> dict:
        r = await self.request("GET", self.url("/tasks"), params={"task_ids": external_id}, headers=self.headers())
        data = r.json().get("data") or []
        if isinstance(data, dict):
            data = [data]
        if not data:
            return {}
        return data[0]

    async def poll(self, external_id: str) -> Polled:
        t = await self._task(external_id)
        if not t:
            return Polled("lost")
        st = t.get("status")
        if st in ("succeeded", "succeed"):
            return Polled("succeeded", 1.0)
        if st == "failed":
            return Polled("failed", error=f"Kling: {t.get('message') or 'üretim başarısız'}")
        return Polled("running", 0.5 if st == "processing" else 0.1)

    output_type = "video"

    async def fetch(self, external_id: str, dest: Path) -> Path:
        t = await self._task(external_id)
        outs = [o for o in (t.get("outputs") or []) if o.get("type") == self.output_type] or (t.get("outputs") or [])
        if not outs or not outs[0].get("url"):
            raise PermanentError("Kling işi bitti ama çıktı adresi yok")
        return await self._save(outs[0]["url"], dest, outs[0].get("id"))

    async def _save(self, url: str, dest: Path, remote_id=None) -> Path:
        ext = Path(urlparse(url).path).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov"):
            dest = dest.with_suffix(ext)
        await self.download(url, dest)
        # Omni ve dudak senkronu video girdisini URL ile aldığı için uzak adresi saklıyoruz
        self.result_meta = {"source_url": url, "remote_id": remote_id}
        return dest


class KlingImage(KlingBase):
    key = "kling_image"
    kind = "image"
    label = "Kling görsel"
    output_type = "image"

    def build_body(self, inp: dict) -> dict:
        prompt = (inp.get("prompt") or "").strip()
        if not prompt:
            raise PermanentError("Görsel prompt boş")
        refs = (inp.get("ref_image_paths") or [])[: self.ctx.config.get("max_refs", 10)]
        contents: list[dict] = []
        ids = []
        for i, rel in enumerate(refs, 1):
            ids.append(f"image_{i}")
            contents.append({"type": "refer_image", "url": self.b64(rel), "id": ids[-1]})
        if ids:
            prompt += " Keep the characters identical to " + " and ".join("@" + i for i in ids) + "."
        return self.with_callback({
            "contents": [{"type": "prompt", "text": prompt}, *contents],
            "settings": {"result_type": "single", "n": 1,
                         "resolution": self.ctx.config.get("resolution", "2k"),
                         "aspect_ratio": inp.get("aspect_ratio") or "16:9"},
        }, inp)

    async def submit(self, inp: dict) -> Submitted:
        path = self.ctx.model_cfg().get("path") or f"/image-generation/{self.ctx.model}"
        r = await self.request("POST", self.url(path), json=self.build_body(inp), headers=self.headers())
        return Submitted(self.task_id(r))

    @classmethod
    def estimate_cost(cls, inp, config, model) -> Cost:
        mc = next((m for m in config.get("models", []) if m["id"] == model), {})
        base = (mc.get("usd_per_image") or {}).get(config.get("resolution", "2k"), 0.0)
        n = len(inp.get("ref_image_paths") or [])
        return Cost(usd=round(base + n * mc.get("usd_per_ref", 0.0), 4), units=1, unit="görsel")


def clamp_duration(mc: dict, seconds: float) -> int:
    want = max(1, round(seconds or 5))
    if mc.get("durations"):
        return min(mc["durations"], key=lambda d: (abs(d - want), d))
    return int(min(max(want, mc.get("min_duration", 3)), mc.get("max_duration", 15)))


class KlingVideo(KlingBase):
    key = "kling_video"
    kind = "video"
    label = "Kling video"

    def build_request(self, inp: dict) -> tuple[str, dict]:
        mc = self.ctx.model_cfg()
        prompt = (inp.get("prompt") or "").strip()
        if not prompt:
            raise PermanentError("Video prompt boş")
        settings: dict = {"duration": clamp_duration(mc, inp.get("duration") or 5),
                          "resolution": mc.get("resolution") or self.ctx.config.get("resolution", "1080p")}
        # "sesli an": ses anahtarı olan modellerde yerel ses açılır; 4.0'da zaten hep açık
        if mc.get("audio") == "toggle":
            settings["audio"] = "native" if inp.get("native_audio") else "off"
        if inp.get("image_path"):
            body = {"contents": [{"type": "prompt", "text": prompt},
                                 {"type": "first_frame", "url": self.b64(inp["image_path"]), "id": "frame_1"}],
                    "settings": settings}
            return f"/image-to-video/{self.ctx.model}", self.with_callback(body, inp)
        settings["aspect_ratio"] = inp.get("aspect_ratio") or "16:9"
        return f"/text-to-video/{self.ctx.model}", self.with_callback({"prompt": prompt, "settings": settings}, inp)

    async def submit(self, inp: dict) -> Submitted:
        path, body = self.build_request(inp)
        r = await self.request("POST", self.url(path), json=body, headers=self.headers())
        return Submitted(self.task_id(r))

    @classmethod
    def estimate_cost(cls, inp, config, model) -> Cost:
        mc = next((m for m in config.get("models", []) if m["id"] == model), {})
        res = mc.get("resolution") or config.get("resolution", "1080p")
        d = clamp_duration(mc, inp.get("duration") or 5)
        table = mc.get("usd_per_second") or {}
        if inp.get("native_audio") and mc.get("audio") == "toggle":
            table = mc.get("usd_per_second_audio") or table
        return Cost(usd=round(d * table.get(res, 0.0), 4), units=d, unit="saniye")


class KlingOmni(KlingBase):
    """Üretilmiş videoda düzeltme: kaynak video (URL) + düzeltme talimatı."""
    key = "kling_omni"
    kind = "video"
    label = "Kling Omni"

    def build_body(self, inp: dict) -> dict:
        instruction = (inp.get("instruction") or "").strip()
        if not instruction:
            raise PermanentError("Düzeltme talimatı boş")
        if not inp.get("video_url"):
            raise PermanentError("Kling Omni kaynak videoyu yalnızca URL ile alır; bu videonun uzak adresi yok")
        return {"contents": [{"type": "prompt", "text": f"{instruction} (@video_1)"},
                             {"type": "base_video", "url": inp["video_url"], "id": "video_1"}],
                "settings": {"audio": self.ctx.config.get("audio", "original")}}

    async def submit(self, inp: dict) -> Submitted:
        mc = self.ctx.model_cfg()
        if mc.get("max_seconds") and (inp.get("video_duration") or 0) > mc["max_seconds"]:
            raise PermanentError(f"Video {inp['video_duration']:.1f} sn; {self.ctx.model} en çok {mc['max_seconds']} sn kabul ediyor")
        path = mc.get("path") or f"/omni-video/{self.ctx.model}"
        r = await self.request("POST", self.url(path), json=self.build_body(inp), headers=self.headers())
        return Submitted(self.task_id(r))

    @classmethod
    def estimate_cost(cls, inp, config, model) -> Cost:
        mc = next((m for m in config.get("models", []) if m["id"] == model), {})
        d = float(inp.get("video_duration") or 5)
        return Cost(usd=round(d * mc.get("usd_per_second", 0.0), 4), units=round(d, 2), unit="saniye")


class KlingLipSync(KlingBase):
    """Konuşan sahnede ağız düzeltmesi: video (URL) + ses (base64)."""
    key = "kling_lipsync"
    kind = "video"
    label = "Kling dudak senkronu"

    async def submit(self, inp: dict) -> Submitted:
        if not inp.get("video_url"):
            raise PermanentError("Kling dudak senkronu videoyu yalnızca URL ile alır; bu videonun uzak adresi yok")
        if not inp.get("audio_path"):
            raise PermanentError("Dudak senkronu için ses dosyası gerekli")
        # 1) yüz tespiti
        r = await self.request("POST", self.url("/v1/videos/identify-face"),
                               json={"video_url": inp["video_url"]}, headers=self.headers())
        data = r.json().get("data") or {}
        faces = data.get("face_data") or []
        if not data.get("session_id") or not faces:
            raise PermanentError("Kling videoda yüz bulamadı")
        face = max(faces, key=lambda f: (f.get("end_time", 0) - f.get("start_time", 0)))
        # 2) dudak senkronu işi
        dur_ms = int(round(float(inp.get("audio_duration") or 0) * 1000))
        body = {"session_id": data["session_id"],
                "face_choose": [{"face_id": face["face_id"],
                                 "sound_file": self.b64(inp["audio_path"], self.ctx.config.get("max_audio_mb", 5)),
                                 "sound_start_time": 0, "sound_end_time": dur_ms,
                                 "sound_insert_time": int(face.get("start_time") or 0),
                                 "sound_volume": 1, "original_audio_volume": 0}]}
        r = await self.request("POST", self.url("/v1/videos/advanced-lip-sync"), json=body, headers=self.headers())
        return Submitted(self.task_id(r))

    async def _legacy(self, external_id: str) -> dict:
        r = await self.request("GET", self.url(f"/v1/videos/advanced-lip-sync/{external_id}"), headers=self.headers())
        return r.json().get("data") or {}

    async def poll(self, external_id: str) -> Polled:
        d = await self._legacy(external_id)
        st = d.get("task_status")
        if st in ("succeed", "succeeded"):
            return Polled("succeeded", 1.0)
        if st == "failed":
            return Polled("failed", error=f"Kling: {d.get('task_status_msg') or 'dudak senkronu başarısız'}")
        return Polled("running", 0.5 if st == "processing" else 0.1)

    async def fetch(self, external_id: str, dest: Path) -> Path:
        d = await self._legacy(external_id)
        vids = (d.get("task_result") or {}).get("videos") or []
        if not vids or not vids[0].get("url"):
            raise PermanentError("Kling işi bitti ama çıktı adresi yok")
        return await self._save(vids[0]["url"], dest, vids[0].get("id"))

    @classmethod
    def estimate_cost(cls, inp, config, model) -> Cost:
        mc = next((m for m in config.get("models", []) if m["id"] == model), {})
        d = float(inp.get("video_duration") or inp.get("audio_duration") or 5)
        blocks = max(1, -(-int(round(d * 10)) // 50))
        return Cost(usd=round(blocks * mc.get("usd_per_5s", 0.0) + mc.get("usd_face_detect", 0.0), 4),
                    units=round(d, 2), unit="saniye")
