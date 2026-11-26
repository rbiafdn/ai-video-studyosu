"""Sahte yönetmen: Anthropic anahtarı yokken Claude'un yerine geçer.

Gerçek modelle AYNI ajan döngüsünü, AYNI araçları ve AYNI servis katmanını kullanır; yalnızca
"hangi aracı çağırayım" kararını önceden yazılmış bir kural dizisi verir. Böylece sohbetten uçtan
uca akış anahtarsız denenebilir. Metin anlamaz: fikri hazır bir örnek senaryoya ya da ekli
senaryo dosyasına çevirir, "İngilizce" ve "N. sahne ..." gibi birkaç basit müdahaleyi tanır.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Callable

from .. import importer, languages
from ..seed import CHARACTERS, SCENES
from .llm import LLMResult

TYPE_TR = {"talking": "konuşan", "silent": "konuşmasız", "narrator": "anlatıcı"}
STOP_AFTER_TURNS = 160


def _low(text: str) -> str:
    """Türkçe'ye uygun küçük harf ("İngilizce" -> "ingilizce")."""
    return text.replace("İ", "i").replace("I", "ı").lower().replace("\u0307", "")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _call(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": "toolu_mock_" + uuid.uuid4().hex[:16], "name": name, "input": inp}


def _user_texts(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        if m["role"] != "user":
            continue
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        for b in content:
            if b.get("type") == "text" and not b["text"].startswith("[Sistem notu"):
                out.append(b["text"])
    return out


def _history(messages: list[dict]) -> list[dict]:
    """Araç çağrıları ve sonuçları: [{name, input, result(dict|str), error, turn}]"""
    results = {}
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else next((x.get("text", "") for x in c or [] if x.get("type") == "text"), "")
                    try:
                        parsed = json.loads(text)
                    except (ValueError, TypeError):
                        parsed = text
                    results[b["tool_use_id"]] = (parsed, bool(b.get("is_error")))
    out, turn = [], 0
    for m in messages:
        if m["role"] != "assistant":
            continue
        turn += 1
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "tool_use":
                res, err = results.get(b["id"], (None, False))
                out.append({"name": b["name"], "input": b.get("input") or {}, "result": res, "error": err, "turn": turn})
    return out


def _scenario(first_message: str) -> tuple[str, list[dict], list[dict]]:
    """(kaynak dil, karakterler, sahneler). Ekli senaryo varsa ondan, yoksa hazır örnekten."""
    m = re.search(r"<senaryo_dosyasi[^>]*>(.*?)</senaryo_dosyasi>", first_message, re.S)
    body = m.group(1) if m else first_message
    try:
        rows = importer.parse(body)
    except Exception:
        rows = []
    idea = re.sub(r"<senaryo_dosyasi.*?</senaryo_dosyasi>", "", first_message, flags=re.S)
    lang = languages.detect(idea or body, "tr")["language"]
    if len(rows) >= 2 and any(r["dialogue"] for r in rows):
        names = []
        for r in rows:
            for n in r["characters"]:
                if n not in names:
                    names.append(n)
        chars = [{"ad": n, "aciklama": f"{n}, distinctive consistent look, cinematic portrait"} for n in names]
        if any(r["type"] == "narrator" for r in rows):
            chars.append({"ad": "Anlatıcı", "anlatici": True})
        scenes = [{"sahne_no": i, "tur": TYPE_TR[r["type"]], "karakterler": r["characters"], "replik": r["dialogue"],
                   "gorsel_prompt": r["image_prompt"] or f"Cinematic shot for scene {i}",
                   "video_prompt": r["video_prompt"] or "Slow cinematic camera move", "sure": r["duration"],
                   "sesli_an": r["audio_moment"]} for i, r in enumerate(rows, 1)]
        return lang, chars, scenes
    english = lang == "en"
    chars = [{"ad": c["name"] if not (english and c.get("is_narrator")) else "Narrator", "aciklama": c.get("description", ""),
              **({"anlatici": True} if c.get("is_narrator") else {})} for c in CHARACTERS]
    scenes = [{"sahne_no": i, "tur": TYPE_TR[d["type"]], "karakterler": d["characters"],
               "replik": d["en"] if english else d["dialogue"], "gorsel_prompt": d["image_prompt"],
               "video_prompt": d["video_prompt"], "sure": d["duration"], "sesli_an": d["audio_moment"]}
              for i, d in enumerate(SCENES, 1)]
    return lang, chars, scenes


def _translate(text: str, target: str) -> str:
    for d in SCENES:
        if d["dialogue"] == text and target == "en":
            return d["en"]
        if d["en"] == text and target == "tr":
            return d["dialogue"]
    return f"[{target}] {text}"


WANTED_LANGS = (("en", ("ingilizce", "english")), ("de", ("almanca", "german", "deutsch")), ("ar", ("arapça", "arapca", "arabic")),
                ("es", ("ispanyolca", "spanish")), ("fr", ("fransızca", "fransizca", "french")), ("tr", ("türkçe", "turkce", "turkish")))


class MockDirector:
    mode = "mock"

    async def complete(self, model: str, system: str, tools: list[dict], messages: list[dict],
                       on_text: Callable[[str], None] = lambda t: None) -> LLMResult:
        text, calls = self.decide(messages)
        if text:
            for i in range(0, len(text), 24):
                on_text(text[i:i + 24])
        content = ([{"type": "text", "text": text}] if text else []) + calls
        return LLMResult(content=content, stop_reason="tool_use" if calls else "end_turn", usage={}, model="mock-yonetmen")

    async def text(self, model: str, system: str, prompt: str) -> str:
        return ""

    # ------------------------------------------------------------------ karar kuralları
    def decide(self, messages: list[dict]) -> tuple[str, list[dict]]:
        hist = _history(messages)
        users = _user_texts(messages)
        first = users[0] if users else ""
        latest = users[-1] if users else ""
        names = [h["name"] for h in hist]
        turns = sum(1 for m in messages if m["role"] == "assistant")
        if turns > STOP_AFTER_TURNS:
            return "Çok uzun sürdü; durduruyorum. Durumu bölüm ekranından kontrol edin.", []
        last = hist[-1] if hist else None
        last_turn = [h for h in hist if last and h["turn"] == last["turn"]]

        def result_of(name: str):
            return next((h["result"] for h in reversed(hist) if h["name"] == name and not h["error"]), None)

        # plan onayı istenmişse bir kez iste
        if last and last["error"] and "Plan onayı gerekli" in str(last["result"]) and "plan_onayi_iste" not in names:
            return "Üretimden önce planı onayınıza sunuyorum.", [_call("plan_onayi_iste", ozet="Sahne listesi ve tahmini maliyet aşağıda.")]
        if last and last["name"] == "plan_onayi_iste":
            if "onay" not in _low(str(last["result"])):
                return "Plan onaylanmadı; üretimi başlatmıyorum.", []

        lang, chars, scenes = _scenario(first)
        extra = [code for code, keys in WANTED_LANGS if code != lang and any(k in _low(" ".join(users)) for k in keys)]

        # ---- kurulum
        if "baglanti_durumu" not in names:
            return "Önce bağlantılarınıza bakıyorum.", [_call("baglanti_durumu")]
        if "proje_olustur" not in names:
            demo = _dict(result_of("baglanti_durumu")).get("deneme_modundaki_adimlar") or []
            note = (f"Şu adımlar deneme modunda (sahte çıktı) kalacak: {', '.join(demo)}. Gerçek üretim için Ayarlar > Bağlantılar'dan "
                    "servisleri bağlayabilirsiniz. " if demo else "")
            title = (re.sub(r"<senaryo_dosyasi.*?</senaryo_dosyasi>", "", first, flags=re.S).strip().splitlines() or ["Yeni bölüm"])[0][:60]
            return (note + "Fikri sahnelere ayırıp projeyi kuruyorum.",
                    [_call("proje_olustur", ad=title or "Yeni bölüm", aciklama="Sohbetten oluşturuldu", dil=lang,
                           gorsel_uslup="cinematic, warm natural light, consistent character design")])
        if "karakter_olustur" not in names:
            return "", [_call("karakter_olustur", **c) for c in chars]
        if "ses_listele" not in names:
            return "Karakterlere ses seçiyorum.", [_call("ses_listele", dil=lang)]
        if "karaktere_ses_ata" not in names:
            voices = _dict(result_of("ses_listele")).get("sesler") or [{"ses_kimligi": ""}]
            ids = [h["result"]["karakter_id"] for h in hist if h["name"] == "karakter_olustur" and not h["error"]]
            if not ids:  # yeniden başlatma sonrası: karakterler zaten var
                ids = [ch["id"] for ch in _dict(result_of("durum_getir")).get("karakterler", [])]
                if not ids:
                    return "", [_call("durum_getir")]
            return "", [_call("karaktere_ses_ata", karakter_id=cid, ses_kimligi=voices[k % len(voices)]["ses_kimligi"] or "mock-ses")
                        for k, cid in enumerate(ids)]
        if "bolum_olustur" not in names:
            return "", [_call("bolum_olustur", baslik="Bölüm 1", diller=[lang, *extra])]
        if "sahneleri_yaz" not in names:
            return f"{len(scenes)} sahneyi yazıyorum.", [_call("sahneleri_yaz", sahneler=scenes)]

        # ---- müdahaleler ve çeviri
        translated = {h["input"].get("dil") for h in hist if h["name"] == "replik_cevir" and not h["error"]}
        for code in extra:
            if code not in translated:
                items = [{"sahne_no": sc["sahne_no"], "metin": _translate(sc["replik"], code)} for sc in scenes if sc["replik"]]
                added = {h["input"].get("dil") for h in hist if h["name"] == "dil_ekle"}
                calls = ([] if code in added else [_call("dil_ekle", dil=code)]) + [_call("replik_cevir", dil=code, ceviriler=items)]
                return f"{languages.name(code)} sürümü için replikleri çeviriyorum.", calls
        m = re.search(r"(\d+)\s*\.?\s*sahne", _low(latest))
        if len(users) > 1 and m and not any(h["name"] == "duzeltme_baslat" and h["input"].get("talimat") == latest for h in hist):
            return (f"{m.group(1)}. sahne için düzeltme başlatıyorum.",
                    [_call("duzeltme_baslat", sahne_no=int(m.group(1)), yontem="omni", talimat=latest)])

        # ---- üretim döngüsü: önce güncel durum, sonra tek bir eylem
        if not (len(last_turn) == 1 and last["name"] == "durum_getir" and not last["error"]):
            return "", [_call("durum_getir")]
        st = _dict(last["result"])
        per = st.get("per_language") or {}
        if st.get("failed_jobs"):
            f = st["failed_jobs"][0]
            return f"Bir iş hata verdi ve devam edemiyorum: {f['kind']} (sahne {f.get('scene_no')}): {f['error']}", []
        if st.get("active_jobs"):
            waiting = [j for j in st["active_jobs"] if j["status"] != "waiting_manual"]
            if waiting:
                return "", [_call("isleri_bekle")]
        pend = st.get("pending_approval") or []
        if pend:
            images = [p for p in pend if p["role"] in ("character_main", "scene_image")]
            if images and "varlik_incele" not in names:
                return "Görsellere bakıyorum.", [_call("varlik_incele", varlik_id=images[0]["asset_id"])]
            return "Çıktılar uygun; onaylıyorum.", [_call("varlik_onayla", varlik_idleri=[p["asset_id"] for p in pend])]
        prev_action = next((h for h in reversed(hist) if h["name"] not in ("durum_getir", "isleri_bekle", "varlik_onayla", "varlik_incele")), None)
        for n in (1, 2, 3, 4):
            if any(not next(x for x in v["steps"] if x["n"] == n)["complete"] for v in per.values()):
                if (prev_action and prev_action["name"] == "adim_calistir" and prev_action["input"].get("adim") == n
                        and not prev_action["error"] and _dict(prev_action["result"]).get("baslatilan_is") == 0):
                    why = "; ".join(f"{k['who']}: {k['reason']}" for k in _dict(prev_action["result"]).get("atlanan", []))
                    return f"{n}. adım ilerleyemiyor: {why or 'eksik varlık var'}", []
                title = {1: "Sesleri", 2: "Görselleri", 3: "Konuşan sahneleri", 4: "Konuşmasız sahneleri"}[n]
                return f"{title} üretiyorum.", [_call("adim_calistir", adim=n, kapsam="eksikler", dil="*")]
        for l, v in per.items():
            if not v["final"] or not v["final"]["fresh"]:
                return f"{languages.name(l)} kurgusunu yapıyorum.", [_call("kurgu_yap", dil=l)]
        for l, v in per.items():
            if not v["report"] or not v["report"]["for_current_final"]:
                return f"{languages.name(l)} kalite kontrolünü çalıştırıyorum.", [_call("kontrol_calistir", dil=l)]
        src = st.get("source_language") or lang
        if "kontrol_raporu_getir" not in names:
            return "Kalite raporunu okuyorum.", [_call("kontrol_raporu_getir", dil=src)]
        if "duzeltme_baslat" not in names:
            # bir düzeltme turu: rapordaki ilk uyarı; yoksa ilk konuşmasız sahnede küçük bir iyileştirme
            rep = _dict(result_of("kontrol_raporu_getir"))
            warn = next((f for f in rep.get("bulgular", []) if f.get("scene_no")), None)
            target = warn["scene_no"] if warn else next((sc["no"] for sc in st.get("scenes", []) if sc["type"] == "konuşmasız"), 1)
            method = "dudak_senkronu" if warn and warn.get("suggest") == "lipsync" else "omni"
            call = _call("duzeltme_baslat", sahne_no=target, yontem=method, dil=src,
                         **({"talimat": (warn or {}).get("message") or "Renkleri biraz daha sıcak yap"} if method == "omni" else {}))
            return f"Bir düzeltme turu yapıyorum (sahne {target}).", [call]
        if "maliyet_getir" not in names or (prev_action and prev_action["name"] != "maliyet_getir"):
            return "", [_call("maliyet_getir")]
        langs = ", ".join(languages.name(l) for l in per)
        review = [str(sc["no"]) for sc in st.get("scenes", []) if sc.get("needs_review")]
        return (f"Bölüm hazır. Diller: {langs}. Tüm sahneler üretildi, kurgu ve kalite kontrolü tamamlandı, bir düzeltme turu yapıldı."
                + (f" Elle bakılması gereken sahneler: {', '.join(review)}." if review else "")
                + " Bu çalışma sahte yönetmenle (mock) yapıldı; gerçek Claude için Ayarlar'dan Anthropic anahtarı girin."), []
