"""REST + SSE uçları (tümü /api altında). İş kuralları services.py / pipeline.py içindedir;
buradaki uçlar yalnızca çağırır ve serileştirir (ajan araçları da aynı işlevleri kullanır)."""
from __future__ import annotations

import asyncio
import functools
import inspect
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import importer, languages, media, pipeline, seed as seed_mod, services, settings_store as store, storage, tenancy
from .auth import current_user
from .agent import llm as llm_mod, runner
from .config import get_models
from .db import get_db
from .events import bus
from .models import AgentRun, Asset, Character, ChatMessage, Conversation, Episode, Job, Project, Scene, User, now
from .providers import registry
from .services import ServiceError
from .worker import worker


def _committing(endpoint):
    """Uç işlevini sarar: oturumu, işlevin ÇALIŞTIĞI iş parçacığında kaydeder.

    Eşzamanlı (def) uçlar iş parçacığı havuzunda çalışır. Kayıt olay döngüsüne bırakılırsa, yazma
    kilidini tutan istek döngüyü, döngü de (işçinin veritabanı yazısı yüzünden) kilidi bekler ve
    30 saniyelik karşılıklı kilitlenme oluşur. Bu yüzden kayıt burada, yanıt dönmeden yapılır.
    """
    if inspect.iscoroutinefunction(endpoint):
        @functools.wraps(endpoint)
        async def awrapper(*args, **kwargs):
            user = kwargs.get("user")
            if user is not None:
                tenancy.USER_ID.set(user.id)
            result = await endpoint(*args, **kwargs)
            s = kwargs.get("s")
            if s is not None:
                s.commit()
            return result
        return awrapper

    @functools.wraps(endpoint)
    def wrapper(*args, **kwargs):
        user = kwargs.get("user")
        if user is not None:
            tenancy.USER_ID.set(user.id)
        result = endpoint(*args, **kwargs)
        s = kwargs.get("s")
        if s is not None:
            s.commit()
        return result
    return wrapper


class CommitRoute(APIRoute):
    """Değişiklikleri yanıt gönderilmeden ÖNCE kaydeder (istemci hemen yeniden okuduğunda eski veri
    görmesin), iş kuralı hatalarını HTTP hatasına çevirir ve veri değiştiren her başarılı istekten
    sonra arayüze 'yenile' olayı yollar."""

    def __init__(self, path, endpoint, **kwargs):
        super().__init__(path, _committing(endpoint), **kwargs)

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                response = await original(request)
            except ServiceError as e:
                raise HTTPException(e.status, str(e))
            except pipeline.StepBlocked as e:
                raise HTTPException(409, str(e))
            except pipeline.BudgetExceeded as e:
                raise HTTPException(409, str(e))
            uid = getattr(request.state, "user_id", None)
            if request.method != "GET" and response.status_code < 400 and uid is not None:
                bus.publish("refresh", user_id=uid)
            return response

        return handler


router = APIRouter(prefix="/api", route_class=CommitRoute)


def owner_of(s: Session, obj) -> int | None:
    """Kaydın sahibi olan kullanıcı. Alt kayıtlar (karakter, bölüm, sahne) sahipliği projeden alır."""
    if obj is None:
        return None
    if isinstance(obj, (Project, Asset, Job, Conversation)):
        return obj.user_id
    if isinstance(obj, (Character, Episode)):
        return owner_of(s, s.get(Project, obj.project_id))
    if isinstance(obj, Scene):
        return owner_of(s, s.get(Episode, obj.episode_id))
    if isinstance(obj, ChatMessage):
        return owner_of(s, s.get(Conversation, obj.conversation_id))
    return None


def own(s: Session, user: User, model, id_, what: str = "Kayıt"):
    """Kaydı yalnızca sahibine verir. Başkasının kaydı ile hiç var olmayan kayıt aynı yanıtı alır: 404."""
    obj = s.get(model, id_)
    if obj is None or owner_of(s, obj) != user.id:
        raise HTTPException(404, f"{what} bulunamadı")
    return obj


def need(obj, what: str = "Kayıt"):
    if obj is None:
        raise HTTPException(404, f"{what} bulunamadı")
    return obj


def iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt else None


# ----------------------------------------------------------------- serileştirme
def asset_out(a: Asset | None) -> dict | None:
    if a is None:
        return None
    meta = {k: v for k, v in (a.meta or {}).items() if k not in ("source_url", "signature", "sources")}
    return {"id": a.id, "kind": a.kind, "role": a.role, "language": a.language, "version": a.version, "step": a.step,
            "provider": a.provider, "mode": a.mode, "model": a.model, "prompt": a.prompt, "params": a.params, "meta": meta,
            "status": a.status, "approved_by": a.approved_by, "url": f"/media/{a.path}", "scene_id": a.scene_id,
            "character_id": a.character_id, "job_id": a.job_id, "created_at": iso(a.created_at)}


def manual_info(s: Session, j: Job) -> dict | None:
    if j.status != "waiting_manual":
        return None
    m = registry.cfg(j.provider).get("manual") or {}
    inp = j.input or {}
    inputs = []
    for key, label in (("video_path", "Kaynak video"), ("audio_path", "Ses dosyası"), ("image_path", "Görsel")):
        if inp.get(key):
            inputs.append({"label": label, "url": f"/media/{inp[key]}"})
    return {"title": m.get("title", "Bu adımı elle tamamlayın"), "steps": m.get("steps", []), "inputs": inputs,
            "instruction": inp.get("instruction", ""), "accept": {"audio": "audio/*", "image": "image/*"}.get(
                registry.CLASSES[j.provider].kind, "video/*")}


def job_out(s: Session, j: Job, scene_no: dict[int, int] | None = None) -> dict:
    no = (scene_no or {}).get(j.scene_id) if j.scene_id else None
    if no is None and j.scene_id:
        sc = s.get(Scene, j.scene_id)
        no = sc.order_no if sc else None
    label = {"local": "ffmpeg (yerel)", "claude": "Claude (Anthropic)"}.get(j.provider) or registry.cfg(j.provider).get("label", j.provider)
    return {"id": j.id, "kind": j.kind, "title": services.KIND_TITLES.get(j.kind, j.kind), "provider": j.provider,
            "provider_label": label, "mode": j.mode, "step": j.step, "project_id": j.project_id, "episode_id": j.episode_id,
            "scene_id": j.scene_id, "scene_no": no, "character_id": j.character_id, "language": j.language,
            "run_id": j.run_id, "status": j.status, "attempts": j.attempts, "error": j.error, "progress": j.progress,
            "cost_usd": j.cost_usd, "cost_units": j.cost_units, "cost_unit": j.cost_unit, "is_fix": j.is_fix,
            "result_asset_id": j.result_asset_id,
            "retry_at": iso(j.run_after) if j.status == "queued" and j.attempts else None,
            "created_at": iso(j.created_at), "started_at": iso(j.started_at), "finished_at": iso(j.finished_at),
            "manual": manual_info(s, j) if j.provider not in ("local", "claude") else None}


def character_out(s: Session, c: Character, full: bool = False) -> dict:
    d = {"id": c.id, "project_id": c.project_id, "name": c.name, "description": c.description,
         "is_narrator": c.is_narrator, "voice_id": c.voice_id, "voice_settings": c.voice_settings or {},
         "voices": c.voices or {}, "heygen": c.heygen or {}, "main_image_id": c.main_image_id,
         "main_image": asset_out(pipeline.asset(s, c.main_image_id))}
    if full:
        assets = s.scalars(select(Asset).where(Asset.character_id == c.id, Asset.scene_id.is_(None))
                           .order_by(Asset.id.desc())).all()
        d["images"] = [asset_out(a) for a in assets if a.role == "character_main"]
        d["references"] = [asset_out(a) for a in assets if a.role == "character_ref"]
        d["jobs"] = [job_out(s, j) for j in s.scalars(
            select(Job).where(Job.character_id == c.id, Job.scene_id.is_(None)).order_by(Job.id.desc()).limit(3))]
    return d


def scene_out(s: Session, ep: Episode, sc: Scene, lang: str, assets: list[Asset], jobs: list[Job]) -> dict:
    el = pipeline.effective_lang(s, ep, sc, lang)
    src = sc.language or pipeline.source_lang(s, ep)
    deps = {role: pipeline.role_lang(sc, role, el) for role in pipeline.ROLES}
    mine = [a for a in assets if a.role in deps and a.language == deps[a.role]]
    latest: dict[str, Job] = {}
    for j in sorted(jobs, key=lambda j: j.id):
        role = pipeline.KIND_ROLE.get(j.kind, j.kind)
        if role in deps and j.language == deps[role]:
            latest[role] = j
    sel = {role: pipeline.sel(s, sc, role, el) for role in pipeline.ROLES}
    a_src = pipeline.sel(s, sc, "voice", src)
    dur = (sel["voice"].meta or {}).get("duration") if sel["voice"] else None
    src_dur = (a_src.meta or {}).get("duration") if a_src else None
    return {"id": sc.id, "episode_id": sc.episode_id, "order_no": sc.order_no, "type": sc.type,
            "character_ids": sc.character_ids or [], "language": sc.language, "effective_language": el,
            "source_language": src, "fixed_language": bool(sc.language and sc.language != pipeline.source_lang(s, ep)),
            "dialogue": pipeline.text_of(s, sc, el),
            "texts": {t.language: {"text": t.text, "status": t.status, "origin": t.origin, "detected": t.detected_language,
                                   "confidence": t.confidence} for t in sc.texts},
            "text_status": (pipeline.text_row(s, sc, el).status if pipeline.text_row(s, sc, el) else ""),
            "image_prompt": sc.image_prompt, "video_prompt": sc.video_prompt, "image_text": sc.image_text or {},
            "duration": sc.duration, "audio_moment": sc.audio_moment, "notes": sc.notes,
            "audio_id": sel["voice"].id if sel["voice"] else None, "image_id": sel["scene_image"].id if sel["scene_image"] else None,
            "video_id": sel["scene_video"].id if sel["scene_video"] else None,
            "language_dependent": {"image": bool(deps["scene_image"]), "video": bool(deps["scene_video"])},
            "chain": bool(sc.chain), "needs_review": sc.needs_review, "review_note": sc.review_note,
            "stale": pipeline.video_stale(s, sc, el), "voice_stale": pipeline.voice_stale(s, sc, el),
            "audio_duration": dur, "source_audio_duration": src_dur if el != src else None,
            "assets": [asset_out(a) for a in mine],
            "jobs": {role: job_out(s, j, {sc.id: sc.order_no}) for role, j in latest.items()
                     if j.status in pipeline.ACTIVE or j.status == "failed"}}


def episode_brief(s: Session, ep: Episode) -> dict:
    return {"id": ep.id, "project_id": ep.project_id, "number": ep.number, "title": ep.title,
            "auto_state": ep.auto_state, "auto_message": ep.auto_message, "scene_count": len(ep.scenes),
            "source_language": pipeline.source_lang(s, ep), "languages": pipeline.ep_langs(s, ep),
            "settings": pipeline.ep_settings(ep)}


def episode_out(s: Session, ep: Episode, lang: str | None = None) -> dict:
    proj = s.get(Project, ep.project_id)
    langs = pipeline.ep_langs(s, ep)
    lang = lang if lang in langs else langs[0]
    ids = [sc.id for sc in ep.scenes]
    assets = s.scalars(select(Asset).where(Asset.scene_id.in_(ids)).order_by(Asset.id.desc())).all() if ids else []
    jobs = s.scalars(select(Job).where(Job.episode_id == ep.id, Job.scene_id.in_(ids))).all() if ids else []
    ep_assets = s.scalars(select(Asset).where(Asset.episode_id == ep.id, Asset.scene_id.is_(None))
                          .order_by(Asset.id.desc())).all()
    rep = pipeline.latest_report(s, ep.id, lang)
    active = s.scalars(select(Job).where(Job.episode_id == ep.id, Job.kind != "claude", Job.status.in_(pipeline.ACTIVE))
                       .order_by(Job.id)).all()
    failed_local = s.scalars(select(Job).where(Job.episode_id == ep.id, Job.kind.in_(("montage", "qc")), Job.language == lang)
                             .order_by(Job.id.desc()).limit(2)).all()

    def first(role: str) -> dict | None:
        return asset_out(next((a for a in ep_assets if a.role == role and a.language == lang), None))

    lang_rows = []
    for l in langs:
        st = pipeline.step_status(s, ep, l)
        lang_rows.append({"code": l, "name": languages.name(l), "rtl": languages.is_rtl(l), "is_source": l == langs[0],
                          "complete": sum(1 for x in st if x["n"] <= 6 and x["complete"]),
                          "has_final": pipeline.latest_final(s, ep.id, l) is not None,
                          "warnings": pipeline.language_warnings(s, ep, l)})
    d = episode_brief(s, ep)
    d.update({
        "language": lang, "language_rows": lang_rows,
        "project": {"id": proj.id, "name": proj.name, "default_language": proj.default_language},
        "characters": [character_out(s, c, full=True) for c in proj.characters],
        "scenes": [scene_out(s, ep, sc, lang, [a for a in assets if a.scene_id == sc.id], [j for j in jobs if j.scene_id == sc.id])
                   for sc in ep.scenes],
        "steps": pipeline.step_status(s, ep, lang),
        "final": first("final"), "final_vertical": first("final_vertical"), "subtitles": first("subtitles"),
        "uploads": [asset_out(a) for a in ep_assets if a.role in ("music", "sfx")],
        "report": {"id": rep.id, "final_asset_id": rep.final_asset_id, "summary": rep.summary, "findings": rep.findings,
                   "created_at": iso(rep.created_at)} if rep else None,
        "active_jobs": [job_out(s, j) for j in active],
        "local_errors": [job_out(s, j) for j in failed_local if j.status == "failed"],
        "cost": services.cost_of(s, episode_id=ep.id), "cost_by_language": services.cost_by_language(s, ep),
    })
    return d


def project_out(s: Session, p: Project, full: bool = False) -> dict:
    d = {"id": p.id, "name": p.name, "description": p.description, "style": p.style,
         "default_language": p.default_language, "pronunciations": p.pronunciations or {},
         "created_at": iso(p.created_at), "character_count": len(p.characters),
         "episode_count": len(p.episodes), "cost": services.cost_of(s, project_id=p.id)}
    if full:
        d["characters"] = [character_out(s, c, full=True) for c in p.characters]
        d["episodes"] = [{**episode_brief(s, e), "cost": services.cost_of(s, episode_id=e.id),
                          "steps": [{"n": x["n"], "complete": x["complete"]}
                                    for x in pipeline.step_status(s, e, pipeline.source_lang(s, e))]}
                         for e in p.episodes]
        # varsayılan dil her zaman ilk sırada
        used = [p.default_language, *sorted({l for e in p.episodes for l in pipeline.ep_langs(s, e)} - {p.default_language})]
        d["languages"] = [{"code": l, "name": languages.name(l)} for l in used]
    return d


# ----------------------------------------------------------------- projeler
class ProjectIn(BaseModel):
    name: str | None = None
    description: str | None = None
    style: str | None = None
    default_language: str | None = None
    pronunciations: dict | None = None


@router.get("/projects")
def list_projects(s: Session = Depends(get_db), user: User = Depends(current_user)):
    return [project_out(s, p) for p in s.scalars(select(Project).where(Project.user_id == user.id).order_by(Project.id.desc()))]


@router.post("/projects")
def create_project(body: ProjectIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = services.create_project(s, body.name or "Yeni proje", body.description or "", body.style or "",
                                body.default_language or "tr")
    return project_out(s, p, True)


@router.get("/projects/{pid}")
def get_project(pid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return project_out(s, own(s, user, Project, pid, "Proje"), True)


@router.patch("/projects/{pid}")
def update_project(pid: int, body: ProjectIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = services.update_project(s, own(s, user, Project, pid, "Proje"), body.model_dump(exclude_unset=True))
    return project_out(s, p, True)


def _cancel_jobs(s: Session, **flt) -> None:
    q = select(Job).where(Job.status.in_(pipeline.ACTIVE))
    for k, v in flt.items():
        q = q.where(getattr(Job, k) == v)
    for j in s.scalars(q):
        j.status, j.finished_at = "canceled", now()
        worker.cancel(j.id)


@router.delete("/projects/{pid}")
def delete_project(pid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = own(s, user, Project, pid, "Proje")
    _cancel_jobs(s, project_id=pid)
    d = storage.rel_path(storage.project_dir(p))
    for model in (Asset, Job):
        for row in s.scalars(select(model).where(model.project_id == pid)):
            s.delete(row)
    for conv in s.scalars(select(Conversation).where(Conversation.project_id == pid)):
        conv.project_id, conv.episode_id = None, None
    s.delete(p)
    storage.delete_tree(d)
    return {"ok": True}


# ----------------------------------------------------------------- karakterler
class CharacterIn(BaseModel):
    name: str | None = None
    description: str | None = None
    is_narrator: bool | None = None
    voice_id: str | None = None
    voice_settings: dict | None = None
    voices: dict | None = None
    heygen: dict | None = None


@router.post("/projects/{pid}/characters")
def create_character(pid: int, body: CharacterIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = own(s, user, Project, pid, "Proje")
    name = body.name or "Yeni karakter"
    n, base_name = 1, name
    while any(storage.slug(c.name) == storage.slug(name) for c in p.characters):
        n += 1
        name = f"{base_name} {n}"
    c = services.create_character(s, p, name, body.description or "", bool(body.is_narrator), body.voice_id or "",
                                  body.voice_settings, body.voices, body.heygen)
    return character_out(s, c, True)


@router.patch("/characters/{cid}")
def update_character(cid: int, body: CharacterIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    c = services.update_character(s, own(s, user, Character, cid, "Karakter"), body.model_dump(exclude_unset=True))
    return character_out(s, c, True)


@router.delete("/characters/{cid}")
def delete_character(cid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own(s, user, Character, cid, "Karakter")
    for sc in s.scalars(select(Scene).join(Episode).where(Episode.project_id == c.project_id)):
        if cid in (sc.character_ids or []):
            sc.character_ids = [x for x in sc.character_ids if x != cid]
    s.delete(c)
    return {"ok": True}


UPLOAD_LIMIT_MB = {"text": 2, "image": 25, "audio": 60, "video": 600}


async def _save_upload(file: UploadFile, dest: Path, kind: str) -> dict:
    """Yüklemeyi diske yazar. Boyut sınırı akış sırasında uygulanır; içerik ffprobe ile doğrulanır."""
    limit = UPLOAD_LIMIT_MB[kind] * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"Dosya çok büyük: bu tür için sınır {UPLOAD_LIMIT_MB[kind]} MB")
            f.write(chunk)
    if kind == "text":
        try:
            dest.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "Dosya kabul edilmedi: metin dosyası UTF-8 olmalı")
        meta = {"size": size}
    else:
        try:
            meta = await asyncio.to_thread(media.validate, dest, kind)
        except media.MediaError as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, f"Dosya kabul edilmedi: {e}")
    await asyncio.to_thread(storage.publish, dest)
    return meta


def _ext(file: UploadFile, default: str) -> str:
    ext = Path(file.filename or "").suffix.lower()
    return ext if ext and len(ext) <= 6 and ext[1:].isalnum() else default


@router.post("/characters/{cid}/references")
async def upload_reference(cid: int, file: UploadFile = File(...), s: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own(s, user, Character, cid, "Karakter")
    v = storage.next_version(s, role="character_ref", character_id=cid)
    dest = storage.target_path(s, role="character_ref", version=v, ext=_ext(file, ".png"), character=c)
    meta = await _save_upload(file, dest, "image")
    a = Asset(user_id=user.id, project_id=c.project_id, character_id=cid, kind="image", role="character_ref", path=storage.rel_path(dest),
              version=v, step=2, provider="upload", mode="manual", status="approved", approved_by="user", meta=meta)
    s.add(a)
    s.flush()
    return asset_out(a)


@router.post("/characters/{cid}/generate-image")
def generate_character_image(cid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own(s, user, Character, cid, "Karakter")
    if pipeline.active_job(s, character_id=cid, kinds=("character_image",)):
        raise HTTPException(409, "Bu karakter için zaten çalışan bir iş var")
    r = pipeline.character_image_job(s, c)
    if not isinstance(r, Job):
        raise HTTPException(409, r)
    return job_out(s, r)


@router.get("/voices")
async def voices(language: str | None = None, user: User = Depends(current_user)):
    try:
        return await services.list_voices(language)
    except Exception as e:
        raise HTTPException(502, f"Ses listesi alınamadı: {e}")


# ----------------------------------------------------------------- diller
@router.get("/languages")
def list_languages(user: User = Depends(current_user)):
    return {"languages": languages.matrix()}


class DetectIn(BaseModel):
    text: str
    fallback: str = "tr"


@router.post("/detect-language")
def detect_language(body: DetectIn, user: User = Depends(current_user)):
    return languages.detect(body.text, body.fallback)


# ----------------------------------------------------------------- bölümler
class EpisodeIn(BaseModel):
    number: int | None = None
    title: str | None = None
    settings: dict | None = None
    language: str | None = None
    languages: list[str] | None = None


@router.post("/projects/{pid}/episodes")
def create_episode(pid: int, body: EpisodeIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = own(s, user, Project, pid, "Proje")
    ep = services.create_episode(s, p, body.title, body.number, body.language or "", body.languages, body.settings)
    return episode_brief(s, ep)


@router.get("/episodes/{eid}")
def get_episode(eid: int, lang: str | None = None, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return episode_out(s, own(s, user, Episode, eid, "Bölüm"), lang)


@router.patch("/episodes/{eid}")
def update_episode(eid: int, body: EpisodeIn, lang: str | None = None, s: Session = Depends(get_db), user: User = Depends(current_user)):
    ep = own(s, user, Episode, eid, "Bölüm")
    data = body.model_dump(exclude_unset=True)
    if "settings" in data:
        services.update_episode_settings(s, ep, data.pop("settings") or {})
    if data.get("language"):
        ep.language = services.check_lang(data.pop("language"))
    for k in ("number", "title"):
        if data.get(k) is not None:
            setattr(ep, k, data[k])
    s.flush()
    return episode_out(s, ep, lang)


@router.delete("/episodes/{eid}")
def delete_episode(eid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    ep = own(s, user, Episode, eid, "Bölüm")
    _cancel_jobs(s, episode_id=eid)
    d = storage.rel_path(storage.episode_dir(s, ep))
    for model in (Asset, Job):
        for row in s.scalars(select(model).where(model.episode_id == eid)):
            s.delete(row)
    for conv in s.scalars(select(Conversation).where(Conversation.episode_id == eid)):
        conv.episode_id = None
    s.delete(ep)
    storage.delete_tree(d)
    return {"ok": True}


class LanguageIn(BaseModel):
    language: str


@router.post("/episodes/{eid}/languages")
def add_language(eid: int, body: LanguageIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return services.add_language(s, own(s, user, Episode, eid, "Bölüm"), body.language)


@router.delete("/episodes/{eid}/languages/{lang}")
def remove_language(eid: int, lang: str, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return {"languages": services.remove_language(s, own(s, user, Episode, eid, "Bölüm"), lang)}


class ImportIn(BaseModel):
    text: str
    filename: str = ""
    replace: bool = False
    preview: bool = False


@router.post("/episodes/{eid}/import")
def import_scenes(eid: int, body: ImportIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    ep = own(s, user, Episode, eid, "Bölüm")
    try:
        rows = importer.parse(body.text, body.filename)
    except Exception as e:
        raise HTTPException(400, f"Senaryo çözümlenemedi: {e}")
    if not rows:
        raise HTTPException(400, "Senaryoda sahne bulunamadı")
    if body.preview:
        return {"rows": importer.preview(rows, pipeline.source_lang(s, ep))}
    base = 0 if body.replace else max((sc.order_no for sc in ep.scenes), default=0)
    for i, r in enumerate(sorted(rows, key=lambda r: r["order_no"]), 1):
        r["order_no"] = base + i
    res = services.write_scenes(s, ep, rows, body.replace)
    return {"scenes": len(res["scenes"]), "new_characters": res["new_characters"],
            "language_suggestions": res["language_suggestions"], "episode": episode_out(s, ep)}


class TranslationsIn(BaseModel):
    language: str
    items: list[dict]
    approve: bool = False


@router.post("/episodes/{eid}/translations")
def write_translations(eid: int, body: TranslationsIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return services.set_translations(s, own(s, user, Episode, eid, "Bölüm"), body.language, body.items,
                                     approve=body.approve, origin="human")


@router.post("/episodes/{eid}/translations/suggest")
async def suggest_translations(eid: int, body: LanguageIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    """Claude ile çeviri önerir (taslak olarak yazar; onaylanmadan ses üretilmez). Anahtar yoksa elle doldurulur."""
    ep = own(s, user, Episode, eid, "Bölüm")
    lang = services.check_lang(body.language)
    src = pipeline.source_lang(s, ep)
    todo = [{"scene_no": sc.order_no, "text": pipeline.text_of(s, sc, src)} for sc in ep.scenes
            if pipeline.text_of(s, sc, src) and not pipeline.text_of(s, sc, lang)]
    if not todo:
        return {"written": 0, "note": "Çevrilecek boş replik yok"}
    items = await runner.suggest_translations(todo, src, lang)
    res = services.set_translations(s, ep, lang, items, approve=False, origin="suggestion")
    return res


@router.post("/episodes/{eid}/uploads")
async def upload_layer(eid: int, role: str = Form("music"), at: float = Form(0.0), volume: float = Form(1.0),
                       file: UploadFile = File(...), s: Session = Depends(get_db), user: User = Depends(current_user)):
    """Müzik ve efekt katmanı için ses dosyası yükler (efektte 'at' = bölümdeki saniye)."""
    ep = own(s, user, Episode, eid, "Bölüm")
    if role not in ("music", "sfx"):
        raise HTTPException(400, "role 'music' ya da 'sfx' olmalı")
    v = storage.next_version(s, role=role, episode_id=eid)
    dest = storage.episode_dir(s, ep) / "ortak" / "yuklemeler" / f"{role}_v{v}{_ext(file, '.mp3')}"
    meta = await _save_upload(file, dest, "audio")
    a = Asset(user_id=user.id, project_id=ep.project_id, episode_id=eid, kind="audio", role=role, path=storage.rel_path(dest), version=v,
              step=5, provider="upload", mode="manual", status="approved", approved_by="user",
              meta={**meta, "name": file.filename}, params={"at": at, "volume": volume})
    s.add(a)
    s.flush()
    return asset_out(a)


class StepRun(BaseModel):
    mode: str = "missing"
    scene_id: int | None = None
    lang: str | None = "*"


@router.post("/episodes/{eid}/steps/{n}/run")
def run_step(eid: int, n: int, body: StepRun, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return services.run_step(s, own(s, user, Episode, eid, "Bölüm"), n, body.mode, body.scene_id, body.lang)


class AutoIn(BaseModel):
    on: bool = True
    langs: list[str] | None = None


@router.post("/episodes/{eid}/auto")
def auto_mode(eid: int, body: AutoIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    ep = own(s, user, Episode, eid, "Bölüm")
    if body.on:
        langs = [services.check_lang(l) for l in body.langs] if body.langs else None
        pipeline.start_auto(s, ep, langs)
    else:
        pipeline.stop_auto(s, ep, "", "Durduruldu")
    return episode_brief(s, ep)


# ----------------------------------------------------------------- sahneler
class SceneIn(BaseModel):
    order_no: int | None = None
    type: str | None = None
    character_ids: list[int] | None = None
    language: str | None = None
    texts: dict[str, str] | None = None
    image_prompt: str | None = None
    video_prompt: str | None = None
    image_text: dict[str, str] | None = None
    duration: float | None = None
    audio_moment: bool | None = None
    notes: str | None = None
    needs_review: bool | None = None


def _scene_full(s: Session, sc: Scene, lang: str | None = None) -> dict:
    ep = s.get(Episode, sc.episode_id)
    langs = pipeline.ep_langs(s, ep)
    lang = lang if lang in langs else langs[0]
    assets = s.scalars(select(Asset).where(Asset.scene_id == sc.id).order_by(Asset.id.desc())).all()
    jobs = s.scalars(select(Job).where(Job.scene_id == sc.id).order_by(Job.id)).all()
    d = scene_out(s, ep, sc, lang, assets, jobs)
    d["history"] = [job_out(s, j, {sc.id: sc.order_no}) for j in jobs[-14:]][::-1]
    d["project_id"] = ep.project_id
    d["languages"] = langs
    d["view_language"] = lang
    return d


@router.post("/episodes/{eid}/scenes")
def create_scene(eid: int, body: SceneIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    ep = own(s, user, Episode, eid, "Bölüm")
    data = body.model_dump(exclude_unset=True)
    no = data.pop("order_no", None) or max((x.order_no for x in ep.scenes), default=0) + 1
    sc = Scene(episode_id=eid, order_no=no, type=data.get("type") or "silent", character_ids=[])
    ep.scenes.append(sc)
    s.flush()
    services.update_scene(s, ep, sc, data)
    return _scene_full(s, sc)


@router.get("/scenes/{sid}")
def get_scene(sid: int, lang: str | None = None, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return _scene_full(s, own(s, user, Scene, sid, "Sahne"), lang)


@router.patch("/scenes/{sid}")
def update_scene(sid: int, body: SceneIn, lang: str | None = None, s: Session = Depends(get_db), user: User = Depends(current_user)):
    sc = own(s, user, Scene, sid, "Sahne")
    services.update_scene(s, s.get(Episode, sc.episode_id), sc, body.model_dump(exclude_unset=True))
    return _scene_full(s, sc, lang)


@router.delete("/scenes/{sid}")
def delete_scene(sid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    sc = own(s, user, Scene, sid, "Sahne")
    _cancel_jobs(s, scene_id=sid)
    s.delete(sc)
    return {"ok": True}


class ApproveIn(BaseModel):
    approved: bool = True


@router.post("/scenes/{sid}/texts/{lang}/approve")
def approve_text(sid: int, lang: str, body: ApproveIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    sc = own(s, user, Scene, sid, "Sahne")
    services.approve_text(s, s.get(Episode, sc.episode_id), sc, lang, body.approved)
    return _scene_full(s, sc, lang)


class GenerateIn(BaseModel):
    kind: str  # audio | image | video
    lang: str | None = None


@router.post("/scenes/{sid}/generate")
def generate_for_scene(sid: int, body: GenerateIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    sc = own(s, user, Scene, sid, "Sahne")
    return job_out(s, services.generate(s, s.get(Episode, sc.episode_id), sc, body.kind, body.lang or "*"))


class FixIn(BaseModel):
    method: str  # omni | lipsync | regen_audio | regen_scene
    instruction: str = ""
    lang: str | None = None


@router.post("/scenes/{sid}/fix")
def fix_scene(sid: int, body: FixIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    sc = own(s, user, Scene, sid, "Sahne")
    return {"jobs": services.fix(s, s.get(Episode, sc.episode_id), sc, body.method, body.instruction, body.lang)}


# ----------------------------------------------------------------- varlıklar
@router.post("/assets/{aid}/{action}")
def asset_action(aid: int, action: str, s: Session = Depends(get_db), user: User = Depends(current_user)):
    a = own(s, user, Asset, aid, "Varlık")
    if action in ("approve", "select"):
        pipeline.approve(s, a, "user")
    elif action == "reject":
        pipeline.reject(s, a)
    else:
        raise HTTPException(400, "Geçersiz işlem")
    s.flush()
    return asset_out(a)


@router.delete("/assets/{aid}")
def delete_asset(aid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    a = own(s, user, Asset, aid, "Varlık")
    if a.role not in ("music", "sfx", "character_ref"):
        raise HTTPException(400, "Üretilen sürümler silinmez; yalnızca yüklenen dosyalar kaldırılabilir")
    storage.abs_path(a.path).unlink(missing_ok=True)
    s.delete(a)
    return {"ok": True}


# ----------------------------------------------------------------- işler
@router.get("/jobs")
def list_jobs(episode_id: int | None = None, status: str | None = None, limit: int = 200, s: Session = Depends(get_db), user: User = Depends(current_user)):
    q = select(Job).where(Job.user_id == user.id, Job.kind != "claude").order_by(Job.id.desc()).limit(min(limit, 500))
    if episode_id:
        q = q.where(Job.episode_id == episode_id)
    if status == "active":
        q = q.where(Job.status.in_(pipeline.ACTIVE))
    elif status:
        q = q.where(Job.status == status)
    jobs = s.scalars(q).all()
    counts = dict(s.execute(select(Job.status, func.count(Job.id)).where(Job.user_id == user.id, Job.kind != "claude")
                            .group_by(Job.status)).all())
    return {"jobs": [job_out(s, j) for j in jobs], "counts": counts}


@router.post("/jobs/{jid}/retry")
def retry_job(jid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    j = own(s, user, Job, jid, "İş")
    if j.status not in ("failed", "canceled", "blocked"):
        raise HTTPException(409, "Yalnızca hatalı, iptal edilmiş ya da bağlantı bekleyen iş yeniden denenir")
    inp = dict(j.input or {})
    if "idempotency_key" in inp:
        inp["idempotency_key"] = uuid.uuid4().hex
    j.input = inp
    j.mode = "local" if j.provider == "local" else registry.resolve_mode(j.provider, inp)
    j.status, j.attempts, j.error, j.external_id, j.progress = "queued", 0, "", "", 0.0
    j.run_after, j.finished_at = now(), None
    pipeline.emit(s, "job", id=j.id, status="queued", episode_id=j.episode_id, scene_id=j.scene_id)
    return job_out(s, j)


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    j = own(s, user, Job, jid, "İş")
    if j.status not in pipeline.ACTIVE:
        raise HTTPException(409, "İş zaten bitmiş")
    j.status, j.finished_at, j.error = "canceled", now(), "Kullanıcı iptal etti"
    worker.cancel(jid)
    pipeline.on_job_failed(s, j)
    pipeline.emit(s, "job", id=j.id, status="canceled", episode_id=j.episode_id, scene_id=j.scene_id)
    return job_out(s, j)


@router.post("/jobs/{jid}/manual-upload")
async def manual_upload(jid: int, file: UploadFile = File(...), s: Session = Depends(get_db), user: User = Depends(current_user)):
    """'Elle adım' işinin sonucunu yükler; iş normal akışa (doğrulama -> yeni sürüm) döner."""
    j = own(s, user, Job, jid, "İş")
    if j.status != "waiting_manual":
        raise HTTPException(409, "Bu iş yükleme beklemiyor")
    kind = registry.CLASSES[j.provider].kind
    name = f"manual_{j.id}{_ext(file, {'audio': '.mp3', 'image': '.png'}.get(kind, '.mp4'))}"
    await _save_upload(file, storage.tmp_dir() / name, kind)
    j.external_id, j.status = f"manual:{name}", "running"
    pipeline.emit(s, "job", id=j.id, status="running", episode_id=j.episode_id, scene_id=j.scene_id)
    return job_out(s, j)


@router.get("/events")
async def events(user: User = Depends(current_user)):
    q = bus.subscribe(user.id)  # yalnızca bu kullanıcının olayları

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": canlı\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ----------------------------------------------------------------- sohbet (yönetmen ajanı)
def run_out(s: Session, run: AgentRun | None) -> dict | None:
    if run is None:
        return None
    st = run.state or {}
    return {"id": run.id, "status": run.status, "budget_usd": run.budget_usd, "claude_usd": round(run.claude_usd, 4),
            "spent_usd": round(pipeline.run_spent(s, run.id), 3), "turns": run.turns,
            "tokens": {"input": run.input_tokens, "output": run.output_tokens, "cache_read": run.cache_read_tokens},
            "plan_approved": run.plan_approved, "error": run.error,
            "ask": {k: v for k, v in (st.get("ask") or {}).items() if k != "tool_use_id"} or None,
            "retries": st.get("retries") or {}, "waiting_service": st.get("waiting_service"), "created_at": iso(run.created_at), "finished_at": iso(run.finished_at)}


def _text_of_blocks(blocks: list) -> str:
    return "\n".join(b.get("text", "") for b in blocks or [] if b.get("type") == "text"
                     and not b.get("text", "").startswith(("[Sistem notu", "<senaryo_dosyasi", "[Ek ")))


def message_out(s: Session, m: ChatMessage) -> list[dict]:
    """Sohbet satırları. Görsel verisi ve sistem notları arayüze gönderilmez."""
    base = {"id": m.id, "run_id": m.run_id, "created_at": iso(m.created_at)}
    if m.kind == "attachment":
        return []
    if m.kind == "note":
        return [{**base, "type": "note", "level": (m.meta or {}).get("level", "info"), "text": _text_of_blocks(m.content),
                 "result": (m.meta or {}).get("result"), "budget": (m.meta or {}).get("budget")}]
    if m.kind in ("pending_user", "merged_user"):
        return [{**base, "type": "user", "text": _text_of_blocks(m.content), "pending": m.kind == "pending_user",
                 "attachments": _attachments(s, (m.meta or {}).get("attachments") or [])}]
    if m.role == "assistant":
        blocks = []
        for b in m.content or []:
            if b.get("type") == "text" and b.get("text"):
                blocks.append({"type": "text", "text": b["text"]})
            elif b.get("type") == "thinking" and b.get("thinking"):
                blocks.append({"type": "thinking", "text": b["thinking"][:2000]})
            elif b.get("type") == "tool_use":
                blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("input")})
            elif b.get("type") == "fallback":
                blocks.append({"type": "text", "text": "(Güvenlik sınıflandırıcısı nedeniyle yedek modele geçildi.)"})
        return [{**base, "type": "assistant", "blocks": blocks, "usd": (m.meta or {}).get("usd", 0)}]
    results, tool_media = {}, (m.meta or {}).get("tool_media") or {}
    for b in m.content or []:
        if b.get("type") == "tool_result":
            c = b.get("content")
            text = c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c or [] if x.get("type") == "text")
            images = 0 if isinstance(c, str) else sum(1 for x in c or [] if x.get("type") == "image")
            results[b["tool_use_id"]] = {"is_error": bool(b.get("is_error")), "text": text[:4000], "images": images,
                                         "media": tool_media.get(b["tool_use_id"]) or []}
    out = []
    if results:
        out.append({**base, "type": "tool_results", "results": results})
    elif _text_of_blocks(m.content) or (m.meta or {}).get("attachments"):
        out.append({**base, "type": "user", "text": _text_of_blocks(m.content), "pending": False,
                    "attachments": _attachments(s, (m.meta or {}).get("attachments") or [])})
    return out


def _attachments(s: Session, ids: list[int]) -> list[dict]:
    out = []
    for i in ids:
        m = s.get(ChatMessage, i)
        if m is not None and m.kind == "attachment":
            f = m.content[0]
            out.append({"id": m.id, "name": f["name"], "kind": f["kind"], "url": f"/media/{f['path']}"})
    return out


def conversation_brief(s: Session, c: Conversation) -> dict:
    run = s.scalars(select(AgentRun).where(AgentRun.conversation_id == c.id).order_by(AgentRun.id.desc())).first()
    return {"id": c.id, "title": c.title, "project_id": c.project_id, "episode_id": c.episode_id, "mode": c.mode,
            "model": c.model, "updated_at": iso(c.updated_at), "status": run.status if run else ""}


def panel_out(s: Session, c: Conversation) -> dict | None:
    """Canlı üretim paneli: hat özeti, sahne küçük resimleri, ilerleme, anlık maliyet."""
    ep = s.get(Episode, c.episode_id) if c.episode_id else None
    if ep is None:
        return None
    summ = services.episode_summary(s, ep)
    src = summ["source_language"]
    thumbs = []
    for sc in ep.scenes:
        img, vid = pipeline.sel(s, sc, "scene_image", src), pipeline.sel(s, sc, "scene_video", src)
        draft = pipeline.has_draft(s, "scene_image", scene_id=sc.id, language=pipeline.image_lang(sc, src))
        shown = img or draft
        busy = pipeline.active_job(s, scene_id=sc.id) is not None
        thumbs.append({"id": sc.id, "no": sc.order_no, "type": sc.type, "image": f"/media/{shown.path}" if shown else None,
                       "has_video": vid is not None, "busy": busy, "needs_review": sc.needs_review,
                       "text": pipeline.text_of(s, sc, src)[:90]})
    return {**summ, "thumbs": thumbs}


@router.get("/conversations")
def list_conversations(s: Session = Depends(get_db), user: User = Depends(current_user)):
    return [conversation_brief(s, c) for c in s.scalars(select(Conversation).where(Conversation.user_id == user.id)
                                                        .order_by(Conversation.updated_at.desc()))]


class ConversationIn(BaseModel):
    title: str = ""


@router.post("/conversations")
def create_conversation(body: ConversationIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return conversation_brief(s, runner.create_conversation(s, body.title))


@router.get("/conversations/{cid}")
def get_conversation(cid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own(s, user, Conversation, cid, "Sohbet")
    rows = s.scalars(select(ChatMessage).where(ChatMessage.conversation_id == cid).order_by(ChatMessage.id)).all()
    run = s.scalars(select(AgentRun).where(AgentRun.conversation_id == cid).order_by(AgentRun.id.desc())).first()
    msgs: list[dict] = []
    for m in rows:
        msgs += message_out(s, m)
    return {**conversation_brief(s, c), "messages": msgs, "run": run_out(s, run), "panel": panel_out(s, c),
            "settings": {"budget_usd": store.get("budget_usd"), "plan_approval": store.get("plan_approval")}}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own(s, user, Conversation, cid, "Sohbet")
    run = runner.open_run(s, cid)
    if run is not None:
        runner.stop_run(s, run, "Sohbet silindi")
    for model in (ChatMessage, AgentRun):
        for row in s.scalars(select(model).where(model.conversation_id == cid)):
            s.delete(row)
    s.delete(c)
    storage.delete_tree(f"u{user.id}/_sohbet/{cid}")
    return {"ok": True}


class MessageIn(BaseModel):
    text: str = ""
    attachment_ids: list[int] = []


@router.post("/conversations/{cid}/messages")
def post_message(cid: int, body: MessageIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own(s, user, Conversation, cid, "Sohbet")
    c.updated_at = now()
    return runner.post_message(s, c, body.text, body.attachment_ids)


@router.post("/conversations/{cid}/attachments")
async def upload_attachment(cid: int, file: UploadFile = File(...), s: Session = Depends(get_db), user: User = Depends(current_user)):
    """Sohbete dosya ekler: senaryo (metin/CSV/JSON), referans görsel, müzik ya da efekt."""
    own(s, user, Conversation, cid, "Sohbet")
    ext = _ext(file, ".txt")
    kind = ("text" if ext in (".txt", ".csv", ".json", ".tsv", ".md") else
            "image" if ext in (".png", ".jpg", ".jpeg", ".webp") else
            "audio" if ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg") else "")
    if not kind:
        raise HTTPException(400, "Desteklenen ekler: metin/CSV/JSON senaryo, PNG/JPG görsel, MP3/WAV ses")
    dest = storage.user_dir(user.id) / "_sohbet" / str(cid) / f"{uuid.uuid4().hex[:8]}{ext}"
    await _save_upload(file, dest, kind)
    m = ChatMessage(conversation_id=cid, kind="attachment", role="user",
                    content=[{"type": "file", "name": file.filename or dest.name, "kind": kind, "path": storage.rel_path(dest)}])
    s.add(m)
    s.flush()
    return {"id": m.id, "name": file.filename, "kind": kind, "url": f"/media/{storage.rel_path(dest)}"}


@router.post("/conversations/{cid}/stop")
def stop_conversation(cid: int, s: Session = Depends(get_db), user: User = Depends(current_user)):
    own(s, user, Conversation, cid, "Sohbet")
    run = runner.open_run(s, cid)
    if run is None:
        raise HTTPException(409, "Çalışan bir akış yok")
    runner.stop_run(s, run, "Kullanıcı durdurdu")
    return run_out(s, run)


class ResumeIn(BaseModel):
    add_budget: float = 0.0


@router.post("/conversations/{cid}/resume")
def resume_conversation(cid: int, body: ResumeIn, s: Session = Depends(get_db), user: User = Depends(current_user)):
    own(s, user, Conversation, cid, "Sohbet")
    run = s.scalars(select(AgentRun).where(AgentRun.conversation_id == cid).order_by(AgentRun.id.desc())).first()
    if run is None:
        raise HTTPException(409, "Sürdürülecek çalışma yok")
    runner.resume_run(s, run, max(body.add_budget, 0.0))
    return run_out(s, run)


# ----------------------------------------------------------------- maliyet
@router.get("/costs")
def costs(s: Session = Depends(get_db), user: User = Depends(current_user)):
    by_provider = []
    q = (select(Job.provider, Job.mode, func.sum(Job.cost_usd), func.sum(Job.cost_units), func.max(Job.cost_unit),
                func.count(Job.id)).where(Job.user_id == user.id, Job.status == "succeeded", Job.provider != "local")
         .group_by(Job.provider, Job.mode))
    for prov, mode, usd, units, unit, n in s.execute(q):
        label = "Claude (yönetmen ajanı)" if prov == "claude" else registry.cfg(prov).get("label", prov)
        by_provider.append({"provider": prov, "label": label, "mode": mode,
                            "usd": round(usd or 0, 4), "units": round(units or 0, 2), "unit": unit, "jobs": n})
    projects = []
    for p in s.scalars(select(Project).where(Project.user_id == user.id).order_by(Project.id)):
        projects.append({"id": p.id, "name": p.name, "cost": services.cost_of(s, project_id=p.id),
                         "episodes": [{"id": e.id, "number": e.number, "title": e.title,
                                       "cost": services.cost_of(s, episode_id=e.id),
                                       "by_language": services.cost_by_language(s, e)} for e in p.episodes]})
    return {"total": services.cost_of(s, user_id=user.id), "by_provider": by_provider, "projects": projects}


@router.post("/seed")
def seed(s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = seed_mod.seed(s, force=True)
    return project_out(s, p, True)
