"""Yerel dosya düzeni: storage/<proje>/<bölüm>/<sahne>/..."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Asset, Character, Episode, Project, Scene

_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def slug(text: str, fallback: str = "x") -> str:
    t = unicodedata.normalize("NFKD", (text or "").translate(_TR)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t[:40] or fallback


def root() -> Path:
    return get_settings().storage_dir


def abs_path(rel: str) -> Path:
    """Göreli yolu yerel dosyaya çevirir. Dizin dışına çıkan yol reddedilir. S3 etkinse ve dosya
    yerelde yoksa (başka sunucuda üretilmişse) indirilir."""
    p = (root() / rel).resolve()
    if root().resolve() not in p.parents and p != root().resolve():
        raise ValueError("storage dışı yol")
    if not p.exists():
        be = s3()
        if be is not None:
            be.download(rel, p)
    return p


# ------------------------------------------------------------------ S3 uyumlu depolama (isteğe bağlı)
class S3Backend:
    """AWS S3 / Cloudflare R2 / MinIO. Yerel disk çalışma kopyasıdır (ffmpeg yerel dosya ister); kalıcı kopya
    kovada durur. Sağlayıcılar girdi dosyasını herkese açık adresten istediğinde kısa ömürlü imzalı adres üretilir."""

    def __init__(self, client=None, bucket: str | None = None):
        st = get_settings()
        self.bucket = bucket or st.s3_bucket
        if client is None:
            import boto3
            client = boto3.client("s3", endpoint_url=st.s3_endpoint or None, aws_access_key_id=st.s3_access_key,
                                  aws_secret_access_key=st.s3_secret_key, region_name=st.s3_region or None)
        self.client = client

    def upload(self, rel: str, path: Path) -> None:
        self.client.upload_file(str(path), self.bucket, rel)

    def download(self, rel: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, rel, str(dest))
            return True
        except Exception:
            dest.unlink(missing_ok=True)
            return False

    def url(self, rel: str, ttl: int = 3600) -> str:
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": rel}, ExpiresIn=ttl)

    def delete_prefix(self, prefix: str) -> int:
        n, token = 0, None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kw)
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
                n += len(keys)
            token = page.get("NextContinuationToken")
            if not token:
                return n


_s3: S3Backend | None = None
s3_override: S3Backend | None = None  # testler için


def s3() -> S3Backend | None:
    global _s3
    if s3_override is not None:
        return s3_override
    if not get_settings().s3_enabled:
        return None
    if _s3 is None:
        _s3 = S3Backend()
    return _s3


def publish(path: Path | str) -> None:
    """Yeni yazılan dosyayı kalıcı depoya gönderir (yalnız yerel diskte çalışılıyorsa hiçbir şey yapmaz)."""
    be = s3()
    if be is None:
        return
    p = Path(path) if isinstance(path, Path) or Path(str(path)).is_absolute() else root() / str(path)
    if p.exists():
        be.upload(rel_path(p), p)


def signed_url(rel: str, ttl: int = 3600) -> str:
    """Kısa ömürlü imzalı adres (S3 etkinse); değilse boş."""
    be = s3()
    return be.url(rel, ttl) if be is not None else ""


def delete_tree(rel_prefix: str) -> None:
    """Bir klasörü yerelden ve (varsa) kovadan siler."""
    import shutil
    target = (root() / rel_prefix).resolve()
    if root().resolve() in target.parents:
        shutil.rmtree(target, ignore_errors=True)
    be = s3()
    if be is not None:
        be.delete_prefix(rel_prefix.rstrip("/") + "/")


def user_dir(user_id: int) -> Path:
    return root() / f"u{user_id}"


def rel_path(p: Path) -> str:
    return Path(p).resolve().relative_to(root().resolve()).as_posix()


def tmp_dir() -> Path:
    d = root() / "_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir(p: Project) -> Path:
    """storage/<kullanıcı>/<proje>/...  (eski tek kullanıcılı kayıtlar kökte kalır)"""
    base = user_dir(p.user_id) if p.user_id else root()
    return base / f"p{p.id}-{slug(p.name)}"


def episode_dir(s: Session, ep: Episode) -> Path:
    return project_dir(s.get(Project, ep.project_id)) / f"bolum-{ep.number:02d}"


def lang_dir(language: str) -> str:
    """storage/<proje>/<bölüm>/<dil>/...  Dilden bağımsız varlıklar 'ortak' klasöründe durur."""
    return language or "ortak"


def scene_dir(s: Session, sc: Scene, language: str = "") -> Path:
    return episode_dir(s, s.get(Episode, sc.episode_id)) / lang_dir(language) / f"sahne-{sc.order_no:03d}-{sc.id}"


def character_dir(s: Session, c: Character) -> Path:
    return project_dir(s.get(Project, c.project_id)) / "karakterler" / f"{c.id}-{slug(c.name)}"


def next_version(s: Session, *, role: str, scene_id: int | None = None, character_id: int | None = None,
                 episode_id: int | None = None, language: str = "") -> int:
    q = select(func.max(Asset.version)).where(Asset.role == role, Asset.language == language)
    if scene_id is not None:
        q = q.where(Asset.scene_id == scene_id)
    elif character_id is not None:
        q = q.where(Asset.character_id == character_id)
    else:
        q = q.where(Asset.episode_id == episode_id, Asset.scene_id.is_(None))
    return (s.execute(q).scalar() or 0) + 1


def target_path(s: Session, *, role: str, version: int, ext: str, scene: Scene | None = None,
                character: Character | None = None, episode: Episode | None = None, language: str = "") -> Path:
    if scene is not None:
        d = scene_dir(s, scene, language)
    elif character is not None:
        d = character_dir(s, character)
    else:
        d = episode_dir(s, episode) / lang_dir(language) / "kurgu"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{role}_v{version}{ext}"
