"""Kimlik doğrulama: kullanıcılar, argon2 şifre özeti, çerez oturumu, CSRF, hız sınırı, tek kullanımlık
bağlantılar ve denetim günlüğü."""
from __future__ import annotations

import logging
import re
import smtplib
import threading
import time
from datetime import timedelta
from email.message import EmailMessage

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import crypto, settings_store as store
from .config import get_settings
from .db import get_db
from .models import (Asset, AuditLog, AuthSession, AuthToken, Conversation, Job, Project, User, now)

log = logging.getLogger("studyo.auth")
COOKIE = "sid"
CSRF_HEADER = "x-csrf-token"
SAFE = ("GET", "HEAD", "OPTIONS")
MAX_FAILED = 5
LOCK_MINUTES = 15
_hasher = PasswordHasher()  # argon2id
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class RateLimiter:
    """Bellek içi kayan pencere sayacı (tek süreçli kurulum için yeterli)."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float) -> bool:
        """True: izin var. False: sınır aşıldı."""
        t = time.monotonic()
        with self._lock:
            arr = [x for x in self._hits.get(key, []) if t - x < window]
            if len(arr) >= limit:
                self._hits[key] = arr
                return False
            arr.append(t)
            self._hits[key] = arr
            if len(self._hits) > 20000:
                self._hits = {k: v for k, v in self._hits.items() if v and t - v[-1] < 3600}
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def audit(s: Session, user_id: int | None, action: str, detail: str = "", ip: str = "") -> None:
    s.add(AuditLog(user_id=user_id, action=action, detail=detail[:500], ip=ip))


# ------------------------------------------------------------------ şifre
def check_password_rules(password: str) -> None:
    if len(password or "") < 10:
        raise HTTPException(400, "Şifre en az 10 karakter olmalı")
    if len(password) > 200:
        raise HTTPException(400, "Şifre çok uzun")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hash_: str, password: str) -> bool:
    try:
        return bool(hash_) and _hasher.verify(hash_, password)
    except (VerificationError, InvalidHashError):
        return False


def norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Geçerli bir e-posta adresi girin")
    return email


# ------------------------------------------------------------------ kullanıcı oluşturma
def create_user(s: Session, email: str, password: str, *, role: str = "user", verified: bool = False, demo: bool = False) -> User:
    u = User(email=email, password_hash=hash_password(password), role=role, email_verified=verified, is_demo=demo,
             prefs={}, limits={})
    s.add(u)
    s.flush()
    return u


def adopt_legacy(s: Session, user: User) -> None:
    """Tek kullanıcılı eski sürümden kalan sahipsiz kayıtlar ilk yöneticiye devredilir."""
    for model in (Project, Asset, Job, Conversation):
        s.execute(update(model).where(model.user_id.is_(None)).values(user_id=user.id))


def register(s: Session, email: str, password: str, invite: str | None, ip: str) -> User:
    email = norm_email(email)
    check_password_rules(password)
    if s.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(409, "Bu e-posta ile kayıt var")
    first = not s.scalars(select(User.id).where(User.role == "admin")).first()
    tok = None
    if not first and store.sys_get("registration_mode") != "open":
        tok = use_token(s, "invite", invite or "", consume=False)
        if tok is None or (tok.email and tok.email != email):
            raise HTTPException(403, "Kayıt yalnızca davetle. Geçerli bir davet bağlantısı gerekli.")
    u = create_user(s, email, password, role="admin" if first else "user", verified=bool(tok and tok.email == email))
    if tok is not None:
        tok.used_at, tok.user_id = now(), u.id
    if first:
        adopt_legacy(s, u)  # ilk kayıt olan kullanıcı yönetici olur
    audit(s, u.id, "register", "ilk yönetici" if first else ("davetle" if tok else "açık kayıt"), ip)
    return u


# ------------------------------------------------------------------ oturum
def start_session(s: Session, user: User, request: Request, response: Response) -> AuthSession:
    st = get_settings()
    token = crypto.new_token()
    sess = AuthSession(token_hash=crypto.token_hash(token), csrf=crypto.new_token(24), user_id=user.id,
                       ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300],
                       expires_at=now() + timedelta(days=st.session_days))
    s.add(sess)
    user.last_login_at, user.failed_logins, user.locked_until = now(), 0, None
    response.set_cookie(COOKIE, token, max_age=st.session_days * 86400, httponly=True, secure=st.secure_cookies,
                        samesite="lax", path="/")
    return sess


def end_session(s: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        sess = s.scalars(select(AuthSession).where(AuthSession.token_hash == crypto.token_hash(token))).first()
        if sess is not None:
            s.delete(sess)
    response.delete_cookie(COOKIE, path="/")


def login(s: Session, email: str, password: str, request: Request, response: Response) -> User:
    ip = client_ip(request)
    email = (email or "").strip().lower()
    # deneme sayısı sınırı: IP başına ve hesap başına
    if not limiter.hit(f"login-ip:{ip}", 20, 300) or not limiter.hit(f"login:{email}:{ip}", 10, 300):
        raise HTTPException(429, "Çok fazla giriş denemesi. Birkaç dakika sonra yeniden deneyin.")
    u = s.scalars(select(User).where(User.email == email)).first()
    if u is not None and u.locked_until and u.locked_until > now():
        mins = int((u.locked_until - now()).total_seconds() // 60) + 1
        raise HTTPException(429, f"Çok fazla hatalı deneme: hesap geçici olarak kilitlendi. {mins} dakika sonra yeniden deneyin.")
    if u is None or not verify_password(u.password_hash, password or ""):
        if u is None:
            hash_password("zamanlama-esitleme")  # var olmayan hesapta da benzer süre
        else:
            u.failed_logins = (u.failed_logins or 0) + 1
            if u.failed_logins >= MAX_FAILED:
                u.locked_until, u.failed_logins = now() + timedelta(minutes=LOCK_MINUTES), 0
                audit(s, u.id, "login_locked", "", ip)
            audit(s, u.id, "login_failed", "", ip)
            s.commit()
        raise HTTPException(401, "E-posta ya da şifre hatalı")
    if u.status != "active":
        raise HTTPException(403, "Bu hesap devre dışı bırakılmış")
    if _hasher.check_needs_rehash(u.password_hash):
        u.password_hash = hash_password(password)
    start_session(s, u, request, response)
    audit(s, u.id, "login", "", ip)
    return u


def _session_user(s: Session, request: Request) -> tuple[User, AuthSession] | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    sess = s.scalars(select(AuthSession).where(AuthSession.token_hash == crypto.token_hash(token))).first()
    if sess is None or sess.expires_at < now():
        return None
    u = s.get(User, sess.user_id)
    if u is None or u.status != "active":
        return None
    return u, sess


def current_user(request: Request, s: Session = Depends(get_db)) -> User:
    """Her korumalı uçta kullanılır: oturumu doğrular, veri değiştiren isteklerde CSRF belirtecini denetler."""
    found = _session_user(s, request)
    if found is None:
        raise HTTPException(401, "Oturum açmanız gerekiyor")
    u, sess = found
    if request.method not in SAFE:
        sent = request.headers.get(CSRF_HEADER) or ""
        if not sent or not crypto.hmac.compare_digest(sent, sess.csrf):
            raise HTTPException(403, "Güvenlik belirteci (CSRF) eksik ya da geçersiz; sayfayı yenileyin")
    request.state.user_id = u.id
    request.state.csrf = sess.csrf
    return u


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(404, "Kayıt bulunamadı")  # yönetici uçlarının varlığı da gizlenir
    return user


# ------------------------------------------------------------------ tek kullanımlık bağlantılar
TTL = {"invite": timedelta(days=7), "reset": timedelta(hours=2), "verify": timedelta(days=3)}


def make_token(s: Session, kind: str, *, email: str = "", user_id: int | None = None, created_by: int | None = None) -> str:
    token = crypto.new_token()
    s.add(AuthToken(kind=kind, token_hash=crypto.token_hash(token), email=email, user_id=user_id, created_by=created_by,
                    expires_at=now() + TTL[kind]))
    return token


def use_token(s: Session, kind: str, token: str, consume: bool = True) -> AuthToken | None:
    if not token:
        return None
    t = s.scalars(select(AuthToken).where(AuthToken.token_hash == crypto.token_hash(token), AuthToken.kind == kind)).first()
    if t is None or t.used_at is not None or t.expires_at < now():
        return None
    if consume:
        t.used_at = now()
    return t


def link(path: str, token: str, request: Request | None = None) -> str:
    """Bağlantı adresi: PUBLIC_BASE_URL varsa o; yoksa isteğin geldiği adres (Host başlığı izinli adlarla sınırlıdır)."""
    base = get_settings().public_base_url.rstrip("/")
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    base = base or f"http://127.0.0.1:{get_settings().port}"
    return f"{base}{path}?token={token}"


# ------------------------------------------------------------------ e-posta (SMTP ayarlıysa)
def send_mail(to: str, subject: str, body: str) -> bool:
    st = get_settings()
    if not st.smtp_enabled:
        return False

    def work() -> None:
        try:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = st.smtp_from, to, subject
            msg.set_content(body)
            with smtplib.SMTP(st.smtp_host, st.smtp_port, timeout=20) as smtp:
                if st.smtp_tls:
                    smtp.starttls()
                if st.smtp_user:
                    smtp.login(st.smtp_user, st.smtp_password)
                smtp.send_message(msg)
        except Exception as e:  # e-posta hatası akışı durdurmaz; şifre günlüğe yazılmaz
            log.warning("e-posta gönderilemedi (%s): %s", to, type(e).__name__)

    threading.Thread(target=work, daemon=True).start()
    return True


def user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "role": u.role, "status": u.status, "email_verified": u.email_verified,
            "setup_done": u.setup_done, "is_demo": u.is_demo, "limits": u.limits or {},
            "created_at": u.created_at.isoformat() + "Z", "last_login_at": u.last_login_at.isoformat() + "Z" if u.last_login_at else None}


def count_users(s: Session) -> int:
    return int(s.execute(select(func.count(User.id)).where(User.is_demo.is_(False))).scalar() or 0)
