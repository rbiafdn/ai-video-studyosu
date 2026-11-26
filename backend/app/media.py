"""ffmpeg / ffprobe yardımcıları (subprocess)."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class MediaError(Exception):
    pass


def ffmpeg(args: list[str], cwd: Path | None = None, timeout: float = 1800) -> str:
    """ffmpeg çalıştırır, stderr'i döndürür (tespit filtreleri oraya yazar)."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", *[str(a) for a in args]]
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"ffmpeg zaman aşımı ({timeout:.0f} sn)") from e
    except FileNotFoundError as e:
        raise MediaError("ffmpeg bulunamadı; kurup PATH'e ekleyin") from e
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-6:])
        raise MediaError(f"ffmpeg hatası: {tail}")
    return p.stderr


async def affmpeg(args: list[str], cwd: Path | None = None, timeout: float = 1800) -> str:
    return await asyncio.to_thread(ffmpeg, args, cwd, timeout)


def probe(path: Path) -> dict:
    p = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if p.returncode != 0:
        raise MediaError(f"ffprobe okuyamadı: {p.stderr.strip()[-300:]}")
    return json.loads(p.stdout or "{}")


def _fps(s: dict) -> float:
    for k in ("avg_frame_rate", "r_frame_rate"):
        try:
            n, d = s.get(k, "0/0").split("/")
            if float(d):
                return round(float(n) / float(d), 3)
        except (ValueError, ZeroDivisionError):
            pass
    return 0.0


def info(path: Path) -> dict:
    """Süre, çözünürlük, fps ve ses bilgisi."""
    d = probe(path)
    out = {"duration": 0.0, "has_video": False, "has_audio": False, "width": 0, "height": 0, "fps": 0.0,
           "video_duration": 0.0, "audio_duration": 0.0, "sample_rate": 0, "channels": 0,
           "size": Path(path).stat().st_size}
    fmt_dur = float(d.get("format", {}).get("duration") or 0)
    out["duration"] = fmt_dur
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and not out["has_video"]:
            if s.get("disposition", {}).get("attached_pic"):
                continue
            out.update(has_video=True, width=int(s.get("width") or 0), height=int(s.get("height") or 0),
                       fps=_fps(s), video_duration=float(s.get("duration") or fmt_dur))
        elif s.get("codec_type") == "audio" and not out["has_audio"]:
            out.update(has_audio=True, sample_rate=int(s.get("sample_rate") or 0),
                       channels=int(s.get("channels") or 0),
                       audio_duration=float(s.get("duration") or fmt_dur))
    return out


def validate(path: Path, kind: str) -> dict:
    """İndirilen/üretilen dosyayı doğrular: boyut > 0 ve ffprobe beklenen akışı okuyabiliyor."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise MediaError("Dosya boş ya da yok")
    i = info(path)
    if kind == "audio" and not i["has_audio"]:
        raise MediaError("Dosyada ses akışı yok")
    if kind in ("video", "image") and not i["has_video"]:
        raise MediaError("Dosyada görüntü akışı yok")
    if kind in ("audio", "video") and i["duration"] <= 0:
        raise MediaError("Dosya süresi okunamadı")
    return i


def timecode(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def find_font() -> Path | None:
    for c in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"):
        if Path(c).exists():
            return Path(c)
    return None


# ------------------------------------------------------------------ ses / dudak senkronu ölçümü
def ffmpeg_bytes(args: list[str], timeout: float = 600) -> bytes:
    """ffmpeg'i çalıştırır ve stdout'u (ham ses/görüntü) döndürür."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *[str(a) for a in args]]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise MediaError(f"ffmpeg hatası: {p.stderr.decode('utf-8', 'replace')[-300:]}")
    return p.stdout


def audio_envelope(path: Path, fps: int = 25) -> list[float]:
    """Kare başına ses enerjisi (RMS)."""
    from array import array
    raw = ffmpeg_bytes(["-i", path, "-vn", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"])  # akış gecikmesi sessizlikle doldurulur
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    win = 8000 // fps
    out = []
    for i in range(0, len(samples) - win + 1, win):
        chunk = samples[i:i + win]
        out.append((sum(v * v for v in chunk) / win) ** 0.5)
    return out


def _region(path: Path, fps: int, crop: str) -> tuple[list[float], list[float]]:
    """(kareler arası hareket, ortalama parlaklık) dizileri."""
    w, h = 32, 24
    raw = ffmpeg_bytes(["-i", path, "-an", "-vf", f"fps={fps}:start_time=0,{crop}scale={w}:{h},format=gray", "-f", "rawvideo", "-"])
    size = w * h
    frames = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
    motion = [0.0]
    for a, b in zip(frames, frames[1:]):
        motion.append(sum(abs(x - y) for x, y in zip(a, b)) / size)
    return motion, [sum(f) / size for f in frames]


def mouth_features(path: Path, fps: int = 25) -> tuple[list[float], list[float]]:
    """Karenin alt-orta bölgesi (ağız bölgesi varsayımı) için iki ölçü: hareket ve parlaklık.
    Kamera/arka plan etkisi, tüm karenin aynı ölçüsü çıkarılarak azaltılır."""
    m_mot, m_bri = _region(path, fps, "crop=iw*0.4:ih*0.3:iw*0.3:ih*0.5,")
    g_mot, g_bri = _region(path, fps, "")
    return ([max(m - g, 0.0) for m, g in zip(m_mot, g_mot)], [m - g for m, g in zip(m_bri, g_bri)])


def mouth_motion(path: Path, fps: int = 25) -> list[float]:
    return mouth_features(path, fps)[0]


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 1e-9 or vb <= 1e-9:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5


def lipsync_offset(video: Path, fps: int = 25, max_lag: int = 15) -> dict:
    """Konuşan videoda ses ile ağız hareketi arasındaki kaymayı ölçer (SyncNet benzeri, hafif yöntem).

    Ses zarfı ile ağız bölgesindeki hareket enerjisi farklı gecikmelerde ilişkilendirilir; en yüksek
    ilişkinin gecikmesi kaymadır. offset_ms > 0: ses görüntüden GERİDE; < 0: ses önde.
    confidence: en yüksek ilişki katsayısı (0-1). Yüz tanıma yapılmaz; ağzın karenin alt-ortasında
    olduğu varsayılır (konuşan sahne görselleri bu kadraja göre üretilir).
    """
    env = audio_envelope(video, fps)
    motion, bright = mouth_features(video, fps)
    n = min(len(env), len(motion))
    if n < fps:  # 1 saniyeden kısa
        return {"measurable": False, "offset_ms": 0, "confidence": 0.0}
    env = env[:n]
    best_lag, best = 0, -2.0
    # iki ölçüden daha güçlü ilişki vereni kullanılır; parlaklıkta işaret bilinmediği için mutlak değer
    for series, absolute in ((motion[:n], False), (bright[:n], True)):
        for lag in range(-max_lag, max_lag + 1):
            c = _corr(env[lag:], series[: n - lag]) if lag >= 0 else _corr(env[: n + lag], series[-lag:])
            c = abs(c) if absolute else c
            if c > best:
                best_lag, best = lag, c
    return {"measurable": True, "offset_ms": int(round(best_lag * 1000 / fps)), "confidence": round(max(best, 0.0), 3)}


def speech_onset(path: Path, fps: int = 50) -> float:
    """Dosyada sesin başladığı an (sn); ses yoksa -1."""
    env = audio_envelope(path, fps)
    if not env:
        return -1.0
    peak = max(env)
    if peak < 30:
        return -1.0
    for i, v in enumerate(env):
        if v > peak * 0.1:
            return round(i / fps, 3)
    return -1.0


def frames_jpeg(path: Path, count: int = 3, max_side: int = 768) -> list[bytes]:
    """Videodan eşit aralıklı kareler (ajanın görsel incelemesi için küçük JPEG)."""
    dur = info(path)["duration"] or 1.0
    out = []
    for i in range(count):
        t = dur * (i + 0.5) / count
        out.append(ffmpeg_bytes(["-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-vf",
                                 f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
                                 "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]))
    return out


def image_jpeg(path: Path, max_side: int = 1024) -> bytes:
    return ffmpeg_bytes(["-i", path, "-frames:v", "1", "-vf",
                         f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
                         "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"])
