"""Servis katmanı: platformdaki her işlem burada tek bir işlevdir.

Arayüz (api.py) ve yönetmen ajanının araçları (agent/tools.py) AYNI işlevleri çağırır; iki ayrı
kod yolu yoktur. İşlevler oturumu (Session) dışarıdan alır ve hata durumunda ServiceError yükseltir.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import languages, media, pipeline, settings_store as store, storage, tenancy
from .models import Asset, Character, Episode, Job, Project, Scene
from .providers import registry
from .storage import slug

SCENE_TYPES = ("talking", "silent", "narrator")
TYPE_ALIASES = {"konusan": "talking", "talking": "talking", "diyalog": "talking", "konusma": "talking",
                "konusmasiz": "silent", "sessiz": "silent", "silent": "silent",
                "anlatici": "narrator", "narrator": "narrator", "dis-ses": "narrator"}
TYPE_TR = {"talking": "konuşan", "silent": "konuşmasız", "narrator": "anlatıcı"}
KIND_TITLES = {"voice": "Ses", "character_image": "Karakter görseli", "scene_image": "Sahne görseli",
               "talking_video": "Konuşan video", "silent_video": "Konuşmasız video", "omni_fix": "Omni düzeltme",
               "lipsync_fix": "Dudak senkronu", "montage": "Kurgu ve miksaj", "qc": "Kalite kontrolü",
               "claude": "Claude (yönetmen)"}


class ServiceError(Exception):
    """Kullanıcıya/ajana gösterilecek iş kuralı hatası."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def check_lang(code: str) -> str:
    code = languages.normalize(code)
    if not code or not languages.known(code):
        raise ServiceError(f"'{code}' dili tanımlı değil. Tanımlı diller: " +
                           ", ".join(l["code"] for l in languages.all_languages()) +
                           ". Yeni dil için config/languages.yaml dosyasına ekleyin.", 400)
    return code


# ------------------------------------------------------------------ proje / karakter
def create_project(s: Session, name: str, description: str = "", style: str = "", default_language: str = "tr") -> Project:
    p = Project(user_id=tenancy.require_uid(), name=(name or "Yeni proje").strip(), description=description or "", style=style or "",
                default_language=check_lang(default_language or "tr"))
    s.add(p)
    s.flush()
    return p


def update_project(s: Session, p: Project, data: dict) -> Project:
    if "default_language" in data and data["default_language"]:
        data["default_language"] = check_lang(data["default_language"])
    for k in ("name", "description", "style", "default_language", "pronunciations"):
        if k in data and data[k] is not None:
            setattr(p, k, data[k])
    s.flush()
    return p


def create_character(s: Session, p: Project, name: str, description: str = "", is_narrator: bool = False,
                     voice_id: str = "", voice_settings: dict | None = None, voices: dict | None = None,
                     heygen: dict | None = None) -> Character:
    name = (name or "Yeni karakter").strip()
    existing = next((c for c in p.characters if slug(c.name) == slug(name)), None)
    if existing is not None:
        raise ServiceError(f"'{name}' adında bir karakter zaten var (kimlik {existing.id})")
    c = Character(project_id=p.id, name=name, description=description or "", is_narrator=bool(is_narrator),
                  voice_id=voice_id or "", voice_settings=voice_settings or {}, voices=voices or {}, heygen=heygen or {})
    s.add(c)
    s.flush()
    s.refresh(p)
    return c


def update_character(s: Session, c: Character, data: dict) -> Character:
    for k in ("name", "description", "is_narrator", "voice_id", "voice_settings", "voices", "heygen"):
        if k in data and data[k] is not None:
            setattr(c, k, data[k])
    s.flush()
    return c


def assign_voice(s: Session, c: Character, voice_id: str, language: str | None = None,
                 voice_settings: dict | None = None) -> Character:
    """Karaktere ses atar. language verilirse yalnızca o dil için (dil başına ayrı ses)."""
    if language:
        language = check_lang(language)
        voices = dict(c.voices or {})
        voices[language] = {"voice_id": voice_id, "voice_settings": voice_settings or (voices.get(language) or {}).get("voice_settings") or {}}
        c.voices = voices
    else:
        c.voice_id = voice_id
        if voice_settings is not None:
            c.voice_settings = voice_settings
    s.flush()
    return c


async def list_voices(language: str | None = None) -> dict:
    if registry.resolve_mode("elevenlabs_tts") == "mock":
        voices = [{"voice_id": f"mock-{n}", "name": f"Mock ses ({label})", "labels": {"gender": g}}
                  for n, label, g in (("kadin", "kadın", "female"), ("erkek", "erkek", "male"),
                                      ("anlatici", "anlatıcı", "male"), ("genc", "genç", "female"))]
        return {"mode": "mock", "voices": voices}
    p = registry.build("elevenlabs_tts", "real")
    return {"mode": "real", "voices": await p.list_voices(languages.base(language) if language else None)}


# ------------------------------------------------------------------ bölüm / dil
def create_episode(s: Session, p: Project, title: str | None = None, number: int | None = None,
                   language: str = "", langs: list[str] | None = None, settings: dict | None = None) -> Episode:
    number = number or (max((e.number for e in p.episodes), default=0) + 1)
    src = check_lang(language) if language else ""
    settings = dict(settings or {})
    # kullanıcının varsayılan çıktı biçimi 9:16 ise dikey sürüm de üretilir (bölüm ayarından değiştirilebilir)
    if "vertical" not in settings and store.prefs(p.user_id)["output_format"] == "9:16":
        settings["vertical"] = True
    ep = Episode(project_id=p.id, number=number, title=title or f"Bölüm {number}", language=src,
                 languages=[], settings=settings)
    s.add(ep)
    s.flush()
    for l in langs or []:
        add_language(s, ep, l)
    s.refresh(p)
    return ep


def add_language(s: Session, ep: Episode, lang: str) -> dict:
    """Bölüme hedef dil ekler. Yalnızca dile bağlı varlıklar (ses, konuşan video, altyazı, kurgu) üretilecektir."""
    lang = check_lang(lang)
    langs = pipeline.ep_langs(s, ep)
    if lang not in langs:
        ep.languages = [*langs, lang]
        s.flush()
    return {"languages": pipeline.ep_langs(s, ep), "warnings": pipeline.language_warnings(s, ep, lang)}


def remove_language(s: Session, ep: Episode, lang: str) -> list[str]:
    if lang == pipeline.source_lang(s, ep):
        raise ServiceError("Kaynak dil kaldırılamaz")
    ep.languages = [l for l in pipeline.ep_langs(s, ep) if l != lang]
    s.flush()
    return pipeline.ep_langs(s, ep)


# ------------------------------------------------------------------ sahneler
def _resolve_chars(s: Session, ep: Episode, names_or_ids: list, created: list[str]) -> list[int]:
    proj = s.get(Project, ep.project_id)
    by_slug = {slug(c.name): c for c in proj.characters}
    by_id = {c.id: c for c in proj.characters}
    ids = []
    for item in names_or_ids or []:
        c = by_id.get(item) if isinstance(item, int) else by_slug.get(slug(str(item)))
        if c is None:
            if isinstance(item, int):
                raise ServiceError(f"Karakter bulunamadı: {item}", 404)
            c = Character(project_id=proj.id, name=str(item).strip(), is_narrator=slug(str(item)) == "anlatici")
            s.add(c)
            s.flush()
            s.refresh(proj)
            by_slug[slug(c.name)] = c
            created.append(c.name)
        if not c.is_narrator and c.id not in ids:
            ids.append(c.id)
    return ids


def write_scenes(s: Session, ep: Episode, rows: list[dict], replace: bool = False) -> dict:
    """Sahneleri toplu ekler/günceller (sıra numarasına göre).

    Satır alanları: order_no, type, characters (ad ya da kimlik listesi), dialogue (kaynak dil metni),
    language (boşsa otomatik algıla), image_prompt, video_prompt, duration, audio_moment, notes, image_text.
    """
    if replace:
        for sc in list(ep.scenes):
            s.delete(sc)
        s.flush()
        s.refresh(ep)
    existing = {sc.order_no: sc for sc in ep.scenes}
    src = pipeline.source_lang(s, ep)
    new_chars: list[str] = []
    out, detected = [], []
    next_no = max(existing, default=0) + 1
    for row in rows:
        typ = TYPE_ALIASES.get(slug(str(row.get("type") or "")), "")
        text = str(row.get("dialogue") or "").strip()
        chars = row.get("characters") or []
        if isinstance(chars, str):
            chars = [chars]
        if not typ:
            typ = "talking" if text and chars else ("narrator" if text else "silent")
        no = row.get("order_no")
        try:
            no = int(float(no)) if no not in (None, "") else None
        except (TypeError, ValueError):
            no = None
        sc = existing.get(no) if no is not None else None
        if sc is None:
            if no is None:
                no = next_no
            next_no = max(next_no, no + 1)
            sc = Scene(episode_id=ep.id, order_no=no, type=typ, character_ids=[])
            ep.scenes.append(sc)
            s.flush()
            existing[no] = sc
        sc.type = typ
        if "characters" in row:
            sc.character_ids = _resolve_chars(s, ep, chars, new_chars)
        for k in ("image_prompt", "video_prompt", "notes"):
            if row.get(k) is not None:
                setattr(sc, k, str(row[k]).strip())
        if row.get("duration") not in (None, ""):
            try:
                sc.duration = max(1.0, float(str(row["duration"]).replace(",", ".")))
            except ValueError:
                pass
        if row.get("audio_moment") is not None:
            sc.audio_moment = bool(row["audio_moment"])
        if isinstance(row.get("image_text"), dict):
            sc.image_text = {languages.normalize(k): v for k, v in row["image_text"].items() if v}
        # dil: açıkça verildiyse o geçerli; verilmediyse algılama yalnızca öneridir (kısa metinde proje dili)
        explicit = languages.normalize(str(row.get("language") or ""))
        if explicit:
            check_lang(explicit)
            sc.language = "" if explicit == src else explicit
        if "dialogue" in row:
            lang = sc.language or src
            t = pipeline.set_text(s, ep, sc, lang, text if typ != "silent" else "", origin=row.get("origin", "human"))
            if text and not explicit and t.detected_language and not languages.base(lang) == t.detected_language:
                d = languages.detect(text, lang)
                if not d["fallback"]:
                    detected.append({"scene_no": sc.order_no, "detected": d["detected"], "confidence": d["confidence"],
                                     "used": lang})
        out.append(sc)
    s.flush()
    s.refresh(ep)
    pipeline.emit(s, "refresh", episode_id=ep.id)
    return {"scenes": out, "new_characters": new_chars, "language_suggestions": detected}


def update_scene(s: Session, ep: Episode, sc: Scene, data: dict) -> Scene:
    if data.get("type") and data["type"] not in SCENE_TYPES:
        raise ServiceError("Geçersiz sahne türü", 400)
    if "language" in data and data["language"] is not None:
        lang = languages.normalize(data.pop("language"))
        if lang:
            check_lang(lang)
        sc.language = "" if lang == pipeline.source_lang(s, ep) else lang
    texts = data.pop("texts", None)
    for k in ("order_no", "type", "character_ids", "image_prompt", "video_prompt", "duration", "audio_moment",
              "notes", "image_text", "needs_review", "review_note"):
        if k in data and data[k] is not None:
            setattr(sc, k, data[k])
    s.flush()
    for lang, text in (texts or {}).items():
        pipeline.set_text(s, ep, sc, check_lang(lang), text)
    s.flush()
    return sc


def scene_by_no(ep: Episode, no: int) -> Scene:
    sc = next((x for x in ep.scenes if x.order_no == no), None)
    if sc is None:
        raise ServiceError(f"Sahne {no} bulunamadı", 404)
    return sc


def set_translations(s: Session, ep: Episode, lang: str, items: list[dict], *, approve: bool, origin: str) -> dict:
    """Bir dilin repliklerini yazar. approve=False ise çeviri taslak kalır (onaylanmadan ses üretilmez)."""
    lang = check_lang(lang)
    if lang not in pipeline.ep_langs(s, ep):
        add_language(s, ep, lang)
    n = 0
    for it in items:
        sc = s.get(Scene, it["scene_id"]) if it.get("scene_id") else scene_by_no(ep, int(it["scene_no"]))
        if sc is None or sc.episode_id != ep.id:
            raise ServiceError("Sahne bu bölüme ait değil", 404)
        pipeline.set_text(s, ep, sc, lang, it.get("text") or "", status="approved" if approve else None, origin=origin)
        n += 1
    s.flush()
    pipeline.emit(s, "refresh", episode_id=ep.id)
    return {"language": lang, "written": n, "approved": approve}


def approve_text(s: Session, ep: Episode, sc: Scene, lang: str, approved: bool = True) -> None:
    row = pipeline.text_row(s, sc, lang)
    if row is None or not row.text:
        raise ServiceError("Onaylanacak metin yok")
    row.status = "approved" if approved else "draft"
    s.flush()


# ------------------------------------------------------------------ üretim
def lang_scope(s: Session, ep: Episode, lang: str | None) -> str:
    if lang in (None, "", "*", "tum", "tümü", "all"):
        return "*"
    lang = check_lang(lang)
    if lang not in pipeline.ep_langs(s, ep):
        raise ServiceError(f"{lang} bu bölümün hedef dilleri arasında değil; önce dil ekleyin")
    return lang


def run_step(s: Session, ep: Episode, step: int, mode: str = "missing", scene_id: int | None = None,
             lang: str | None = "*") -> dict:
    if mode not in ("all", "missing"):
        raise ServiceError("kapsam 'all' (tümü) ya da 'missing' (eksikler) olmalı", 400)
    try:
        return pipeline.run_step(s, ep, step, mode, scene_id, lang_scope(s, ep, lang))
    except pipeline.StepBlocked as e:
        raise ServiceError(str(e))


def generate(s: Session, ep: Episode, sc: Scene, kind: str, lang: str) -> Job:
    """Tek sahne için ses / görsel / video üretir (yeni sürüm)."""
    lang = lang_scope(s, ep, lang)
    if lang == "*":
        lang = pipeline.source_lang(s, ep)
    el = pipeline.effective_lang(s, ep, sc, lang)
    kinds = {"audio": ("voice",), "image": ("scene_image",), "video": pipeline.VIDEO_KINDS}.get(kind)
    if kinds is None:
        raise ServiceError("tür 'audio', 'image' ya da 'video' olmalı", 400)
    role = {"audio": "voice", "image": "scene_image", "video": "scene_video"}[kind]
    if pipeline.active_job(s, scene_id=sc.id, kinds=kinds, language=pipeline.role_lang(sc, role, el)):
        raise ServiceError("Bu sahne için zaten çalışan bir iş var")
    fn = {"audio": pipeline.voice_job, "image": pipeline.scene_image_job, "video": pipeline.video_job}[kind]
    r = fn(s, ep, sc, lang)
    if not isinstance(r, Job):
        raise ServiceError(f"Başlatılamadı: {r}")
    return r


def fix(s: Session, ep: Episode, sc: Scene, method: str, instruction: str = "", lang: str | None = None) -> list[int]:
    """7. adım: düzeltme. Sonrasında yalnızca etkilenen sahne, dil ve kurgu yeniden işlenir."""
    lang = lang_scope(s, ep, lang)
    if lang == "*":
        lang = pipeline.source_lang(s, ep)
    el = pipeline.effective_lang(s, ep, sc, lang)
    if pipeline.active_job(s, scene_id=sc.id):
        raise ServiceError("Bu sahne için zaten çalışan bir iş var")
    vid, aud = pipeline.sel(s, sc, "scene_video", el), pipeline.sel(s, sc, "voice", el)
    try:
        if method in ("omni", "lipsync"):
            if vid is None:
                raise ServiceError("Sahnenin seçili videosu yok")
            provider = "kling_omni" if method == "omni" else "kling_lipsync"
            inp: dict[str, Any] = {"video_path": vid.path, "video_url": pipeline.fresh_url(vid, provider),
                                   "video_duration": (vid.meta or {}).get("duration", 0),
                                   "src": dict((vid.params or {}).get("src") or {}), "label": f"Sahne {sc.order_no}",
                                   "language": vid.language}
            if method == "omni":
                if not (instruction or "").strip():
                    raise ServiceError("Düzeltme talimatı yazın")
                inp["instruction"] = instruction.strip()
            else:
                if sc.type != "talking":
                    raise ServiceError("Dudak senkronu yalnızca konuşan sahnelerde uygulanır")
                if aud is None:
                    raise ServiceError(f"Dudak senkronu için sahnenin onaylı {el} sesi gerekli")
                if aud.language != el:
                    raise ServiceError("Bir dilin videosu başka dilin sesiyle eşleştirilemez")
                ok, why = languages.support("kling_lipsync", el)
                if not ok:
                    raise ServiceError(why)
                inp.update(audio_path=aud.path, audio_duration=(aud.meta or {}).get("duration", 0))
                inp["src"]["audio_id"] = aud.id
            job = pipeline.enqueue(s, kind=f"{method}_fix", provider=provider, step=7, ep=ep, project_id=ep.project_id,
                                   scene=sc, inp=inp, language=vid.language, is_fix=True,
                                   auto_approve=ep.auto_state == "running")
            return [job.id]
        if method == "regen_audio":
            r = pipeline.voice_job(s, ep, sc, lang, is_fix=True)
            if not isinstance(r, Job):
                raise ServiceError(r)
            return [r.id]
        if method == "regen_scene":
            return pipeline.start_chain(s, ep, sc, lang)
    except pipeline.StepBlocked as e:
        raise ServiceError(str(e))
    raise ServiceError("Geçersiz düzeltme yöntemi (omni | lipsync | regen_audio | regen_scene)", 400)


def start_montage(s: Session, ep: Episode, lang: str | None, then_qc: bool = False) -> list[int]:
    scope = lang_scope(s, ep, lang)
    try:
        return [pipeline.enqueue_montage(s, ep, l, then_qc).id
                for l in (pipeline.ep_langs(s, ep) if scope == "*" else [scope])]
    except pipeline.StepBlocked as e:
        raise ServiceError(str(e))


def start_qc(s: Session, ep: Episode, lang: str | None) -> list[int]:
    scope = lang_scope(s, ep, lang)
    try:
        return [pipeline.enqueue_qc(s, ep, l).id for l in (pipeline.ep_langs(s, ep) if scope == "*" else [scope])]
    except pipeline.StepBlocked as e:
        raise ServiceError(str(e))


def update_episode_settings(s: Session, ep: Episode, patch: dict) -> dict:
    ep.settings = {**(ep.settings or {}), **{k: v for k, v in patch.items() if k in pipeline.DEFAULT_EP_SETTINGS}}
    s.flush()
    return pipeline.ep_settings(ep)


# ------------------------------------------------------------------ durum ve maliyet
def cost_of(s: Session, **flt) -> dict:
    if tenancy.uid() is not None:
        flt.setdefault("user_id", tenancy.uid())  # maliyetler her zaman kullanıcı bazındadır
    q = select(Job.mode, Job.status, Job.kind, func.sum(Job.cost_usd), func.count(Job.id)).group_by(Job.mode, Job.status, Job.kind)
    for k, v in flt.items():
        q = q.where(getattr(Job, k) == v)
    out = {"real_usd": 0.0, "mock_usd": 0.0, "pending_usd": 0.0, "claude_usd": 0.0, "jobs": 0}
    for mode, status, kind, usd, n in s.execute(q):
        usd = usd or 0.0
        if kind == "claude":
            if mode == "real":
                out["claude_usd"] += usd
                out["real_usd"] += usd
            continue
        out["jobs"] += n
        if status == "succeeded":
            out["mock_usd" if mode == "mock" else "real_usd"] += usd
        elif status in pipeline.ACTIVE and mode == "real":
            out["pending_usd"] += usd
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in out.items()}


def cost_by_language(s: Session, ep: Episode) -> list[dict]:
    out = []
    for l in ["", *pipeline.ep_langs(s, ep)]:
        c = cost_of(s, episode_id=ep.id, language=l)
        out.append({"language": l, "label": languages.name(l) if l else "Ortak (dilden bağımsız)", **c})
    return out


def estimate_plan(s: Session, ep: Episode, langs: list[str] | None = None) -> dict:
    """Bölümün baştan sona tahmini üretim maliyeti (plan onayı ve bütçe uyarısı için)."""
    langs = langs or pipeline.ep_langs(s, ep)
    total: dict[str, float] = {}

    def add(provider: str, inp: dict) -> None:
        total[provider] = total.get(provider, 0.0) + registry.estimate(provider, inp).usd

    for ch in pipeline.episode_characters(s, ep):
        add("kling_image", {"prompt": "x", "ref_image_paths": []})
    for sc in ep.scenes:
        refs = ["x"] * len(sc.character_ids or [])
        add("kling_image", {"prompt": "x", "ref_image_paths": refs})
        if sc.type != "talking":
            add("kling_video", {"duration": sc.duration, "native_audio": sc.audio_moment})
        for l in langs:
            el = pipeline.effective_lang(s, ep, sc, l)
            text = pipeline.text_of(s, sc, el) or pipeline.text_of(s, sc, pipeline.source_lang(s, ep))
            if pipeline.needs_audio(sc) and text:
                add("elevenlabs_tts", {"text": text, "model": languages.tts_model_for(el, store.model_for("elevenlabs_tts"))})
                if sc.type == "talking":
                    add("heygen_avatar", {"audio_duration": max(1.5, len(text.split()) * 0.4)})
    by = [{"provider": k, "label": registry.cfg(k).get("label", k), "usd": round(v, 3)} for k, v in total.items()]
    return {"languages": langs, "scenes": len(ep.scenes), "by_provider": by, "total_usd": round(sum(total.values()), 2)}


def episode_summary(s: Session, ep: Episode) -> dict:
    """Bölüm hattının sıkıştırılmış özeti (ajanın durum_getir aracı ve canlı panel için)."""
    proj = s.get(Project, ep.project_id)
    langs = pipeline.ep_langs(s, ep)
    src = pipeline.source_lang(s, ep)
    drafts = s.scalars(select(Asset).where(Asset.project_id == ep.project_id, Asset.status == "draft")
                       .order_by(Asset.id)).all()
    scene_no = {sc.id: sc.order_no for sc in ep.scenes}
    pending = []
    latest: dict[tuple, Asset] = {}
    for a in drafts:
        if a.role == "character_main" and a.scene_id is None:
            latest[("c", a.character_id)] = a
        elif a.scene_id in scene_no and a.role in pipeline.ROLES:
            latest[(a.scene_id, a.role, a.language)] = a
    chars = {c.id: c for c in proj.characters}
    for key, a in latest.items():
        if key[0] == "c":
            ch = chars.get(a.character_id)
            if ch is None or ch.main_image_id:
                continue
            pending.append({"asset_id": a.id, "role": "character_main", "character": ch.name, "version": a.version})
        else:
            pending.append({"asset_id": a.id, "role": a.role, "scene_no": scene_no[a.scene_id],
                            "language": a.language or None, "version": a.version, "is_fix": a.step == 7})
    jobs = s.scalars(select(Job).where(Job.episode_id == ep.id, Job.kind != "claude").order_by(Job.id)).all()
    active = [j for j in jobs if j.status in pipeline.ACTIVE]
    last: dict[tuple, Job] = {}
    for j in jobs:
        last[(j.kind, j.scene_id, j.character_id, j.language)] = j
    failed = [j for j in last.values() if j.status == "failed"]
    per_lang = {}
    for l in langs:
        steps = pipeline.step_status(s, ep, l)
        final = pipeline.latest_final(s, ep.id, l)
        rep = pipeline.latest_report(s, ep.id, l)
        per_lang[l] = {
            "steps": [{"n": x["n"], "title": x["title"], "done": f"{x['approved']}/{x['total']}", "complete": x["complete"],
                       **{k: x[k] for k in ("draft", "running", "failed", "stale") if x.get(k)},
                       **({"note": x["note"]} if x.get("note") else {}),
                       **({"blockers": x["blockers"][:6]} if x.get("blockers") else {})} for x in steps],
            "final": {"asset_id": final.id, "version": final.version, "url": f"/media/{final.path}",
                      "duration": round((final.meta or {}).get("duration", 0), 1),
                      "fresh": (final.meta or {}).get("signature") == pipeline.montage_signature(s, ep, l)} if final else None,
            "report": {"verdict": rep.summary.get("verdict"), "errors": rep.summary.get("errors"),
                       "warnings": rep.summary.get("warnings"),
                       "for_current_final": bool(final and rep.final_asset_id == final.id)} if rep else None,
            "warnings": pipeline.language_warnings(s, ep, l),
        }
    scenes = []
    for sc in ep.scenes:
        texts = {t.language: {"status": t.status, "chars": len(t.text)} for t in sc.texts if t.text}
        scenes.append({"no": sc.order_no, "id": sc.id, "type": TYPE_TR[sc.type],
                       "characters": [chars[c].name for c in sc.character_ids or [] if c in chars],
                       "texts": texts, **({"fixed_language": sc.language} if sc.language else {}),
                       **({"needs_review": sc.review_note or True} if sc.needs_review else {})})
    return {
        "project": {"id": proj.id, "name": proj.name}, "episode": {"id": ep.id, "number": ep.number, "title": ep.title},
        "source_language": src, "languages": langs, "per_language": per_lang, "scenes": scenes,
        "characters": [{"id": c.id, "name": c.name, "narrator": c.is_narrator, "has_voice": bool(c.voice_id or c.voices),
                        "main_image_approved": bool(c.main_image_id)} for c in proj.characters],
        "pending_approval": pending,
        "active_jobs": [{"id": j.id, "kind": j.kind, "scene_no": scene_no.get(j.scene_id), "language": j.language or None,
                         "status": j.status, "progress": j.progress} for j in active],
        "failed_jobs": [{"id": j.id, "kind": j.kind, "scene_no": scene_no.get(j.scene_id), "language": j.language or None,
                         "error": j.error[:300]} for j in failed],
        "cost": cost_of(s, episode_id=ep.id),
    }


def inspect_asset(s: Session, a: Asset, frames: int = 3) -> dict:
    """Varlığın üst verisi + görsel inceleme için küçük JPEG'ler (görsel: kendisi; video: birkaç kare)."""
    path = storage.abs_path(a.path)
    info = {"asset_id": a.id, "role": a.role, "kind": a.kind, "language": a.language or None, "version": a.version,
            "status": a.status, "provider": a.provider, "mode": a.mode, "prompt": (a.prompt or "")[:600],
            "meta": {k: v for k, v in (a.meta or {}).items() if k in ("duration", "width", "height", "fps", "has_audio")}}
    images: list[bytes] = []
    if a.kind == "image":
        images = [media.image_jpeg(path)]
    elif a.kind == "video":
        images = media.frames_jpeg(path, frames)
        info["note"] = f"{len(images)} kare eşit aralıklarla alındı"
    return {"info": info, "images": images}
