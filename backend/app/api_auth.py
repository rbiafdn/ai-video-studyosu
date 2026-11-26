"""Hesap, oturum, kurulum sihirbazı (bağlantılar + tercihler), bildirimler, yönetici paneli ve webhook uçları."""
from __future__ import annotations

import base64
import hashlib
import hmac
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth, connections, crypto, languages, pipeline, services, settings_store as store, storage, tenancy
from .agent import llm as llm_mod
from .api import CommitRoute, iso
from .auth import admin_user, audit, client_ip, current_user
from .config import get_models, get_settings
from .db import get_db
from .models import (AgentRun, Asset, AuditLog, AuthSession, AuthToken, ChatMessage, Connection, Conversation, Job,
                     Notification, Project, User, now)
from .providers import registry
from .worker import worker

router = APIRouter(prefix="/api", route_class=CommitRoute)


# ----------------------------------------------------------------- oturum
class Credentials(BaseModel):
    email: str
    password: str
    invite: str | None = None


def me_out(s: Session, user: User, csrf: str) -> dict:
    with tenancy.as_user(user.id):
        unread = s.execute(select(func.count(Notification.id)).where(Notification.user_id == user.id,
                                                                      Notification.read_at.is_(None))).scalar() or 0
        return {"user": auth.user_out(user), "csrf": csrf, "prefs": store.prefs(user.id),
                "connections": connections.summary(user.id), "director": llm_mod.llm_mode(),
                "demo_mode": bool(store.sys_get("demo_mode")), "unread": int(unread)}


@router.get("/auth/state")
def auth_state(s: Session = Depends(get_db)):
    """Giriş ekranı için herkese açık durum: ilk kurulum mu, kayıt açık mı."""
    has_admin = bool(s.scalars(select(User.id).where(User.role == "admin")).first())
    return {"first_run": not has_admin, "registration": "open" if not has_admin else store.sys_get("registration_mode"),
            "smtp": get_settings().smtp_enabled}


@router.post("/auth/register")
def register(body: Credentials, request: Request, response: Response, s: Session = Depends(get_db)):
    if not auth.limiter.hit(f"register:{client_ip(request)}", 10, 3600):
        raise HTTPException(429, "Çok fazla kayıt denemesi. Daha sonra yeniden deneyin.")
    u = auth.register(s, body.email, body.password, body.invite, client_ip(request))
    sess = auth.start_session(s, u, request, response)
    if not u.email_verified and get_settings().smtp_enabled:
        token = auth.make_token(s, "verify", email=u.email, user_id=u.id)
        auth.send_mail(u.email, "E-posta adresinizi doğrulayın", "Doğrulamak için bağlantıya tıklayın:\n" + auth.link("/dogrula", token, request))
    s.flush()
    return me_out(s, u, sess.csrf)


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response, s: Session = Depends(get_db)):
    u = auth.login(s, body.email, body.password, request, response)
    s.flush()
    sess = s.scalars(select(AuthSession).where(AuthSession.user_id == u.id).order_by(AuthSession.id.desc())).first()
    return me_out(s, u, sess.csrf)


@router.post("/auth/logout")
def logout(request: Request, response: Response, s: Session = Depends(get_db), user: User = Depends(current_user)):
    auth.end_session(s, request, response)
    audit(s, user.id, "logout", "", client_ip(request))
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    return me_out(s, user, request.state.csrf)


class EmailIn(BaseModel):
    email: str


@router.post("/auth/forgot")
def forgot(body: EmailIn, request: Request, s: Session = Depends(get_db)):
    """Her zaman aynı yanıt döner (hesabın var olup olmadığı sızdırılmaz)."""
    if not auth.limiter.hit(f"forgot:{client_ip(request)}", 5, 900):
        raise HTTPException(429, "Çok fazla istek. Daha sonra yeniden deneyin.")
    email = (body.email or "").strip().lower()
    u = s.scalars(select(User).where(User.email == email)).first()
    if u is not None and u.status == "active" and get_settings().smtp_enabled:
        token = auth.make_token(s, "reset", email=u.email, user_id=u.id)
        auth.send_mail(u.email, "Şifre sıfırlama", "Şifrenizi sıfırlamak için (2 saat geçerli):\n" + auth.link("/sifre-sifirla", token, request))
        audit(s, u.id, "reset_requested", "", client_ip(request))
    return {"ok": True, "smtp": get_settings().smtp_enabled,
            "message": "Hesap varsa sıfırlama bağlantısı e-postayla gönderildi." if get_settings().smtp_enabled
            else "E-posta gönderimi ayarlı değil: yöneticinizden tek kullanımlık sıfırlama bağlantısı isteyin."}


class ResetIn(BaseModel):
    token: str
    password: str


@router.post("/auth/reset")
def reset(body: ResetIn, request: Request, s: Session = Depends(get_db)):
    auth.check_password_rules(body.password)
    t = auth.use_token(s, "reset", body.token)
    u = s.get(User, t.user_id) if t else None
    if u is None:
        raise HTTPException(400, "Bağlantı geçersiz ya da süresi dolmuş")
    u.password_hash, u.failed_logins, u.locked_until = auth.hash_password(body.password), 0, None
    for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == u.id)):
        s.delete(sess)  # bütün oturumlar kapanır
    audit(s, u.id, "password_reset", "", client_ip(request))
    return {"ok": True}


class TokenIn(BaseModel):
    token: str


@router.post("/auth/verify")
def verify_email(body: TokenIn, s: Session = Depends(get_db)):
    t = auth.use_token(s, "verify", body.token)
    u = s.get(User, t.user_id) if t else None
    if u is None:
        raise HTTPException(400, "Bağlantı geçersiz ya da süresi dolmuş")
    u.email_verified = True
    return {"ok": True}


# ----------------------------------------------------------------- hesap
class AccountIn(BaseModel):
    current_password: str
    email: str | None = None
    new_password: str | None = None


@router.patch("/account")
def update_account(body: AccountIn, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    if not auth.verify_password(user.password_hash, body.current_password):
        raise HTTPException(403, "Mevcut şifre hatalı")
    if body.email and body.email.strip().lower() != user.email:
        email = auth.norm_email(body.email)
        if s.scalars(select(User).where(User.email == email)).first():
            raise HTTPException(409, "Bu e-posta kullanımda")
        user.email, user.email_verified = email, False
        audit(s, user.id, "email_changed", "", client_ip(request))
    if body.new_password:
        auth.check_password_rules(body.new_password)
        user.password_hash = auth.hash_password(body.new_password)
        cur = crypto.token_hash(request.cookies.get(auth.COOKIE, ""))
        for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.token_hash != cur)):
            s.delete(sess)  # diğer cihazlardaki oturumlar kapanır
        audit(s, user.id, "password_changed", "", client_ip(request))
    return auth.user_out(user)


def delete_user_data(s: Session, user: User) -> None:
    """Hesabın bütün verisi: projeler, varlıklar, işler, sohbetler, anahtarlar, oturumlar ve dosyalar."""
    for j in s.scalars(select(Job).where(Job.user_id == user.id, Job.status.in_(pipeline.ACTIVE))):
        worker.cancel(j.id)
    for run in s.scalars(select(AgentRun).join(Conversation, Conversation.id == AgentRun.conversation_id)
                         .where(Conversation.user_id == user.id)):
        worker.cancel_run(run.id)
        s.delete(run)
    conv_ids = s.scalars(select(Conversation.id).where(Conversation.user_id == user.id)).all()
    if conv_ids:
        for m in s.scalars(select(ChatMessage).where(ChatMessage.conversation_id.in_(conv_ids))):
            s.delete(m)
    for model in (Conversation, Asset, Job):
        for row in s.scalars(select(model).where(model.user_id == user.id)):
            s.delete(row)
    for p in s.scalars(select(Project).where(Project.user_id == user.id)):
        s.delete(p)  # karakterler, bölümler, sahneler, metinler birlikte silinir
    for model in (Connection, AuthSession, Notification):
        for row in s.scalars(select(model).where(model.user_id == user.id)):
            s.delete(row)
    for row in s.scalars(select(AuthToken).where(AuthToken.user_id == user.id)):
        s.delete(row)
    storage.delete_tree(f"u{user.id}")
    s.delete(user)


class DeleteIn(BaseModel):
    password: str


@router.post("/account/delete")
def delete_account(body: DeleteIn, request: Request, response: Response, s: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    if not auth.verify_password(user.password_hash, body.password):
        raise HTTPException(403, "Şifre hatalı")
    if user.role == "admin":
        others = s.execute(select(func.count(User.id)).where(User.role == "admin", User.id != user.id, User.status == "active")).scalar()
        rest = s.execute(select(func.count(User.id)).where(User.id != user.id, User.is_demo.is_(False))).scalar()
        if not others and rest:
            raise HTTPException(409, "Tek yöneticisiniz: önce başka bir kullanıcıyı yönetici yapın")
    audit(s, None, "account_deleted", f"kullanıcı {user.id}", client_ip(request))
    delete_user_data(s, user)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


# ----------------------------------------------------------------- bağlantılar (kurulum sihirbazı)
class ConnectionIn(BaseModel):
    values: dict[str, str]


@router.get("/connections")
def list_connections(user: User = Depends(current_user)):
    return {"connections": connections.view(user.id), "summary": connections.summary(user.id)}


def _save_connection(s: Session, owner: int | None, service: str, values: dict, actor: User, ip: str) -> None:
    if service not in connections.FIELDS:
        raise HTTPException(404, "Servis bulunamadı")
    try:
        connections.save(owner, service, values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(s, actor.id, "connection_saved", f"{service}{' (ortak)' if owner is None else ''}", ip)


@router.put("/connections/{service}")
async def save_connection(service: str, body: ConnectionIn, request: Request, s: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    """Anahtarı şifreleyip kaydeder ve hemen sınar. Yanıtta anahtar dönmez; yalnızca son 4 karakter."""
    _save_connection(s, user.id, service, body.values, user, client_ip(request))
    s.commit()
    res = await connections.test(user.id, service)
    return {"result": res, "connection": next(c for c in connections.view(user.id) if c["service"] == service)}


@router.post("/connections/{service}/test")
async def test_connection(service: str, request: Request, user: User = Depends(current_user)):
    if service not in connections.FIELDS:
        raise HTTPException(404, "Servis bulunamadı")
    if not auth.limiter.hit(f"conntest:{user.id}", 30, 300):
        raise HTTPException(429, "Çok sık sınama yapıldı; biraz bekleyin.")
    res = await connections.test(user.id, service)
    return {"result": res, "connection": next(c for c in connections.view(user.id) if c["service"] == service)}


@router.delete("/connections/{service}")
def delete_connection(service: str, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    if not connections.delete(user.id, service):
        raise HTTPException(404, "Bağlantı bulunamadı")
    audit(s, user.id, "connection_deleted", service, client_ip(request))
    return {"connections": connections.view(user.id)}


# ----------------------------------------------------------------- tercihler ve yapılandırma
class PrefsIn(BaseModel):
    default_language: str | None = None
    budget_usd: float | None = None
    plan_approval: bool | None = None
    output_format: str | None = None
    models: dict[str, str] | None = None
    claude_model: str | None = None
    concurrency: dict[str, int] | None = None


def config_out(user: User) -> dict:
    cl = get_models()["claude"]
    return {"prefs": store.prefs(user.id), "providers": registry.describe(), "montage": get_models()["montage"],
            "ffmpeg": bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")),
            "claude": {"mode": llm_mod.llm_mode(), "model": store.claude_model(), "default": cl["default"], "effort": cl.get("effort"),
                       "models": [{"id": m["id"], "label": m.get("label", m["id"]), "input": m["input"], "output": m["output"]}
                                  for m in cl["models"]]},
            "languages": languages.matrix(), "connections": connections.summary(user.id)}


@router.get("/config")
def get_config(user: User = Depends(current_user)):
    return config_out(user)


@router.put("/prefs")
def put_prefs(body: PrefsIn, user: User = Depends(current_user)):
    data = body.model_dump(exclude_unset=True)
    if data.get("default_language"):
        data["default_language"] = services.check_lang(data["default_language"])
    if data.get("budget_usd") is not None:
        data["budget_usd"] = max(0.0, min(float(data["budget_usd"]), 10000.0))
    if data.get("output_format") not in (None, "16:9", "9:16"):
        raise HTTPException(400, "Çıktı biçimi 16:9 ya da 9:16 olmalı")
    if data.get("concurrency"):
        data["concurrency"] = {k: max(1, min(int(v), 10)) for k, v in data["concurrency"].items() if k in store.pref_defaults()["concurrency"]}
    store.put_prefs(user.id, data)
    return config_out(user)


@router.post("/setup/complete")
def setup_complete(s: Session = Depends(get_db), user: User = Depends(current_user)):
    """Sihirbaz biter: Anthropic bağlı olmalı (ya da yönetici demo modunu açmış olmalı)."""
    if llm_mod.llm_mode() == "none":
        raise HTTPException(409, "Sohbet için Anthropic (Claude) bağlantısı zorunlu. Anahtarınızı girip bağlantıyı test edin.")
    user.setup_done = True
    return {"ok": True, "connections": connections.summary(user.id)}


# ----------------------------------------------------------------- bildirimler
@router.get("/notifications")
def notifications(s: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = s.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(40)).all()
    return [{"id": n.id, "level": n.level, "text": n.text, "link": n.link, "read": n.read_at is not None, "created_at": iso(n.created_at)}
            for n in rows]


@router.post("/notifications/read")
def notifications_read(s: Session = Depends(get_db), user: User = Depends(current_user)):
    for n in s.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))):
        n.read_at = now()
    return {"ok": True}


# ----------------------------------------------------------------- yönetici paneli
def usage_of(s: Session, uid: int) -> dict:
    with tenancy.as_user(uid):
        c = services.cost_of(s, user_id=uid)
    return {**c, "shared_month_usd": round(connections.shared_spent(s, uid), 3), "shared_limit_usd": connections.shared_limit(s, uid),
            "projects": int(s.execute(select(func.count(Project.id)).where(Project.user_id == uid)).scalar() or 0)}


@router.get("/admin/overview")
def admin_overview(s: Session = Depends(get_db), user: User = Depends(admin_user)):
    st = get_settings()
    users = s.scalars(select(User).order_by(User.id)).all()
    invites = s.scalars(select(AuthToken).where(AuthToken.kind == "invite", AuthToken.used_at.is_(None), AuthToken.expires_at > now())
                        .order_by(AuthToken.id.desc())).all()
    total = {"real_usd": 0.0, "mock_usd": 0.0, "claude_usd": 0.0, "jobs": 0}
    rows = []
    for u in users:
        usage = usage_of(s, u.id)
        for k in total:
            total[k] = round(total[k] + usage[k], 4)
        # yönetici, kullanıcıların anahtarlarını göremez: yalnızca hangi servisin bağlı olduğu
        rows.append({**auth.user_out(u), "usage": usage,
                     "connected": [c["service"] for c in connections.view(u.id) if c["set"]]})
    return {
        "users": rows, "total": total,
        "invites": [{"id": t.id, "email": t.email, "expires_at": iso(t.expires_at)} for t in invites],
        "settings": store.system_view(),
        # sistem anahtarlarının yalnızca DURUMU görünür
        "system": {"app_secret": bool(st.app_secret), "encryption_key": bool(st.encryption_key),
                   "database": "PostgreSQL" if not st.is_sqlite else "SQLite", "storage": "S3 uyumlu" if st.s3_enabled else "Yerel disk",
                   "public_base_url": st.public_base_url or "", "https": st.public_base_url.lower().startswith("https://"),
                   "webhooks": st.public_base_url.lower().startswith("https://"), "smtp": st.smtp_enabled,
                   "secure_cookies": st.secure_cookies},
        "shared": connections.view(None),
    }


class InviteIn(BaseModel):
    email: str = ""


@router.post("/admin/invites")
def admin_invite(body: InviteIn, request: Request, s: Session = Depends(get_db), user: User = Depends(admin_user)):
    email = auth.norm_email(body.email) if body.email.strip() else ""
    token = auth.make_token(s, "invite", email=email, created_by=user.id)
    url = auth.link("/kayit", token, request)
    sent = bool(email) and auth.send_mail(email, "AI Video Stüdyosu daveti", "Kayıt olmak için (7 gün geçerli):\n" + url)
    audit(s, user.id, "invite_created", email or "(herkese açık bağlantı)", client_ip(request))
    return {"url": url, "emailed": sent}


@router.delete("/admin/invites/{tid}")
def admin_revoke_invite(tid: int, s: Session = Depends(get_db), user: User = Depends(admin_user)):
    t = s.get(AuthToken, tid)
    if t is None or t.kind != "invite":
        raise HTTPException(404, "Davet bulunamadı")
    s.delete(t)
    return {"ok": True}


class UserPatch(BaseModel):
    status: str | None = None
    role: str | None = None
    shared_monthly_usd: float | None = None


@router.patch("/admin/users/{uid}")
def admin_update_user(uid: int, body: UserPatch, request: Request, s: Session = Depends(get_db), user: User = Depends(admin_user)):
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    if body.status in ("active", "disabled"):
        if u.id == user.id and body.status == "disabled":
            raise HTTPException(409, "Kendi hesabınızı devre dışı bırakamazsınız")
        u.status = body.status
        if body.status == "disabled":
            for sess in s.scalars(select(AuthSession).where(AuthSession.user_id == u.id)):
                s.delete(sess)
        audit(s, user.id, "user_" + body.status, u.email, client_ip(request))
    if body.role in ("admin", "user") and body.role != u.role:
        if u.id == user.id:
            raise HTTPException(409, "Kendi rolünüzü değiştiremezsiniz")
        u.role = body.role
        audit(s, user.id, "user_role", f"{u.email} -> {body.role}", client_ip(request))
    if body.shared_monthly_usd is not None:
        u.limits = {**(u.limits or {}), "shared_monthly_usd": max(0.0, float(body.shared_monthly_usd))}
        audit(s, user.id, "user_limit", f"{u.email}: {body.shared_monthly_usd}", client_ip(request))
    return auth.user_out(u)


@router.post("/admin/users/{uid}/reset-link")
def admin_reset_link(uid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin_user)):
    """Tek kullanımlık şifre sıfırlama bağlantısı (SMTP yoksa yönetici bunu kullanıcıya elden iletir)."""
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404, "Kullanıcı bulunamadı")
    token = auth.make_token(s, "reset", email=u.email, user_id=u.id, created_by=user.id)
    audit(s, user.id, "reset_link_created", u.email, client_ip(request))
    return {"url": auth.link("/sifre-sifirla", token, request)}


class SystemIn(BaseModel):
    registration_mode: str | None = None
    demo_mode: bool | None = None
    mock_mode: bool | None = None
    shared_monthly_usd: float | None = None
    global_concurrency: dict[str, int] | None = None
    max_attempts: int | None = None
    retry_base_seconds: float | None = None


@router.put("/admin/settings")
def admin_settings(body: SystemIn, request: Request, s: Session = Depends(get_db), user: User = Depends(admin_user)):
    data = body.model_dump(exclude_unset=True)
    if data.get("registration_mode") not in (None, "open", "invite"):
        raise HTTPException(400, "Kayıt modu 'open' ya da 'invite' olmalı")
    for k, v in data.items():
        if v is None:
            continue
        if k == "global_concurrency":
            v = {kk: max(1, min(int(vv), 50)) for kk, vv in v.items()}
        store.sys_put(k, v)
    audit(s, user.id, "system_settings", ", ".join(f"{k}={v}" for k, v in data.items() if k != "global_concurrency")[:300], client_ip(request))
    return store.system_view()


@router.put("/admin/connections/{service}")
async def admin_save_shared(service: str, body: ConnectionIn, request: Request, s: Session = Depends(get_db),
                            user: User = Depends(admin_user)):
    """Ortak (kurum) anahtarı: kendi anahtarını girmeyen kullanıcılar aylık sınırları içinde bunu kullanır."""
    _save_connection(s, None, service, body.values, user, client_ip(request))
    s.commit()
    res = await connections.test(None, service)
    return {"result": res, "shared": connections.view(None)}


@router.delete("/admin/connections/{service}")
def admin_delete_shared(service: str, request: Request, s: Session = Depends(get_db), user: User = Depends(admin_user)):
    if not connections.delete(None, service):
        raise HTTPException(404, "Bağlantı bulunamadı")
    audit(s, user.id, "connection_deleted", f"{service} (ortak)", client_ip(request))
    return {"shared": connections.view(None)}


@router.get("/admin/audit")
def admin_audit(limit: int = 200, s: Session = Depends(get_db), user: User = Depends(admin_user)):
    rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))).all()
    emails = {u.id: u.email for u in s.scalars(select(User))}
    return [{"id": a.id, "user": emails.get(a.user_id, "—"), "action": a.action, "detail": a.detail, "ip": a.ip,
             "created_at": iso(a.created_at)} for a in rows]


# ----------------------------------------------------------------- sağlayıcı webhook'ları
def _kling_signature_ok(secret: str, request: Request, raw: bytes) -> bool:
    """Standard Webhooks: HMAC-SHA256(base64(secret), '{id}.{timestamp}.{gövde}')."""
    wid, ts, sig = (request.headers.get(h, "") for h in ("webhook-id", "webhook-timestamp", "webhook-signature"))
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError):
        return False
    good = base64.b64encode(hmac.new(key, f"{wid}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(good, part.split(",", 1)[-1]) for part in sig.split())


@router.post("/webhooks/{token}")
async def provider_webhook(token: str, request: Request, s: Session = Depends(get_db)):
    """Sağlayıcı geri çağrısı. Adres tahmin edilemez bir belirteç taşır ve içerik güvenilmez sayılır: yalnızca
    bekleyen yoklamayı dürter; sonuç yine sağlayıcıdan, kullanıcının anahtarıyla sorgulanarak alınır."""
    if not auth.limiter.hit(f"hook:{client_ip(request)}", 600, 60):
        raise HTTPException(429, "Çok fazla istek")
    raw = await request.body()
    j = s.scalars(select(Job).where(Job.webhook_token == token)).first() if len(token) >= 16 else None
    if j is not None and j.status in ("running", "submitting"):
        secret = store.get_secret("kling_webhook_secret", j.user_id) if j.provider.startswith("kling") else ""
        # kullanıcı imza anahtarı tanımladıysa imzasız ya da hatalı imzalı çağrı yok sayılır
        if not secret or _kling_signature_ok(secret, request, raw):
            worker.poke(j.id)
    return {"ok": True}  # belirteç geçerli olsun olmasın aynı yanıt
