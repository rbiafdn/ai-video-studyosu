"""Dil listesi (config/languages.yaml), sağlayıcı desteği ve otomatik dil algılama."""
from __future__ import annotations

import sys
import threading
from functools import lru_cache

import yaml

from .config import ROOT, get_models, get_settings


@lru_cache
def _cfg() -> dict:
    path = get_settings().models_file.parent / "languages.yaml"
    if not path.exists():
        path = ROOT / "config" / "languages.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def base(code: str) -> str:
    """BCP-47 kodunun taban dili: es-MX -> es."""
    return (code or "").split("-")[0].lower()


def normalize(code: str) -> str:
    """tr, en, es-MX biçimine getirir."""
    parts = (code or "").strip().replace("_", "-").split("-")
    if not parts or not parts[0]:
        return ""
    return "-".join([parts[0].lower(), *[p.upper() if len(p) == 2 else p.title() for p in parts[1:]]])


def all_languages() -> list[dict]:
    return _cfg()["languages"]


def info(code: str) -> dict | None:
    b = base(code)
    exact = next((l for l in all_languages() if l["code"] == code), None)
    return exact or next((l for l in all_languages() if l["code"] == b), None)


def known(code: str) -> bool:
    return info(code) is not None


def name(code: str) -> str:
    i = info(code)
    return f"{i['name']} ({code})" if i and code != i["code"] else (i["name"] if i else code)


def is_rtl(code: str) -> bool:
    i = info(code)
    return bool(i and i.get("rtl"))


def font_for(code: str) -> str:
    i = info(code) or {}
    fonts = _cfg()["fonts"].get(i.get("script", "latin")) or _cfg()["fonts"]["latin"]
    key = "windows" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")
    return fonts.get(key) or fonts["linux"]


def tts_models(code: str) -> list[str]:
    i = info(code)
    return list(i.get("elevenlabs") or []) if i else []


def tts_model_for(code: str, selected: str) -> str | None:
    """Seçili model dili destekliyorsa onu, desteklemiyorsa destekleyen ilk modeli döndürür.

    Bu bir MODEL seçimidir; dil asla değiştirilmez. Hiçbir model desteklemiyorsa None.
    """
    models = tts_models(code)
    if not models:
        return None
    if selected in models:
        return selected
    order = [m["id"] for m in get_models()["providers"]["elevenlabs_tts"]["models"]]
    return next((m for m in order if m in models), None)


def support(provider: str, code: str) -> tuple[bool, str]:
    """(destekleniyor mu, açıklama). Desteklenmiyorsa üretim başlamadan uyarı verilir."""
    i = info(code)
    if i is None:
        return False, f"'{code}' dili config/languages.yaml içinde tanımlı değil"
    if provider == "elevenlabs_tts":
        if not i.get("elevenlabs"):
            return False, f"ElevenLabs {i['name']} dilini desteklemiyor"
        return True, ""
    if i.get(provider) is False:
        return False, f"{provider} {i['name']} dilini desteklemiyor"
    note = (_cfg().get("provider_notes") or {}).get(provider) or {}
    if note.get("documented") is False:
        return True, "dil desteği belgelenmemiş (ses dosyasından çalışır)"
    return True, ""


def matrix() -> list[dict]:
    """Ayarlar ekranı ve rapor için: dil x sağlayıcı desteği."""
    out = []
    for l in all_languages():
        out.append({"code": l["code"], "name": l["name"], "rtl": bool(l.get("rtl")), "script": l.get("script", "latin"),
                    "elevenlabs": l.get("elevenlabs") or [],
                    "heygen": l.get("heygen_avatar", True), "kling_lipsync": l.get("kling_lipsync", True),
                    "font": font_for(l["code"])})
    return out


# ------------------------------------------------------------------ otomatik algılama
_detector = None
_lock = threading.Lock()


def _get_detector():
    global _detector
    with _lock:
        if _detector is None:
            from lingua import IsoCode639_1, Language, LanguageDetectorBuilder
            wanted = []
            for l in all_languages():
                try:
                    wanted.append(Language.from_iso_code_639_1(getattr(IsoCode639_1, l["code"].upper())))
                except (AttributeError, ValueError):
                    pass
            _detector = LanguageDetectorBuilder.from_languages(*wanted).build()
    return _detector


def detect(text: str, fallback: str) -> dict:
    """Metnin dilini algılar.

    Dönen: {"language": kullanılacak dil, "detected": algılanan, "confidence": 0-1, "fallback": bool}
    Metin kısaysa ya da güven düşükse 'fallback' (proje/bölüm dili) kullanılır.
    """
    conf = _cfg().get("detection") or {}
    text = (text or "").strip()
    out = {"language": fallback, "detected": "", "confidence": 0.0, "fallback": True}
    if not text:
        return out
    try:
        values = _get_detector().compute_language_confidence_values(text)
    except Exception:
        return out
    if not values:
        return out
    top = values[0]
    code = top.language.iso_code_639_1.name.lower()
    out.update(detected=code, confidence=round(float(top.value), 3))
    if len(text) >= conf.get("min_chars", 20) and top.value >= conf.get("min_confidence", 0.6):
        # proje dili aynı taban dilin bölgeli hâliyse (es-MX) onu koru
        out.update(language=fallback if base(fallback) == code else code, fallback=False)
    return out
