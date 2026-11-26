"""Kullanıcı bağlantıları: servis kimlik bilgilerinin şifreli saklanması, çözülmesi ve "bağlantıyı test et".

- Yalnızca resmi API kimlik bilgileri saklanır; servislerin e-posta/şifre ile hesap girişi asla istenmez.
- Kaydedilen değer arayüze geri dönmez; yalnızca son 4 karakter gösterilir.
- user_id = None satırı yöneticinin tanımladığı ortak (kurum) anahtarıdır; kullanıcı kendi anahtarını
  girmediyse ve aylık sınırı dolmadıysa o kullanılır.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select

from . import crypto, settings_store as store
from .config import get_models
from .db import session_scope
from .models import AgentRun, Connection, Conversation, Job, User, now

# servis -> alanlar (ad, etiket, zorunlu mu)
FIELDS = {
    "anthropic": [("api_key", "API anahtarı", True)],
    "elevenlabs": [("api_key", "API anahtarı", True)],
    # Güncel dokümana göre bütün uçlar (eski /v1 dudak senkronu dahil) tek API anahtarıyla çalışır.
    # Webhook Secret isteğe bağlıdır: Kling konsolunda oluşturulursa geri çağrıların imzası doğrulanır.
    "kling": [("api_key", "API anahtarı", True),
              ("webhook_secret", "Webhook Secret (isteğe bağlı, whsec_...)", False)],
    "heygen": [("api_key", "API anahtarı", True)],
}
INFO = {
    "anthropic": {"label": "Anthropic (Claude)", "required": True, "url": "https://console.anthropic.com/settings/keys",
                  "purpose": "Sohbetteki yönetmen ajanı ve çeviri bununla çalışır.", "steps": []},
    "elevenlabs": {"label": "ElevenLabs", "required": False, "url": "https://elevenlabs.io/app/developers/api-keys",
                   "purpose": "Bütün karakter sesleri ve anlatıcı (1. adım).", "steps": [1]},
    "kling": {"label": "Kling", "required": False, "url": "https://kling.ai/dev/api-key",
              "purpose": "Karakter ve sahne görselleri, konuşmasız videolar, Omni düzeltme ve dudak senkronu (2, 4 ve 7. adımlar).",
              "steps": [2, 4, 7]},
    "heygen": {"label": "HeyGen", "required": False, "url": "https://app.heygen.com/settings?nav=API",
               "purpose": "Konuşan karakter sahneleri (3. adım).", "steps": [3]},
}
PROVIDER_SERVICE = {"elevenlabs_tts": "elevenlabs", "kling_image": "kling", "kling_video": "kling", "kling_omni": "kling",
                    "kling_lipsync": "kling", "heygen_avatar": "heygen", "claude": "anthropic"}

# testlerin sahte HTTP katmanı takabilmesi için
transport_override: httpx.AsyncBaseTransport | None = None
anthropic_http_client = None


def _row(s, user_id: int | None, service: str) -> Connection | None:
    q = select(Connection).where(Connection.service == service)
    q = q.where(Connection.user_id.is_(None)) if user_id is None else q.where(Connection.user_id == user_id)
    return s.scalars(q).first()


def month_start() -> datetime:
    d = datetime.now(timezone.utc).replace(tzinfo=None)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def shared_spent(s, user_id: int) -> float:
    q = select(func.sum(Job.cost_usd)).where(Job.user_id == user_id, Job.cred_source == "shared", Job.mode == "real",
                                             Job.created_at >= month_start(), Job.status.notin_(("failed", "canceled")))
    return float(s.execute(q).scalar() or 0.0)


def shared_limit(s, user_id: int) -> float:
    u = s.get(User, user_id)
    own = (u.limits or {}).get("shared_monthly_usd") if u else None
    return float(own if own is not None else store.sys_get("shared_monthly_usd") or 0)


def resolve(service: str, user_id: int | None, check_limit: bool = True) -> tuple[dict | None, str]:
    """(kimlik bilgileri, kaynak). Kaynak: 'own' | 'shared' | ''. Önce kullanıcının kendi anahtarı."""
    if user_id is None:
        return None, ""
    with session_scope() as s:
        row = _row(s, user_id, service)
        if row is not None and row.secret:
            creds = crypto.decrypt_json(row.secret)
            if creds:
                return creds, "own"
        shared = _row(s, None, service)
        if shared is not None and shared.secret:
            creds = crypto.decrypt_json(shared.secret)
            if creds:
                return creds, "shared"
    return None, ""


def save(user_id: int | None, service: str, values: dict) -> None:
    if service not in FIELDS:
        raise ValueError("Bilinmeyen servis")
    clean = {}
    for name, label, required in FIELDS[service]:
        v = (values.get(name) or "").strip()
        if required and not v:
            raise ValueError(f"{label} boş olamaz")
        if v:
            if len(v) > 600 or any(c.isspace() for c in v):
                raise ValueError(f"{label} geçersiz görünüyor")
            clean[name] = v
    with session_scope() as s:
        row = _row(s, user_id, service) or Connection(user_id=user_id, service=service)
        row.secret = crypto.encrypt_json(clean)
        row.last4 = crypto.last4(clean[FIELDS[service][0][0]])
        row.status, row.message, row.info, row.tested_at = "untested", "", {}, None
        s.add(row)


def delete(user_id: int | None, service: str) -> bool:
    with session_scope() as s:
        row = _row(s, user_id, service)
        if row is None:
            return False
        s.delete(row)
        return True


def view(user_id: int | None) -> list[dict]:
    """Arayüze giden görünüm: gizli değer asla dönmez, yalnızca son 4 karakter."""
    out = []
    with session_scope() as s:
        for service, meta in INFO.items():
            row = _row(s, user_id, service)
            shared = _row(s, None, service) if user_id is not None else None
            mode = "connected" if row and row.secret else ("shared" if shared and shared.secret else "demo")
            out.append({
                "service": service, **meta,
                "fields": [{"name": n, "label": l, "required": r} for n, l, r in FIELDS[service]],
                "set": bool(row and row.secret), "last4": row.last4 if row else "",
                "status": row.status if row else "", "message": row.message if row else "",
                "info": (row.info or {}) if row else {}, "tested_at": row.tested_at.isoformat() + "Z" if row and row.tested_at else None,
                "mode": mode,  # connected: kendi anahtarı · shared: ortak anahtar · demo: deneme modu (mock)
                "shared_available": bool(shared and shared.secret),
            })
    return out


def summary(user_id: int) -> dict:
    """Üst çubuk ve ajan için: hangi servis bağlı, hangisi deneme modunda, hangi adımlar etkileniyor."""
    rows = view(user_id)
    demo_steps = sorted({st for r in rows if r["mode"] == "demo" for st in r["steps"]})
    return {"services": [{"service": r["service"], "label": r["label"], "mode": r["mode"], "status": r["status"],
                          "steps": r["steps"]} for r in rows],
            "demo_steps": demo_steps, "all_connected": all(r["mode"] != "demo" for r in rows)}


# ------------------------------------------------------------------ bağlantı testleri (gerçek, ücretsiz çağrılar)
def _client() -> httpx.AsyncClient:
    kw: dict = {"timeout": httpx.Timeout(25.0, connect=10.0)}
    if transport_override is not None:
        kw["transport"] = transport_override
    return httpx.AsyncClient(**kw)


def _result(ok: bool, message: str, **info) -> dict:
    return {"ok": ok, "message": message, "info": info}


async def test_anthropic(creds: dict) -> dict:
    """GET /v1/models: ücretsiz, anahtarı doğrular. Kalan bakiye normal API anahtarıyla sorgulanamaz."""
    import anthropic
    kw: dict = {"api_key": creds["api_key"], "max_retries": 1, "timeout": 25.0}
    if anthropic_http_client is not None:
        kw["http_client"] = anthropic_http_client
    client = anthropic.AsyncAnthropic(**kw)
    try:
        page = await client.models.list(limit=100)
        ids = [m.id for m in page.data]
    except anthropic.AuthenticationError:
        return _result(False, "API anahtarı geçersiz.")
    except anthropic.PermissionDeniedError:
        return _result(False, "Bu anahtarın yetkisi yok (çalışma alanı ya da izin kısıtı).")
    except anthropic.RateLimitError:
        return _result(True, "Anahtar geçerli (şu an hız sınırında).", models=[])
    except anthropic.APIConnectionError:
        return _result(False, "Anthropic API'ye ulaşılamadı; ağ bağlantısını kontrol edin.")
    except anthropic.APIStatusError as e:
        return _result(False, f"Anthropic API hatası ({e.status_code}).")
    want = store.claude_model()
    return _result(True, f"Bağlandı. {len(ids)} model erişilebilir." + ("" if want in ids else f" Seçili model ({want}) bu hesapta görünmüyor."),
                   models=ids[:40], selected_model_available=want in ids,
                   credit="Kalan bakiye API anahtarıyla sorgulanamıyor; Anthropic Console'dan bakın.")


async def test_elevenlabs(creds: dict) -> dict:
    base = get_models()["providers"]["elevenlabs_tts"]["base_url"]
    h = {"xi-api-key": creds["api_key"]}
    async with _client() as c:
        try:
            r = await c.get(f"{base}/v1/user/subscription", headers=h)
        except httpx.HTTPError:
            return _result(False, "ElevenLabs API'ye ulaşılamadı.")
        if r.status_code in (401, 403):
            return _result(False, "API anahtarı geçersiz ya da yetkisi eksik (ses ve kullanıcı okuma izni gerekir).")
        if r.status_code >= 400:
            return _result(False, f"ElevenLabs API hatası ({r.status_code}).")
        sub = r.json()
        left = max(int(sub.get("character_limit") or 0) - int(sub.get("character_count") or 0), 0)
        voices = []
        try:
            rv = await c.get(f"{base}/v2/voices", params={"page_size": 100}, headers=h)
            if rv.status_code < 400:
                voices = [{"voice_id": v.get("voice_id"), "name": v.get("name"), "labels": v.get("labels") or {}}
                          for v in rv.json().get("voices", [])]
        except httpx.HTTPError:
            pass
    return _result(True, f"Bağlandı. Plan: {sub.get('tier', '?')}, kalan {left:,} karakter. Ses kütüphanesinde {len(voices)} ses.".replace(",", "."),
                   tier=sub.get("tier"), credit=f"{left} karakter", characters_left=left, voices=voices[:60], voice_count=len(voices))


KLING_FEATURES = (("image", "Görsel", ("image", "kolors")), ("video", "Video", ("video", "kling-v", "kling-3", "kling-4", "kling-2")),
                  ("omni", "Omni (video düzeltme)", ("omni", "o1")), ("lipsync", "Dudak senkronu", ("lip",)))


async def test_kling(creds: dict) -> dict:
    """GET /account/costs: ücretsiz hesap sorgusu (kaynak paketleri ve kalan miktar). Anahtarı doğrular.

    Kling'de yetki sorgusu yoktur; hangi özelliğin kullanılabildiği, paketlerin geçerli olduğu model
    adlarından TAHMİN edilir ve ilk gerçek çağrıda kesinleşir (yetki yoksa 1103 hatası döner). Ücretli
    olabilecek hiçbir deneme isteği gönderilmez.
    """
    import time
    base = get_models()["providers"]["kling_video"]["base_url"].rstrip("/")
    h = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
    end_ms = int(time.time() * 1000)
    async with _client() as c:
        try:
            r = await c.get(f"{base}/account/costs", params={"start_time": end_ms - 30 * 86400 * 1000, "end_time": end_ms}, headers=h)
        except httpx.HTTPError:
            return _result(False, "Kling API'ye ulaşılamadı.")
    code = _kling_code(r)
    if r.status_code == 401 or code in (1000, 1001, 1002, 1003, 1004):
        return _result(False, "API anahtarı geçersiz ya da süresi dolmuş.")
    if code == 1304:
        return _result(False, "Bu sunucunun IP adresi Kling hesabındaki beyaz listede değil.")
    if r.status_code >= 400 and code not in (1100, 1101, 1102):
        return _result(False, f"Kling API hatası (HTTP {r.status_code}, kod {code}).")
    packs = []
    try:
        packs = ((r.json().get("data") or {}).get("resource_pack_subscribe_infos")) or []
    except Exception:
        pass
    online = [p for p in packs if p.get("status") == "online"]
    names = " ".join(str(p.get("resource_pack_name", "")) + " " + str(p.get("resource_pack_applicable_models", "")) for p in online).lower()
    features = {}
    for key, label, words in KLING_FEATURES:
        state = "paket var" if online and any(w in names for w in words) else ("belirsiz" if online else "paket yok")
        features[key] = {"label": label, "state": state}
    left = [{"name": p.get("resource_pack_name"), "remaining": p.get("remaining_quantity"), "total": p.get("total_quantity"),
             "status": p.get("status")} for p in packs[:12]]
    if code in (1100, 1101, 1102):
        return _result(False, "Anahtar geçerli ama hesapta sorun var (borç ya da kaynak paketi bitmiş).", packs=left, features=features)
    credit = ", ".join(f"{p['name']}: {p['remaining']}" for p in left if p["status"] == "online")[:200]
    msg = f"Bağlandı. {len(online)} etkin kaynak paketi." if packs else "Bağlandı. Etkin kaynak paketi görünmüyor (kullandıkça öde hesabı olabilir)."
    return _result(True, msg, packs=left, features=features, credit=credit or "Kaynak paketi yok ya da görünmüyor",
                   note="Özellik erişimi paket adlarından tahmin edilir; yetki yoksa ilk çağrıda hata olarak bildirilir.",
                   webhook_signed=bool(creds.get("webhook_secret")))


def _kling_code(r: httpx.Response):
    try:
        return r.json().get("code")
    except Exception:
        return None


async def test_heygen(creds: dict) -> dict:
    base = get_models()["providers"]["heygen_avatar"]["base_url"].rstrip("/")
    async with _client() as c:
        try:
            r = await c.get(f"{base}/v3/users/me", headers={"X-Api-Key": creds["api_key"]})
        except httpx.HTTPError:
            return _result(False, "HeyGen API'ye ulaşılamadı.")
    if r.status_code in (401, 403):
        return _result(False, "API anahtarı geçersiz ya da kapsamı yetersiz.")
    if r.status_code >= 400:
        return _result(False, f"HeyGen API hatası ({r.status_code}).")
    d = r.json().get("data") or {}
    bt = d.get("billing_type") or ""
    w = d.get(bt) if isinstance(d.get(bt), dict) else d
    credit = ""
    if bt == "wallet":
        credit = f"{w.get('remaining_balance', '?')} {str(w.get('currency', '')).upper()}"
    elif bt == "subscription":
        credit = f"{((w.get('credits') or {}).get('premium_credits') or {}).get('remaining', '?')} kredi"
    elif bt == "usage_based":
        credit = f"{w.get('remaining_credits', '?')} kredi"
    models = [{"id": m["id"], "label": m.get("label", m["id"])} for m in get_models()["providers"]["heygen_avatar"]["models"]]
    # kullanıcının hazır avatar görünümleri (GET /v3/avatars/looks); kapsam yoksa boş kalır
    looks = []
    try:
        async with _client() as c:
            rl = await c.get(f"{base}/v3/avatars/looks", params={"ownership": "private", "limit": 20}, headers={"X-Api-Key": creds["api_key"]})
        if rl.status_code < 400:
            looks = [{"id": x.get("id"), "name": x.get("name"), "engines": x.get("supported_api_engines") or []}
                     for x in (rl.json().get("data") or [])]
    except Exception:
        pass
    return _result(True, "Bağlandı." + (f" Kalan: {credit}." if credit else "") + f" {len(models)} motor, {len(looks)} hazır avatar görünümü.",
                   billing_type=bt, credit=credit, models=models, looks=looks[:20],
                   note="Anahtarın şu kapsamları olmalı: account:read, assets:write, videos:write, videos:read.")


TESTS = {"anthropic": test_anthropic, "elevenlabs": test_elevenlabs, "kling": test_kling, "heygen": test_heygen}


async def test(user_id: int | None, service: str) -> dict:
    """Kayıtlı bağlantıyı sınar, sonucu kaydeder ve bağlantı düzeldiyse bekleyen işleri sürdürür."""
    with session_scope() as s:
        row = _row(s, user_id, service)
        creds = crypto.decrypt_json(row.secret) if row and row.secret else None
    if not creds:
        return _result(False, "Bu servis için kayıtlı anahtar yok.")
    try:
        res = await TESTS[service](creds)
    except Exception as e:  # sınama hiçbir koşulda gizli değeri sızdırmamalı
        res = _result(False, f"Bağlantı sınanamadı ({type(e).__name__}).")
    with session_scope() as s:
        row = _row(s, user_id, service)
        if row is not None:
            row.status = "connected" if res["ok"] else "error"
            row.message, row.info, row.tested_at = res["message"], res["info"], now()
    if res["ok"]:
        unblock(user_id, service)
    return res


def mark_failed(user_id: int | None, service: str, message: str) -> None:
    """Üretim sırasında anahtar geçersiz/kredisiz çıktı: bağlantıyı hatalı işaretle."""
    with session_scope() as s:
        for uid in (user_id, None):
            row = _row(s, uid, service)
            if row is not None and (uid is not None or store.cred_source(service, user_id) == "shared"):
                row.status, row.message = "error", message[:300]
                break


def unblock(user_id: int | None, service: str) -> int:
    """Bağlantı yenilendi: bu servisi bekleyen işler ve ajan çalışmaları kaldığı yerden sürer."""
    from . import pipeline
    n = 0
    with session_scope() as s:
        q = select(Job).where(Job.status == "blocked")
        if user_id is not None:
            q = q.where(Job.user_id == user_id)
        for j in s.scalars(q):
            if PROVIDER_SERVICE.get(j.provider) == service:
                # external_id korunur: sağlayıcıya gönderilmiş iş yeniden gönderilmez, yoklaması sürer
                j.status, j.error, j.attempts, j.run_after = "queued", "", 0, now()
                n += 1
                pipeline.emit(s, "job", id=j.id, status="queued", episode_id=j.episode_id, scene_id=j.scene_id, user_id=j.user_id)
        rq = select(AgentRun).join(Conversation, Conversation.id == AgentRun.conversation_id).where(AgentRun.status == "waiting_connection")
        if user_id is not None:
            rq = rq.where(Conversation.user_id == user_id)
        for run in s.scalars(rq):
            if (run.state or {}).get("waiting_service") in (service, None):
                run.status, run.error = "queued", ""
                pipeline.emit(s, "chat", conversation_id=run.conversation_id, event="status")
    return n
