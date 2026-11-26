"""Kullanıcı bildirimleri (uygulama içi; SMTP ayarlıysa önemli olanlar e-postayla da gider)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .events import bus
from .models import Notification, User


def notify(s: Session, user_id: int | None, text: str, level: str = "info", link: str = "", email_subject: str = "") -> None:
    if user_id is None:
        return
    s.add(Notification(user_id=user_id, level=level, text=text[:600], link=link))
    s.info.setdefault("events", []).append(("notification", {"user_id": user_id, "level": level, "text": text[:300]}))
    if email_subject:
        from . import auth
        u = s.get(User, user_id)
        if u is not None and not u.is_demo:
            auth.send_mail(u.email, email_subject, text + ("\n\n" + auth.link(link, "").removesuffix("?token=") if link else ""))


def publish_now(user_id: int, level: str = "info") -> None:
    bus.publish("notification", user_id=user_id, level=level)
