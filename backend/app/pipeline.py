"""Üretim hattı: 7 sabit adım, dil kuralları, bağımlılıklar, onay ve otomatik mod.

1 Sesler · 2 Görseller · 3 Konuşan sahneler · 4 Konuşmasız sahneler · 5 Kurgu · 6 Kontrol · 7 Düzeltme

Dil kuralları
- Ses ve konuşan sahne videosu DİLE BAĞLIDIR; karakter görselleri ve konuşmasız sahneler diller
  arasında ortaktır (sahnede "görseldeki metin" varsa o sahnenin görseli ve videosu da dile bağlanır).
- Konuşan video her zaman o dilin sesinden üretilir; kurgu bunu ayrıca doğrular.
- Konuşan sahne asla Kling video adımına gitmez; yalnızca HeyGen.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from . import connections, crypto, languages, settings_store as store, storage, tenancy
from .config import get_settings
from .events import bus
from .models import (AgentRun, Asset, Character, Episode, Job, Project, QCReport, Scene, SceneText, Selection, now)
from .providers import registry

STEP_TITLES = {1: "Sesler", 2: "Görseller", 3: "Konuşan sahneler", 4: "Konuşmasız sahneler",
               5: "Kurgu ve miksaj", 6: "Kontrol", 7: "Düzeltme"}
ACTIVE = ("queued", "submitting", "running", "waiting_manual", "blocked")
ROLES = ("voice", "scene_image", "scene_video")
KIND_ROLE = {"voice": "voice", "character_image": "character_main", "scene_image": "scene_image",
             "talking_video": "scene_video", "silent_video": "scene_video",
             "omni_fix": "scene_video", "lipsync_fix": "scene_video"}
VIDEO_KINDS = ("talking_video", "silent_video", "omni_fix", "lipsync_fix")
DEFAULT_EP_SETTINGS = {"transition": "fade", "transition_dur": 0.3, "subtitles": True, "burn_subtitles": True,
                       "vertical": False, "music_volume": 0.25, "ducking": True}
# Konuşan sahne görsellerine otomatik eklenen dudak senkronu şartları
LIPSYNC_PROMPT = (" Framing for lip sync: the speaking character faces the camera (frontal or at most a slight angle), "
                  "the face is large in the frame, the mouth is clearly visible, closed and neutral, and nothing covers "
                  "the mouth (no hand, microphone, hair or object).")

# İşlemi kimin yaptığı (onay kaydı için) ve hangi ajan çalışmasına ait olduğu (bütçe için)
ACTOR: ContextVar[str] = ContextVar("actor", default="user")
RUN_ID: ContextVar[int | None] = ContextVar("run_id", default=None)


class StepBlocked(Exception):
    """Adımın bağımlılıkları hazır değil."""


class BudgetExceeded(Exception):
    """Ajan çalışmasının bütçe sınırı aşılacak."""


# --- olaylar: yalnızca commit'ten sonra yayınla -------------------------------
def emit(s: Session, type_: str, **payload) -> None:
    """Olay yalnızca sahibi olan kullanıcıya gider; sahibi geçerli kullanıcı bağlamından alınır."""
    payload.setdefault("user_id", tenancy.uid())
    s.info.setdefault("events", []).append((type_, payload))


@event.listens_for(Session, "after_commit")
def _flush_events(session: Session) -> None:
    for type_, payload in session.info.pop("events", []):
        bus.publish(type_, **payload)


@event.listens_for(Session, "after_rollback")
def _drop_events(session: Session) -> None:
    session.info.pop("events", None)


# --- dil yardımcıları ---------------------------------------------------------
def ep_settings(ep: Episode) -> dict:
    return {**DEFAULT_EP_SETTINGS, **(ep.settings or {})}


def source_lang(s: Session, ep: Episode) -> str:
    return ep.language or s.get(Project, ep.project_id).default_language or "tr"


def ep_langs(s: Session, ep: Episode) -> list[str]:
    """Bölümün hedef dilleri; kaynak dil her zaman ilk sıradadır."""
    src = source_lang(s, ep)
    return [src, *[l for l in (ep.languages or []) if l and l != src]]


def effective_lang(s: Session, ep: Episode, sc: Scene, lang: str) -> str:
    """Sahne açıkça başka bir dile sabitlenmişse (sahne düzeyinde ezme) her çıktıda o dilde kalır."""
    if sc.language and sc.language != source_lang(s, ep):
        return sc.language
    return lang


def text_row(s: Session, sc: Scene, lang: str) -> SceneText | None:
    return next((t for t in sc.texts if t.language == lang), None)


def text_of(s: Session, sc: Scene, lang: str) -> str:
    row = text_row(s, sc, lang)
    return (row.text if row else "").strip()


def text_hash(text: str) -> str:
    return hashlib.md5((text or "").strip().encode("utf-8")).hexdigest()[:12]


def set_text(s: Session, ep: Episode, sc: Scene, lang: str, text: str, *, status: str | None = None,
             origin: str = "human", detect: bool = True) -> SceneText:
    """Sahne metnini yazar. Kaynak dil metni kendiliğinden onaylıdır; çeviri taslak başlar."""
    row = text_row(s, sc, lang)
    if row is None:
        row = SceneText(scene_id=sc.id, language=lang, text="")
        sc.texts.append(row)
    text = (text or "").strip()
    changed = text != (row.text or "")
    row.text = text
    if changed:
        row.origin = origin
        if detect and text:
            d = languages.detect(text, lang)
            row.detected_language, row.confidence = d["detected"], d["confidence"]
    is_source = lang == (sc.language or source_lang(s, ep))
    if status:
        row.status = status
    elif is_source:
        row.status = "approved"
    elif changed:
        row.status = "draft"
    s.flush()
    return row


def image_lang(sc: Scene, lang: str) -> str:
    return lang if any((v or "").strip() for v in (sc.image_text or {}).values()) else ""


def video_lang(sc: Scene, lang: str) -> str:
    return lang if sc.type == "talking" or image_lang(sc, lang) else ""


def role_lang(sc: Scene, role: str, lang: str) -> str:
    if role == "voice":
        return lang
    return image_lang(sc, lang) if role == "scene_image" else video_lang(sc, lang)


def asset(s: Session, asset_id: int | None) -> Asset | None:
    return s.get(Asset, asset_id) if asset_id else None


def sel(s: Session, sc: Scene, role: str, lang: str) -> Asset | None:
    """Sahnenin bu dil için seçili (onaylı) sürümü."""
    key = role_lang(sc, role, lang)
    row = next((x for x in sc.selections if x.role == role and x.language == key), None)
    return asset(s, row.asset_id) if row else None


def set_sel(s: Session, sc: Scene, role: str, language: str, asset_id: int | None) -> None:
    row = next((x for x in sc.selections if x.role == role and x.language == language), None)
    if asset_id is None:
        if row is not None:
            sc.selections.remove(row)
    elif row is None:
        sc.selections.append(Selection(scene_id=sc.id, language=language, role=role, asset_id=asset_id))
    else:
        row.asset_id = asset_id
    s.flush()


def narrator_of(s: Session, project_id: int) -> Character | None:
    return s.scalars(select(Character).where(Character.project_id == project_id,
                                             Character.is_narrator.is_(True))).first()


def scene_chars(s: Session, sc: Scene) -> list[Character]:
    out = []
    for cid in sc.character_ids or []:
        c = s.get(Character, cid)
        if c is not None:
            out.append(c)
    return out


def speaker(s: Session, sc: Scene, project_id: int) -> Character | None:
    chars = scene_chars(s, sc)
    if sc.type == "narrator":
        return narrator_of(s, project_id) or (chars[0] if chars else None)
    return chars[0] if chars else None


def voice_for(ch: Character, lang: str) -> tuple[str, dict, bool]:
    """(ses kimliği, ses ayarları, dile özel mi). Dile özel ses yoksa karakterin varsayılan sesi."""
    v = (ch.voices or {}).get(lang) or (ch.voices or {}).get(languages.base(lang)) or {}
    if v.get("voice_id"):
        return v["voice_id"], v.get("voice_settings") or ch.voice_settings or {}, True
    return ch.voice_id or "", ch.voice_settings or {}, False


def apply_pronunciation(proj: Project, lang: str, text: str) -> str:
    """Dil bazlı telaffuz sözlüğü: sayı, tarih, kısaltma gibi yazılışları okunuşlarıyla değiştirir."""
    conf = (proj.pronunciations or {}).get(lang) or (proj.pronunciations or {}).get(languages.base(lang)) or {}
    for pair in conf.get("rules") or []:
        if len(pair) == 2 and pair[0]:
            text = re.sub(rf"(?<!\w){re.escape(str(pair[0]))}(?!\w)", str(pair[1]), text, flags=re.IGNORECASE)
    return text


def needs_audio(sc: Scene) -> bool:
    return sc.type in ("talking", "narrator")


def active_job(s: Session, *, scene_id: int | None = None, character_id: int | None = None,
               kinds: tuple[str, ...] = (), language: str | None = None) -> Job | None:
    q = select(Job).where(Job.status.in_(ACTIVE))
    if scene_id is not None:
        q = q.where(Job.scene_id == scene_id)
    if character_id is not None:
        q = q.where(Job.character_id == character_id, Job.scene_id.is_(None))
    if kinds:
        q = q.where(Job.kind.in_(kinds))
    if language is not None:
        q = q.where(Job.language == language)
    return s.scalars(q).first()


def has_draft(s: Session, role: str, *, scene_id: int | None = None, character_id: int | None = None,
              language: str = "") -> Asset | None:
    q = select(Asset).where(Asset.role == role, Asset.status == "draft").order_by(Asset.id.desc())
    if scene_id is not None:
        q = q.where(Asset.scene_id == scene_id, Asset.language == language)
    else:
        q = q.where(Asset.character_id == character_id, Asset.scene_id.is_(None))
    return s.scalars(q).first()


def fresh_url(a: Asset, provider: str) -> str:
    """Varlığın sağlayıcıdaki uzak adresi (Omni/dudak senkronu video girdisi yalnızca URL alır)."""
    url = (a.meta or {}).get("source_url")
    hours = registry.cfg(provider).get("remote_url_max_age_hours", 72)
    if url and a.created_at and now() - a.created_at < timedelta(hours=hours):
        return url
    # sağlayıcı adresi yok ya da eskimiş: S3 uyumlu depolama varsa kısa ömürlü imzalı adres üret
    return storage.signed_url(a.path, 3600)


# --- güncellik (stale) --------------------------------------------------------
def voice_stale(s: Session, sc: Scene, lang: str) -> bool:
    a = sel(s, sc, "voice", lang)
    return bool(a and (a.params or {}).get("text_hash") not in (None, text_hash(text_of(s, sc, lang))))


def image_stale(s: Session, sc: Scene, lang: str) -> bool:
    a = sel(s, sc, "scene_image", lang)
    if a is None or not image_lang(sc, lang):
        return False
    return (a.params or {}).get("image_text", "") != ((sc.image_text or {}).get(lang) or "").strip()


def video_stale(s: Session, sc: Scene, lang: str) -> str:
    """Seçili video eski girdilerle üretildiyse nedenini döndürür."""
    v = sel(s, sc, "scene_video", lang)
    if v is None:
        return ""
    src = (v.params or {}).get("src") or {}
    img = sel(s, sc, "scene_image", lang)
    if src.get("image_id") and img is not None and src["image_id"] != img.id:
        return "Video eski görselle üretilmiş"
    if sc.type == "talking":
        aud = sel(s, sc, "voice", lang)
        if src.get("audio_id") and aud is not None and src["audio_id"] != aud.id:
            return "Video eski sesle üretilmiş"
        if voice_stale(s, sc, lang):
            return "Replik değişti: ses ve video güncel değil"
    return ""


# --- iş kuyruğa alma ----------------------------------------------------------
def run_spent(s: Session, run_id: int) -> float:
    """Ajan çalışmasının harcaması: sağlayıcı işleri (tahmini) + Claude API."""
    q = select(func.sum(Job.cost_usd)).where(Job.run_id == run_id, Job.status.notin_(("failed", "canceled")))
    return float(s.execute(q).scalar() or 0.0)


def enqueue(s: Session, *, kind: str, provider: str, step: int, ep: Episode | None, project_id: int,
            scene: Scene | None = None, character: Character | None = None, inp: dict, language: str = "",
            auto_approve: bool = False, is_fix: bool = False) -> Job:
    """İşi kuyruğa alır. İş, PROJENİN SAHİBİ olan kullanıcının anahtarıyla ve onun sınırlarıyla yürür."""
    owner = s.get(Project, project_id).user_id
    token, source = "", ""
    with tenancy.as_user(owner):
        if provider == "local":
            mode, usd, units, unit = "local", 0.0, 0.0, ""
        else:
            mode = registry.resolve_mode(provider, inp)
            c = registry.estimate(provider, inp)
            usd, units, unit = c.usd, c.units, c.unit
            if mode == "real":
                source = store.cred_source(connections.PROVIDER_SERVICE[provider])
                base = get_settings().public_base_url
                if base.lower().startswith("https://"):
                    # sağlayıcı webhook'u: tahmin edilemez adres; içerik yine sağlayıcıdan sorgulanarak doğrulanır
                    token = crypto.new_token(24)
                    inp = {**inp, "callback_url": f"{base.rstrip('/')}/api/webhooks/{token}"}
    if source == "shared" and owner is not None:
        limit, spent = connections.shared_limit(s, owner), connections.shared_spent(s, owner)
        if limit > 0 and spent + usd > limit:
            raise BudgetExceeded(f"Ortak (kurum) anahtarının aylık sınırı doldu: {spent:.2f} $ / {limit:.2f} $. "
                                 "Kendi anahtarınızı bağlayın ya da yöneticiye başvurun.")
    run_id = RUN_ID.get()
    if run_id is not None:
        run = s.get(AgentRun, run_id)
        if run is not None and run.budget_usd > 0 and run_spent(s, run_id) + usd > run.budget_usd:
            raise BudgetExceeded(f"Bütçe sınırı ({run.budget_usd:.2f} $) aşılacak: harcanan "
                                 f"{run_spent(s, run_id):.2f} $, bu iş yaklaşık {usd:.2f} $")
    job = Job(kind=kind, provider=provider, mode=mode, step=step, project_id=project_id, user_id=owner,
              episode_id=ep.id if ep else None, scene_id=scene.id if scene else None,
              character_id=character.id if character else None, input=inp, language=language, run_id=run_id,
              cost_usd=usd, cost_units=units, cost_unit=unit, auto_approve=auto_approve, is_fix=is_fix,
              cred_source=source, webhook_token=token)
    s.add(job)
    s.flush()
    emit(s, "job", id=job.id, status=job.status, episode_id=job.episode_id, scene_id=job.scene_id, user_id=owner)
    return job


def _auto(ep: Episode | None, sc: Scene | None = None) -> bool:
    return bool((ep and ep.auto_state == "running") or (sc and sc.chain))


# --- iş kurucular: Job ya da engel nedeni (str) döner -------------------------
def voice_job(s: Session, ep: Episode, sc: Scene, lang: str, is_fix: bool = False) -> Job | str:
    el = effective_lang(s, ep, sc, lang)
    row = text_row(s, sc, el)
    text = (row.text if row else "").strip()
    if not text:
        return f"{languages.name(el)} replik/anlatıcı metni boş"
    if row.status != "approved":
        return f"{languages.name(el)} çevirisi onaylanmamış"
    ch = speaker(s, sc, ep.project_id)
    if ch is None:
        return "anlatıcı karakteri tanımlı değil" if sc.type == "narrator" else "sahneye karakter atanmamış"
    ok, why = languages.support("elevenlabs_tts", el)
    if not ok:
        return why
    voice_id, settings, per_lang = voice_for(ch, el)
    if registry.resolve_mode("elevenlabs_tts") != "mock" and not voice_id:
        return f"{ch.name} için ElevenLabs ses kimliği girilmemiş"
    # dili destekleyen model: seçili model desteklemiyorsa destekleyen bir modele geçilir (dil değişmez)
    model = languages.tts_model_for(el, store.model_for("elevenlabs_tts"))
    if model is None:
        return f"ElevenLabs'te {languages.name(el)} destekleyen model yok"
    proj = s.get(Project, ep.project_id)
    pron = (proj.pronunciations or {}).get(el) or {}
    inp = {"text": apply_pronunciation(proj, el, text), "original_text": text, "text_hash": text_hash(text),
           "voice_id": voice_id, "voice_settings": settings, "language": el, "language_code": languages.base(el),
           "model": model, "voice_per_language": per_lang, "label": f"Sahne {sc.order_no} · {ch.name} · {el}",
           "speaker_id": ch.id}
    if pron.get("dictionary_id"):
        inp["pronunciation_locators"] = [{"pronunciation_dictionary_id": pron["dictionary_id"],
                                          **({"version_id": pron["version_id"]} if pron.get("version_id") else {})}]
    return enqueue(s, kind="voice", provider="elevenlabs_tts", step=7 if is_fix else 1, ep=ep,
                   project_id=ep.project_id, scene=sc, inp=inp, language=el, auto_approve=_auto(ep, sc), is_fix=is_fix)


def character_image_job(s: Session, ch: Character, ep: Episode | None = None) -> Job | str:
    if not (ch.description or "").strip():
        return f"{ch.name}: karakter açıklaması boş"
    proj = s.get(Project, ch.project_id)
    refs = s.scalars(select(Asset).where(Asset.character_id == ch.id, Asset.role == "character_ref")).all()
    prompt = f"Character portrait of {ch.name}: {ch.description}".strip()
    if proj.style:
        prompt += f" Style: {proj.style}"
    inp = {"prompt": prompt, "ref_image_paths": [a.path for a in refs], "aspect_ratio": "1:1", "label": ch.name}
    return enqueue(s, kind="character_image", provider="kling_image", step=2, ep=ep, project_id=ch.project_id,
                   character=ch, inp=inp, auto_approve=_auto(ep))


def scene_image_job(s: Session, ep: Episode, sc: Scene, lang: str, is_fix: bool = False) -> Job | str:
    el = effective_lang(s, ep, sc, lang)
    prompt = (sc.image_prompt or "").strip()
    if not prompt:
        return "görsel prompt boş"
    refs, ref_ids = [], []
    for ch in scene_chars(s, sc):
        a = asset(s, ch.main_image_id)
        if a is None or a.status != "approved":
            return f"{ch.name} karakterinin ana görseli onaylı değil"
        refs.append(a.path)
        ref_ids.append(a.id)
    proj = s.get(Project, ep.project_id)
    if proj.style:
        prompt += f" Style: {proj.style}"
    if sc.type == "talking":
        prompt += LIPSYNC_PROMPT  # konuşan sahne görselleri dudak senkronuna uygun üretilir
    il = image_lang(sc, el)
    visible = ((sc.image_text or {}).get(el) or "").strip() if il else ""
    if il and not visible:
        return f"görseldeki metin {languages.name(el)} için girilmemiş"
    if visible:
        prompt += f' The image contains this visible text, written exactly: "{visible}".'
    inp = {"prompt": prompt, "ref_image_paths": refs, "aspect_ratio": "16:9", "label": f"Sahne {sc.order_no}",
           "image_text": visible, "src": {"character_images": ref_ids}}
    return enqueue(s, kind="scene_image", provider="kling_image", step=7 if is_fix else 2, ep=ep,
                   project_id=ep.project_id, scene=sc, inp=inp, language=il, auto_approve=_auto(ep, sc), is_fix=is_fix)


def video_job(s: Session, ep: Episode, sc: Scene, lang: str, is_fix: bool = False) -> Job | str:
    el = effective_lang(s, ep, sc, lang)
    img = sel(s, sc, "scene_image", el)
    if img is None or img.status != "approved":
        return "sahne görseli onaylı değil"
    label = f"Sahne {sc.order_no}"
    if sc.type == "talking":
        # Konuşan sahne yalnızca HeyGen ile ve o dilin sesinden üretilir
        aud = sel(s, sc, "voice", el)
        if aud is None or aud.status != "approved":
            return f"{languages.name(el)} sesi onaylı değil"
        if aud.language != el:
            return f"seçili ses {aud.language} dilinde; {el} videosu başka dilin sesiyle üretilemez"
        if voice_stale(s, sc, el):
            return "replik değişmiş: önce sesi yeniden üretip onaylayın"
        ok, why = languages.support("heygen_avatar", el)
        if not ok:
            return why
        inp = {"image_path": img.path, "audio_path": aud.path, "audio_duration": (aud.meta or {}).get("duration", 0),
               "prompt": sc.video_prompt or "", "label": f"{label} · {el}", "language": el,
               "idempotency_key": uuid.uuid4().hex, "src": {"image_id": img.id, "audio_id": aud.id}}
        return enqueue(s, kind="talking_video", provider="heygen_avatar", step=7 if is_fix else 3, ep=ep,
                       project_id=ep.project_id, scene=sc, inp=inp, language=el, auto_approve=_auto(ep, sc),
                       is_fix=is_fix)
    duration = float(sc.duration or 5)
    if sc.type == "narrator":
        # video diller arasında ortak: en uzun anlatıcı sesine göre süre verilir
        for l in ep_langs(s, ep):
            a = sel(s, sc, "voice", effective_lang(s, ep, sc, l))
            if a is not None:
                duration = max(duration, float((a.meta or {}).get("duration", 0)) + 0.5)
    inp = {"image_path": img.path, "prompt": (sc.video_prompt or sc.image_prompt or "").strip(),
           "duration": duration, "native_audio": bool(sc.audio_moment), "label": label, "src": {"image_id": img.id}}
    if not inp["prompt"]:
        return "video prompt boş"
    return enqueue(s, kind="silent_video", provider="kling_video", step=7 if is_fix else 4, ep=ep,
                   project_id=ep.project_id, scene=sc, inp=inp, language=video_lang(sc, el),
                   auto_approve=_auto(ep, sc), is_fix=is_fix)


def episode_characters(s: Session, ep: Episode) -> list[Character]:
    """Bölümde görseli gereken karakterler (sahnelerde geçenler)."""
    seen: dict[int, Character] = {}
    for sc in ep.scenes:
        for ch in scene_chars(s, sc):
            seen.setdefault(ch.id, ch)
    return list(seen.values())


def language_warnings(s: Session, ep: Episode, lang: str) -> list[str]:
    """Üretim başlamadan gösterilecek dil uyarıları (desteklemeyen sağlayıcı, model değişimi)."""
    out = []
    if not languages.known(lang):
        return [f"'{lang}' dili config/languages.yaml içinde tanımlı değil"]
    ok, why = languages.support("elevenlabs_tts", lang)
    if not ok:
        out.append(why)
    else:
        chosen = store.model_for("elevenlabs_tts")
        use = languages.tts_model_for(lang, chosen)
        if use and use != chosen:
            out.append(f"ElevenLabs {chosen} modeli {languages.name(lang)} desteklemiyor; bu dilde {use} kullanılacak")
    for prov, label in (("heygen_avatar", "HeyGen"), ("kling_lipsync", "Kling dudak senkronu")):
        ok, why = languages.support(prov, lang)
        if not ok:
            out.append(why)
    return out


# --- adım çalıştırma ----------------------------------------------------------
def run_step(s: Session, ep: Episode, step: int, mode: str = "missing", scene_id: int | None = None,
             lang: str = "*") -> dict:
    """mode: 'all' (tümünü üret) | 'missing' (yalnız eksikler ve güncel olmayanlar).

    scene_id verilirse tek sahne. lang: dil kodu ya da '*' (tüm hedef diller). Dilden bağımsız
    varlıklar (karakter görselleri, konuşmasız sahneler) diller arasında bir kez üretilir.
    """
    created: list[int] = []
    skipped: list[dict] = []
    done: set[tuple] = set()
    scenes = [sc for sc in ep.scenes if scene_id is None or sc.id == scene_id]
    langs = ep_langs(s, ep) if lang in ("*", "", None) else [lang]
    for l in langs:
        if l not in ep_langs(s, ep):
            raise StepBlocked(f"{l} bu bölümün hedef dilleri arasında değil")

    def handle(res, who: str):
        if isinstance(res, Job):
            created.append(res.id)
        elif not any(k["who"] == who and k["reason"] == res for k in skipped):
            skipped.append({"who": who, "reason": res})

    def want(key: tuple, selected, stale: bool, role: str, *, sc: Scene | None = None, ch: Character | None = None,
             kinds=(), language: str = "") -> bool:
        if key in done:
            return False
        done.add(key)
        if active_job(s, scene_id=sc.id if sc else None, character_id=ch.id if ch else None, kinds=kinds,
                      language=language if sc else None):
            return False
        if mode == "all" or scene_id is not None:
            return True
        if selected is not None and not stale:
            return False
        return not has_draft(s, role, scene_id=sc.id if sc else None, character_id=ch.id if ch else None,
                             language=language)

    if step == 5:
        for l in langs:
            created.append(enqueue_montage(s, ep, l).id)
        return {"created": created, "skipped": skipped}
    if step == 6:
        for l in langs:
            created.append(enqueue_qc(s, ep, l).id)
        return {"created": created, "skipped": skipped}
    if step not in (1, 2, 3, 4):
        raise StepBlocked("Düzeltme adımı sahne üzerinden başlatılır")

    if step == 2 and scene_id is None:
        for ch in episode_characters(s, ep):
            if want(("char", ch.id), asset(s, ch.main_image_id), False, "character_main", ch=ch, kinds=("character_image",)):
                handle(character_image_job(s, ch, ep), ch.name)
    for l in langs:
        for sc in scenes:
            el = effective_lang(s, ep, sc, l)
            who = f"Sahne {sc.order_no}" + (f" ({el})" if len(langs) > 1 or el != l else "")
            if step == 1 and needs_audio(sc):
                if want(("voice", sc.id, el), sel(s, sc, "voice", el), voice_stale(s, sc, el), "voice", sc=sc,
                        kinds=("voice",), language=el):
                    handle(voice_job(s, ep, sc, l), who)
            elif step == 2:
                il = image_lang(sc, el)
                if want(("img", sc.id, il), sel(s, sc, "scene_image", el), image_stale(s, sc, el), "scene_image",
                        sc=sc, kinds=("scene_image",), language=il):
                    handle(scene_image_job(s, ep, sc, l), who if il else f"Sahne {sc.order_no}")
            elif step in (3, 4) and (sc.type == "talking") == (step == 3):
                vl = video_lang(sc, el)
                if want(("vid", sc.id, vl), sel(s, sc, "scene_video", el), bool(video_stale(s, sc, el)), "scene_video",
                        sc=sc, kinds=VIDEO_KINDS, language=vl):
                    handle(video_job(s, ep, sc, l), who if vl else f"Sahne {sc.order_no}")
    return {"created": created, "skipped": skipped}


def montage_blockers(s: Session, ep: Episode, lang: str) -> list[str]:
    """Kurgu öncesi doğrulama: eksik varlıklar ve DİL EŞLEŞMESİ (bir dilin videosu başka dilin sesiyle eşleşemez)."""
    out = []
    if not ep.scenes:
        out.append("Bölümde sahne yok")
    for sc in ep.scenes:
        el = effective_lang(s, ep, sc, lang)
        v = sel(s, sc, "scene_video", el)
        if v is None or v.status != "approved":
            out.append(f"Sahne {sc.order_no}: onaylı video yok")
            continue
        a = sel(s, sc, "voice", el) if needs_audio(sc) else None
        if needs_audio(sc):
            if a is None or a.status != "approved":
                out.append(f"Sahne {sc.order_no}: onaylı {el} sesi yok")
                continue
            if a.language != el:
                out.append(f"Sahne {sc.order_no}: ses {a.language} dilinde, beklenen {el}")
            if voice_stale(s, sc, el):
                out.append(f"Sahne {sc.order_no}: replik değişmiş, {el} sesi güncel değil")
        if sc.type == "talking":
            if v.language != el:
                out.append(f"Sahne {sc.order_no}: konuşan video {v.language or 'dilsiz'}, beklenen {el}")
            src_audio = ((v.params or {}).get("src") or {}).get("audio_id")
            if src_audio and a is not None and src_audio != a.id:
                out.append(f"Sahne {sc.order_no}: konuşan video seçili {el} sesinden üretilmemiş (güncel değil)")
    return out


def latest_final(s: Session, ep_id: int, lang: str, role: str = "final") -> Asset | None:
    return s.scalars(select(Asset).where(Asset.episode_id == ep_id, Asset.role == role, Asset.language == lang)
                     .order_by(Asset.id.desc())).first()


def latest_report(s: Session, ep_id: int, lang: str) -> QCReport | None:
    return s.scalars(select(QCReport).where(QCReport.episode_id == ep_id, QCReport.language == lang)
                     .order_by(QCReport.id.desc())).first()


def enqueue_montage(s: Session, ep: Episode, lang: str, then_qc: bool = False) -> Job:
    blockers = montage_blockers(s, ep, lang)
    if blockers:
        raise StepBlocked(f"Kurgu ({lang}) başlatılamıyor: " + "; ".join(blockers))
    existing = s.scalars(select(Job).where(Job.episode_id == ep.id, Job.kind == "montage", Job.language == lang,
                                           Job.status == "queued")).first()
    if existing:
        return existing
    return enqueue(s, kind="montage", provider="local", step=5, ep=ep, project_id=ep.project_id,
                   inp={"then_qc": then_qc, "language": lang}, language=lang)


def enqueue_qc(s: Session, ep: Episode, lang: str) -> Job:
    if latest_final(s, ep.id, lang) is None:
        raise StepBlocked(f"Kontrol için önce {lang} kurgu çıktısı gerekli")
    existing = s.scalars(select(Job).where(Job.episode_id == ep.id, Job.kind == "qc", Job.language == lang,
                                           Job.status == "queued")).first()
    if existing:
        return existing
    return enqueue(s, kind="qc", provider="local", step=6, ep=ep, project_id=ep.project_id,
                   inp={"language": lang}, language=lang)


def montage_signature(s: Session, ep: Episode, lang: str) -> str:
    """Kurgunun girdileri değişti mi? (sahne sürümleri + metinler + ayarlar + müzik/efekt)"""
    parts = [lang]
    for sc in ep.scenes:
        el = effective_lang(s, ep, sc, lang)
        v, a = sel(s, sc, "scene_video", el), sel(s, sc, "voice", el)
        parts.append(f"{sc.id}:{sc.order_no}:{sc.type}:{v.id if v else 0}:{a.id if a else 0}:"
                     f"{int(sc.audio_moment)}:{text_hash(text_of(s, sc, el))}")
    extra = s.scalars(select(Asset).where(Asset.episode_id == ep.id, Asset.role.in_(("music", "sfx")))
                      .order_by(Asset.id)).all()
    parts += [f"{a.id}:{a.params}" for a in extra]
    parts.append(str(sorted(ep_settings(ep).items())))
    return "|".join(parts)


# --- onay / ret / seçim -------------------------------------------------------
def _affected_langs(s: Session, ep: Episode, sc: Scene | None, a: Asset) -> list[str]:
    out = []
    for l in ep_langs(s, ep):
        if a.language and sc is not None and effective_lang(s, ep, sc, l) != a.language:
            continue
        out.append(l)
    return out


def approve(s: Session, a: Asset, by: str | None = None) -> None:
    a.status = "approved"
    a.approved_by = by or ACTOR.get()
    sc = s.get(Scene, a.scene_id) if a.scene_id else None
    if sc is not None and a.role in ROLES:
        set_sel(s, sc, a.role, a.language, a.id)
    if a.role == "character_main" and a.character_id:
        s.get(Character, a.character_id).main_image_id = a.id
    s.flush()
    emit(s, "refresh", episode_id=a.episode_id, scene_id=a.scene_id, project_id=a.project_id)
    job = s.get(Job, a.job_id) if a.job_id else None
    ep = s.get(Episode, a.episode_id) if a.episode_id else None
    if sc is not None and sc.chain:
        advance_chain(s, ep, sc)
    elif job is not None and job.is_fix and ep is not None and ep.auto_state != "running" and RUN_ID.get() is None:
        # düzeltme onaylandı: yalnızca bu sahne, bu dil ve kurgu yeniden işlenir
        if a.role == "scene_video" or sc.type == "narrator":
            for l in _affected_langs(s, ep, sc, a):
                if latest_final(s, ep.id, l) is not None and not montage_blockers(s, ep, l):
                    enqueue_montage(s, ep, l, then_qc=True)


def reject(s: Session, a: Asset) -> None:
    a.status = "rejected"
    sc = s.get(Scene, a.scene_id) if a.scene_id else None

    def previous(**flt) -> int | None:
        q = select(Asset.id).where(Asset.role == a.role, Asset.status == "approved", Asset.id != a.id)
        for k, v in flt.items():
            q = q.where(getattr(Asset, k) == v)
        return s.execute(q.order_by(Asset.id.desc())).scalars().first()

    if sc is not None and a.role in ROLES:
        cur = next((x for x in sc.selections if x.role == a.role and x.language == a.language), None)
        if cur is not None and cur.asset_id == a.id:
            set_sel(s, sc, a.role, a.language, previous(scene_id=sc.id, language=a.language))
    if a.role == "character_main" and a.character_id:
        ch = s.get(Character, a.character_id)
        if ch.main_image_id == a.id:
            ch.main_image_id = previous(character_id=ch.id)
    emit(s, "refresh", episode_id=a.episode_id, scene_id=a.scene_id, project_id=a.project_id)


# --- "sahneyi baştan üret" zinciri -------------------------------------------
def start_chain(s: Session, ep: Episode, sc: Scene, lang: str) -> list[int]:
    el = effective_lang(s, ep, sc, lang)
    sc.chain = lang
    set_sel(s, sc, "scene_image", image_lang(sc, el), None)
    set_sel(s, sc, "scene_video", video_lang(sc, el), None)
    created = []
    try:
        if needs_audio(sc):
            set_sel(s, sc, "voice", el, None)
            r = voice_job(s, ep, sc, lang, is_fix=True)
            if not isinstance(r, Job):
                raise StepBlocked(r)
            created.append(r.id)
        r = scene_image_job(s, ep, sc, lang, is_fix=True)
        if not isinstance(r, Job):
            raise StepBlocked(r)
        created.append(r.id)
    except (StepBlocked, BudgetExceeded):
        sc.chain = ""
        raise
    return created


def advance_chain(s: Session, ep: Episode, sc: Scene) -> None:
    if not sc.chain or active_job(s, scene_id=sc.id):
        return
    lang = sc.chain
    el = effective_lang(s, ep, sc, lang)
    if sel(s, sc, "scene_video", el) is not None:
        sc.chain = ""
        if ep.auto_state != "running" and RUN_ID.get() is None:
            for l in ep_langs(s, ep):
                if latest_final(s, ep.id, l) is not None and not montage_blockers(s, ep, l):
                    enqueue_montage(s, ep, l, then_qc=True)
        return
    if sel(s, sc, "scene_image", el) is not None and (sel(s, sc, "voice", el) is not None or not needs_audio(sc)):
        try:
            r = video_job(s, ep, sc, lang, is_fix=True)
        except BudgetExceeded:
            r = "bütçe"
        if not isinstance(r, Job):
            sc.chain = ""


# --- durum özeti --------------------------------------------------------------
def step_status(s: Session, ep: Episode, lang: str) -> list[dict]:
    jobs = s.scalars(select(Job).where(Job.episode_id == ep.id, Job.kind != "claude")).all()

    def mine(j: Job) -> bool:  # bu dile ait ya da dilden bağımsız işler
        return j.language in ("", lang) or j.kind == "character_image"

    jobs = [j for j in jobs if mine(j)]
    active = [j for j in jobs if j.status in ACTIVE]
    latest: dict[tuple, Job] = {}
    for j in sorted(jobs, key=lambda j: j.id):
        latest[(j.kind, j.scene_id, j.character_id, j.language)] = j
    failed = [j for j in latest.values() if j.status == "failed"]
    drafts = s.scalars(select(Asset).where(Asset.project_id == ep.project_id, Asset.status == "draft")).all()
    chars = episode_characters(s, ep)
    el = {sc.id: effective_lang(s, ep, sc, lang) for sc in ep.scenes}

    def scene_count(kinds, items, role, stale_fn):
        total = len(items)
        ok = [sc for sc in items if sel(s, sc, role, el[sc.id]) is not None and not stale_fn(sc)]
        stale = sum(1 for sc in items if sel(s, sc, role, el[sc.id]) is not None and stale_fn(sc))
        pend = {sc.id: role_lang(sc, role, el[sc.id]) for sc in items if sc not in ok}
        dr = len({d.scene_id for d in drafts if d.role == role and d.scene_id in pend and d.language == pend[d.scene_id]})
        return dict(total=total, approved=len(ok), draft=dr, stale=stale,
                    running=sum(1 for j in active if j.kind in kinds), failed=sum(1 for j in failed if j.kind in kinds))

    out = []
    aud = [sc for sc in ep.scenes if needs_audio(sc)]
    st1 = scene_count(("voice",), aud, "voice", lambda sc: voice_stale(s, sc, el[sc.id]))
    untranslated = sum(1 for sc in aud if not text_of(s, sc, el[sc.id]))
    unapproved = sum(1 for sc in aud if text_of(s, sc, el[sc.id]) and text_row(s, sc, el[sc.id]).status != "approved")
    if untranslated or unapproved:
        st1["note"] = "; ".join(x for x in (f"{untranslated} sahnenin {lang} metni yok" if untranslated else "",
                                           f"{unapproved} çeviri onay bekliyor" if unapproved else "") if x)
    out.append(dict(n=1, **st1))
    ct = len(chars)
    cd = sum(1 for c in chars if c.main_image_id)
    pending_chars = {c.id for c in chars if not c.main_image_id}
    cdr = len({d.character_id for d in drafts if d.role == "character_main" and d.scene_id is None and d.character_id in pending_chars})
    st2 = scene_count(("scene_image",), list(ep.scenes), "scene_image", lambda sc: image_stale(s, sc, el[sc.id]))
    out.append(dict(n=2, total=ct + st2["total"], approved=cd + st2["approved"], draft=cdr + st2["draft"],
                    stale=st2["stale"], running=st2["running"] + sum(1 for j in active if j.kind == "character_image"),
                    failed=st2["failed"] + sum(1 for j in failed if j.kind == "character_image")))
    talk = [sc for sc in ep.scenes if sc.type == "talking"]
    rest = [sc for sc in ep.scenes if sc.type != "talking"]
    for n, items, kinds in ((3, talk, ("talking_video",)), (4, rest, ("silent_video",))):
        out.append(dict(n=n, **scene_count(kinds, items, "scene_video", lambda sc: bool(video_stale(s, sc, el[sc.id])))))
    final = latest_final(s, ep.id, lang)
    fresh = bool(final and (final.meta or {}).get("signature") == montage_signature(s, ep, lang))
    out.append(dict(n=5, total=1, approved=1 if fresh else 0, draft=0, stale=0,
                    running=sum(1 for j in active if j.kind == "montage" and j.language == lang),
                    failed=sum(1 for j in failed if j.kind == "montage" and j.language == lang),
                    note="" if fresh or not final else "Kurgu güncel değil: girdiler değişti",
                    blockers=montage_blockers(s, ep, lang)))
    rep = latest_report(s, ep.id, lang)
    rep_ok = bool(rep and final and rep.final_asset_id == final.id)
    out.append(dict(n=6, total=1, approved=1 if rep_ok else 0, draft=0, stale=0,
                    running=sum(1 for j in active if j.kind == "qc" and j.language == lang),
                    failed=sum(1 for j in failed if j.kind == "qc" and j.language == lang),
                    note="" if rep_ok or not rep else "Rapor eski kurguya ait"))
    fix = [j for j in jobs if j.is_fix]
    out.append(dict(n=7, total=len(fix), approved=sum(1 for j in fix if j.status == "succeeded"), draft=0, stale=0,
                    running=sum(1 for j in fix if j.status in ACTIVE),
                    failed=sum(1 for j in fix if j.status == "failed" and latest.get((j.kind, j.scene_id, j.character_id, j.language)) is j)))
    for st in out:
        st["title"] = STEP_TITLES[st["n"]]
        st.setdefault("note", "")
        st["complete"] = st["n"] != 7 and st["total"] > 0 and st["approved"] >= st["total"]
        if st["n"] in (1, 3, 4) and st["total"] == 0:
            st["complete"] = True
    return out


# --- otomatik mod -------------------------------------------------------------
def start_auto(s: Session, ep: Episode, langs: list[str] | None = None) -> None:
    ep.auto_langs = langs or ep_langs(s, ep)
    ep.auto_state, ep.auto_message = "running", "Başlatıldı"
    s.flush()
    advance(s, ep)


def stop_auto(s: Session, ep: Episode, state: str = "", message: str = "") -> None:
    ep.auto_state, ep.auto_message = state, message
    emit(s, "refresh", episode_id=ep.id)


def approve_drafts(s: Session, ep: Episode, langs: list[str], by: str = "auto") -> int:
    """Bekleyen taslakları (en yeni sürüm) ve çevirileri kabul eder."""
    n = 0
    for ch in episode_characters(s, ep):
        if not ch.main_image_id:
            d = has_draft(s, "character_main", character_id=ch.id)
            if d:
                approve(s, d, by)
                n += 1
    for l in langs:
        for sc in ep.scenes:
            el = effective_lang(s, ep, sc, l)
            row = text_row(s, sc, el)
            if row is not None and row.text and row.status != "approved":
                row.status = "approved"
            for role in ROLES:
                stale = {"voice": voice_stale(s, sc, el), "scene_image": image_stale(s, sc, el),
                         "scene_video": bool(video_stale(s, sc, el))}[role]
                if sel(s, sc, role, el) is None or stale:
                    d = has_draft(s, role, scene_id=sc.id, language=role_lang(sc, role, el))
                    if d:
                        approve(s, d, by)
                        n += 1
    s.flush()
    return n


def advance(s: Session, ep: Episode) -> None:
    """Otomatik mod: 1'den 6'ya, onay beklemeden. Her iş bitiminde yeniden çağrılır."""
    if ep.auto_state != "running":
        return
    if s.scalars(select(Job.id).where(Job.episode_id == ep.id, Job.kind != "claude", Job.status.in_(ACTIVE))).first():
        return
    s.refresh(ep)
    langs = [l for l in (ep.auto_langs or ep_langs(s, ep)) if l in ep_langs(s, ep)] or ep_langs(s, ep)
    approve_drafts(s, ep, langs)
    for steps in ((1, 2), (2,), (3, 4)):
        created, skipped = [], []
        for n in steps:
            for l in langs:
                r = run_step(s, ep, n, "missing", lang=l)
                created += r["created"]
                skipped += r["skipped"]
        if created:
            ep.auto_message = "Çalışıyor: " + ", ".join(STEP_TITLES[n] for n in steps)
            emit(s, "refresh", episode_id=ep.id)
            return
        done = all({x["n"]: x for x in step_status(s, ep, l)}[n]["complete"] for n in steps for l in langs)
        if not done:
            if steps == (1, 2) and all({x["n"]: x for x in step_status(s, ep, l)}[1]["complete"] for l in langs):
                continue  # karakter görselleri hazır, sahne görselleri bir sonraki turda
            why = "; ".join(f"{k['who']}: {k['reason']}" for k in skipped) or "eksik varlık var"
            stop_auto(s, ep, "error", f"Otomatik mod durdu ({', '.join(STEP_TITLES[n] for n in steps)}): {why}")
            return
    try:
        for l in langs:
            st = {x["n"]: x for x in step_status(s, ep, l)}
            if not st[5]["complete"]:
                enqueue_montage(s, ep, l)
                ep.auto_message = f"Çalışıyor: Kurgu ve miksaj ({l})"
                emit(s, "refresh", episode_id=ep.id)
                return
            if not st[6]["complete"]:
                enqueue_qc(s, ep, l)
                ep.auto_message = f"Çalışıyor: Kontrol ({l})"
                emit(s, "refresh", episode_id=ep.id)
                return
    except StepBlocked as e:
        stop_auto(s, ep, "error", str(e))
        return
    stop_auto(s, ep, "done", "Otomatik mod tamamlandı: kurgu ve kalite raporu hazır (" + ", ".join(langs) + ")")


def on_job_failed(s: Session, job: Job) -> None:
    if job.scene_id:
        sc = s.get(Scene, job.scene_id)
        if sc is not None and sc.chain:
            sc.chain = ""
    if job.episode_id:
        ep = s.get(Episode, job.episode_id)
        if ep is not None and ep.auto_state == "running":
            stop_auto(s, ep, "error", f"Otomatik mod durdu: {STEP_TITLES.get(job.step, job.kind)} işi hata verdi: {job.error[:200]}")


def on_job_succeeded(s: Session, job: Job, a: Asset | None) -> None:
    ep = s.get(Episode, job.episode_id) if job.episode_id else None
    if a is not None and job.auto_approve and a.status == "draft":
        approve(s, a, "auto")
    if ep is None:
        return
    s.flush()
    if job.kind == "montage" and (job.input or {}).get("then_qc") and ep.auto_state != "running":
        enqueue_qc(s, ep, job.language)
    advance(s, ep)
