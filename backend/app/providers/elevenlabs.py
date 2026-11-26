"""ElevenLabs metinden sese.

Doküman: https://elevenlabs.io/docs/api-reference/text-to-speech/convert
POST /v1/text-to-speech/{voice_id}?output_format=...   başlık: xi-api-key
Uç nokta eşzamanlıdır (iş kuyruğu yok): submit sesi alıp geçici dosyaya yazar, poll hazır der.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from .base import Cost, PermanentError, Polled, Provider, Submitted, TransientError

VOICE_FIELDS = ("stability", "similarity_boost", "style", "use_speaker_boost", "speed")


class ElevenLabsTTS(Provider):
    key = "elevenlabs_tts"
    service = "elevenlabs"
    kind = "audio"
    label = "ElevenLabs"
    poll_interval = 0.2

    def _secret_values(self):
        return [self.ctx.secrets("elevenlabs_api_key")]

    def error_message(self, r: httpx.Response) -> str:
        try:
            d = r.json().get("detail")
            if isinstance(d, dict):
                return f"HTTP {r.status_code}: {d.get('message') or d.get('code') or d}"
            return f"HTTP {r.status_code}: {str(d)[:300]}"
        except Exception:
            return super().error_message(r)

    def _ext(self) -> str:
        fmt = self.ctx.config.get("output_format", "mp3_44100_128")
        return {"mp3": ".mp3", "wav": ".wav", "opus": ".opus"}.get(fmt.split("_")[0], ".mp3")

    def build_request(self, inp: dict) -> tuple[str, dict, dict]:
        """(url, sorgu parametreleri, gövde): testlerde biçimi doğrulamak için ayrı."""
        text = (inp.get("text") or "").strip()
        if not text:
            raise PermanentError("Seslendirilecek metin boş")
        voice = (inp.get("voice_id") or "").strip()
        if not voice:
            raise PermanentError("Karakterin ElevenLabs ses kimliği (voice_id) girilmemiş")
        # dili destekleyen model iş girdisinde gelir (yoksa ayarlardaki model)
        model = inp.get("model") or self.ctx.model
        mc = next((m for m in self.ctx.config.get("models", []) if m["id"] == model), {})
        limit = mc.get("max_chars")
        if limit and len(text) > limit:
            raise PermanentError(f"Metin {len(text)} karakter; {model} sınırı {limit}")
        body: dict = {"text": text, "model_id": model}
        vs = {k: v for k, v in (inp.get("voice_settings") or {}).items() if k in VOICE_FIELDS and v is not None}
        if vs:
            body["voice_settings"] = vs
        # language_code: ISO 639-1; bazı modeller (multilingual_v2) bu parametreyi desteklemez
        if inp.get("language_code") and model not in (self.ctx.config.get("no_language_code") or []):
            body["language_code"] = inp["language_code"]
        if inp.get("pronunciation_locators"):
            body["pronunciation_dictionary_locators"] = inp["pronunciation_locators"][:3]
        url = f"{self.ctx.config['base_url']}/v1/text-to-speech/{voice}"
        return url, {"output_format": self.ctx.config.get("output_format", "mp3_44100_128")}, body

    async def submit(self, inp: dict) -> Submitted:
        url, params, body = self.build_request(inp)
        r = await self.request("POST", url, params=params, json=body,
                               headers={"xi-api-key": self.ctx.secrets("elevenlabs_api_key")})
        if not r.content:
            raise TransientError("ElevenLabs boş ses döndürdü")
        out = self.ctx.tmp_dir / f"el_{self.ctx.job_id}{self._ext()}"
        out.write_bytes(r.content)
        try:
            units = float(r.headers.get("character-cost", ""))
        except ValueError:
            units = None
        return Submitted(external_id=out.name, cost_units=units)

    async def poll(self, external_id: str) -> Polled:
        if (self.ctx.tmp_dir / external_id).exists():
            return Polled("succeeded", 1.0)
        return Polled("lost")  # yeniden başlatmada geçici dosya gitmişse yeniden üret

    async def fetch(self, external_id: str, dest: Path) -> Path:
        src = self.ctx.tmp_dir / external_id
        dest = dest.with_suffix(src.suffix)
        shutil.move(str(src), dest)
        return dest

    @classmethod
    def estimate_cost(cls, inp: dict, config: dict, model: str) -> Cost:
        model = inp.get("model") or model
        mc = next((m for m in config.get("models", []) if m["id"] == model), {})
        n = len((inp.get("text") or "").strip())
        return Cost(usd=round(n / 1000 * mc.get("usd_per_1k_chars", 0.0), 5), units=n, unit="karakter")

    async def list_voices(self, language: str | None = None) -> list[dict]:
        """GET /v2/voices. language: 'language' etiketine göre süzer (ISO 639-1)."""
        params: dict = {"page_size": 100}
        if language:
            params["language"] = language
        r = await self.request("GET", f"{self.ctx.config['base_url']}/v2/voices", params=params,
                               headers={"xi-api-key": self.ctx.secrets("elevenlabs_api_key")})
        out = []
        for v in r.json().get("voices", []):
            langs = sorted({x.get("language") for x in (v.get("verified_languages") or []) if x.get("language")})
            out.append({"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category"),
                        "labels": v.get("labels") or {}, "languages": langs, "preview_url": v.get("preview_url")})
        return out
