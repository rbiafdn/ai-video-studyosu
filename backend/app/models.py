"""Veri modeli: Proje -> Bölüm -> Sahne, Karakter, Sahne metni (dil başına), Varlık, İş, Sohbet."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    # sahibi (tam veri yalıtımı: her sorgu bu alan üzerinden süzülür)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # tüm görsel promptlara eklenen ortak üslup metni
    style: Mapped[str] = mapped_column(Text, default="")
    # varsayılan dil (BCP-47); bölüm ve sahne düzeyinde ezilebilir
    default_language: Mapped[str] = mapped_column(String(20), default="tr")
    # dil başına telaffuz sözlüğü: {dil: {"rules": [[yazılış, okunuş], ...], "dictionary_id": "", "version_id": ""}}
    pronunciations: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    characters: Mapped[list["Character"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Character.id"
    )
    episodes: Mapped[list["Episode"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Episode.number"
    )


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    is_narrator: Mapped[bool] = mapped_column(Boolean, default=False)
    # varsayılan ses (dil başına tanım yoksa çok dilli modelle bu kullanılır)
    voice_id: Mapped[str] = mapped_column(String(200), default="")
    voice_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    # dil başına ses: {dil: {"voice_id": "...", "voice_settings": {...}}}
    voices: Mapped[dict] = mapped_column(JSON, default=dict)
    heygen: Mapped[dict] = mapped_column(JSON, default=dict)
    # onaylı ana görsel (assets.id)
    main_image_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    project: Mapped[Project] = relationship(back_populates="characters")


class Episode(Base):
    __tablename__ = "episodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200), default="")
    # kaynak dil (boşsa projenin varsayılanı) ve hedef diller (kaynak dahil)
    language: Mapped[str] = mapped_column(String(20), default="")
    languages: Mapped[list] = mapped_column(JSON, default=list)
    # kurgu ayarları: transition, transition_dur, subtitles, vertical, music_volume, ducking
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    # otomatik mod: "" | running | done | error ; auto_langs = çalıştığı diller
    auto_state: Mapped[str] = mapped_column(String(20), default="")
    auto_message: Mapped[str] = mapped_column(Text, default="")
    auto_langs: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project: Mapped[Project] = relationship(back_populates="episodes")
    scenes: Mapped[list["Scene"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan", order_by="Scene.order_no"
    )


class Scene(Base):
    __tablename__ = "scenes"
    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    order_no: Mapped[int] = mapped_column(Integer, default=1)
    # talking (konuşan) | silent (konuşmasız) | narrator (anlatıcı)
    type: Mapped[str] = mapped_column(String(20), default="silent")
    character_ids: Mapped[list] = mapped_column(JSON, default=list)
    # kaynak dil (boşsa bölümün dili)
    language: Mapped[str] = mapped_column(String(20), default="")
    # görsel ve video promptları dilden bağımsızdır
    image_prompt: Mapped[str] = mapped_column(Text, default="")
    video_prompt: Mapped[str] = mapped_column(Text, default="")
    # "görseldeki metin" (tabela, ekran yazısı): {dil: metin}; doluysa sahnenin görseli dile bağlıdır
    image_text: Mapped[dict] = mapped_column(JSON, default=dict)
    duration: Mapped[float] = mapped_column(Float, default=5.0)
    audio_moment: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    # "baştan üret" zinciri sürerken dil kodu (ya da "*"): çıktılar kendiliğinden onaylanır
    chain: Mapped[str] = mapped_column(String(20), default="")
    # ajan 3 denemede düzeltemediyse: elle bakılmalı
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_note: Mapped[str] = mapped_column(Text, default="")

    episode: Mapped[Episode] = relationship(back_populates="scenes")
    texts: Mapped[list["SceneText"]] = relationship(cascade="all, delete-orphan", order_by="SceneText.id")
    selections: Mapped[list["Selection"]] = relationship(cascade="all, delete-orphan")


class SceneText(Base):
    """Sahne + dil -> replik/anlatıcı metni."""
    __tablename__ = "scene_texts"
    __table_args__ = (UniqueConstraint("scene_id", "language"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")
    # draft (taslak) | approved (onaylı): çeviri onaylanmadan ses üretilmez
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # human | agent | suggestion
    origin: Mapped[str] = mapped_column(String(20), default="human")
    detected_language: Mapped[str] = mapped_column(String(20), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Selection(Base):
    """Sahne + dil + rol için seçili sürüm. Dilden bağımsız varlıklarda language = ""."""
    __tablename__ = "selections"
    __table_args__ = (UniqueConstraint("scene_id", "language", "role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(20), default="")
    role: Mapped[str] = mapped_column(String(40))
    asset_id: Mapped[int] = mapped_column(Integer)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    episode_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scene_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    character_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # audio | image | video | text
    kind: Mapped[str] = mapped_column(String(20))
    # voice | character_main | character_ref | scene_image | scene_video | music | sfx |
    # final | final_vertical | subtitles | qc_report | attachment
    role: Mapped[str] = mapped_column(String(40))
    # dil (dilden bağımsızsa "")
    language: Mapped[str] = mapped_column(String(20), default="", index=True)
    path: Mapped[str] = mapped_column(Text)  # storage_dir'e göre göreli
    version: Mapped[int] = mapped_column(Integer, default=1)
    step: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(60), default="")
    mode: Mapped[str] = mapped_column(String(20), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    # draft (taslak) | approved (onaylı) | rejected (reddedildi)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # kimin onayladığı: user | agent | auto
    approved_by: Mapped[str] = mapped_column(String(20), default="")
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    # iş, sahibi olan kullanıcının anahtarlarıyla ve onun sınırlarıyla yürür
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    # own (kullanıcının anahtarı) | shared (kurum anahtarı) | "" (mock/yerel)
    cred_source: Mapped[str] = mapped_column(String(20), default="")
    # sağlayıcı webhook'u için tek kullanımlık adres parçası
    webhook_token: Mapped[str] = mapped_column(String(64), default="", index=True)
    # voice | character_image | scene_image | talking_video | silent_video |
    # omni_fix | lipsync_fix | montage | qc | claude
    kind: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(60))
    mode: Mapped[str] = mapped_column(String(20), default="")  # real | mock | manual | local
    step: Mapped[int] = mapped_column(Integer, default=0)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    episode_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scene_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    character_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str] = mapped_column(String(20), default="")
    # işi başlatan ajan çalışması (elle başlatıldıysa boş)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued | submitting | running | waiting_manual | blocked (bağlantı yenilenmeli) | succeeded | failed | canceled
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    external_id: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_units: Mapped[float] = mapped_column(Float, default=0.0)
    cost_unit: Mapped[str] = mapped_column(String(30), default="")
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fix: Mapped[bool] = mapped_column(Boolean, default=False)
    result_asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QCReport(Base):
    __tablename__ = "qc_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(20), default="")
    final_asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    secret: Mapped[bool] = mapped_column(Boolean, default=False)


# ------------------------------------------------------------------ sohbet / yönetmen ajanı
class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="Yeni sohbet")
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # real | mock (sohbet boyunca sabit)
    mode: Mapped[str] = mapped_column(String(20), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    # Geçmiş yalnızca eklemeli olmalı: sistem istemi ve araç listesi sohbet başında dondurulur
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    tools: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # api: Claude'a giden geçmişin parçası · pending_user: çalışan akışa eklenmeyi bekleyen kullanıcı mesajı ·
    # merged_user: araç sonuçlarıyla aynı tura eklenmiş kullanıcı mesajı (yalnızca gösterim) · note: sistem notu/sonuç kartı
    kind: Mapped[str] = mapped_column(String(20), default="api")
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    # queued | running | waiting_jobs | waiting_user | waiting_connection | paused_budget | done | failed | stopped
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    claude_usd: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    plan_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    # soru, bekleyen araç sonuçları, sahne başı deneme sayaçları, kontrol turu sayacı
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ------------------------------------------------------------------ kullanıcılar ve erişim
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, default="")
    # admin | user
    role: Mapped[str] = mapped_column(String(20), default="user")
    # active | disabled
    status: Mapped[str] = mapped_column(String(20), default="active")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    setup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    # tercihler: default_language, budget_usd, plan_approval, output_format, models, claude_model, concurrency
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    # yöneticinin koyduğu sınırlar: {"shared_monthly_usd": 50}
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Connection(Base):
    """Kullanıcı + servis -> şifreli kimlik bilgileri. user_id boşsa yöneticinin tanımladığı ortak (kurum) anahtarı."""
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("user_id", "service"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    # anthropic | elevenlabs | kling | heygen
    service: Mapped[str] = mapped_column(String(40))
    secret: Mapped[str] = mapped_column(Text, default="")        # şifreli JSON
    last4: Mapped[str] = mapped_column(String(8), default="")
    # connected | error | untested
    status: Mapped[str] = mapped_column(String(20), default="untested")
    message: Mapped[str] = mapped_column(Text, default="")
    info: Mapped[dict] = mapped_column(JSON, default=dict)       # kalan kredi, özellikler, ses sayısı...
    tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class AuthToken(Base):
    """Tek kullanımlık bağlantılar: davet, şifre sıfırlama, e-posta doğrulama."""
    __tablename__ = "auth_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    # invite | reset | verify
    kind: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """Denetim günlüğü: giriş, anahtar ekleme/silme, yönetici işlemleri. Gizli değer yazılmaz."""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    level: Mapped[str] = mapped_column(String(20), default="info")   # info | warn | error | ok
    text: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(String(300), default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
