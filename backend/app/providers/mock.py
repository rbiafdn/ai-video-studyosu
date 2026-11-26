"""MOCK adaptör: API anahtarı yokken ffmpeg ile sahte ses/görsel/video üretir.

Gerçek adaptörlerle aynı submit/poll/fetch akışından geçer; böylece uygulama anahtarsız da
uçtan uca denenebilir. Mock videolar bilerek 1280x720@24 üretilir ki kurgunun
çözünürlük/fps eşitlemesi de sınansın.
"""
from __future__ import annotations

import hashlib
import shutil
import textwrap
import time
from pathlib import Path

from .. import media
from .base import PermanentError, Polled, Provider, Submitted


def _h(text: str) -> int:
    return int(hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8], 16)


def _color(seed: int, light: float = 0.35) -> str:
    import colorsys
    r, g, b = colorsys.hls_to_rgb((seed % 360) / 360.0, light, 0.55)
    return f"0x{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


class MockProvider(Provider):
    label = "Mock"
    poll_interval = 0.15

    def __init__(self, ctx, kind: str = "video"):
        super().__init__(ctx)
        self.kind = kind

    async def submit(self, inp: dict) -> Submitted:
        if inp.get("mock_fail"):
            raise PermanentError("Mock: istenen hata")
        return Submitted(f"mock:{time.time():.3f}")

    async def poll(self, external_id: str) -> Polled:
        try:
            t0 = float(external_id.split(":", 1)[1])
        except (IndexError, ValueError):
            return Polled("lost")
        delay = max(self.ctx.mock_delay, 0.0)
        el = time.time() - t0
        if el >= delay:
            return Polled("succeeded", 1.0)
        return Polled("running", el / delay if delay else 1.0)

    # ------------------------------------------------------------------ üretim
    def _work(self) -> Path:
        d = self.ctx.tmp_dir / "mock"
        d.mkdir(parents=True, exist_ok=True)
        font = media.find_font()
        if font and not (d / "font.ttf").exists():
            shutil.copyfile(font, d / "font.ttf")
        return d

    def _drawtext(self, work: Path, text: str, size: int = 40, y: str = "(h-text_h)/2") -> str:
        """drawtext süzgeci; yazı tipi yoksa boş döner (yollar göreli, kaçış derdi yok)."""
        if not (work / "font.ttf").exists():
            return ""
        name = f"label_{self.ctx.job_id}_{_h(text) % 10000}.txt"
        (work / name).write_text("\n".join(textwrap.wrap(text, 46)[:6]) or " ", encoding="utf-8")
        return (f"drawtext=fontfile=font.ttf:textfile={name}:fontcolor=white:fontsize={size}:line_spacing=10:"
                f"x=(w-text_w)/2:y={y}:box=1:boxcolor=black@0.45:boxborderw=18")

    async def fetch(self, external_id: str, dest: Path) -> Path:
        inp, kind, work = self.inp, self.ctx.kind, self._work()
        if kind == "voice":
            return await self._voice(inp, dest.with_suffix(".mp3"))
        if kind in ("character_image", "scene_image"):
            return await self._image(inp, dest.with_suffix(".png"), work, square=kind == "character_image")
        if kind == "omni_fix":
            return await self._omni(inp, dest.with_suffix(".mp4"), work)
        if kind == "lipsync_fix":
            return await self._lipsync(inp, dest.with_suffix(".mp4"))
        return await self._video(inp, dest.with_suffix(".mp4"), work)

    async def _voice(self, inp: dict, out: Path) -> Path:
        words = len((inp.get("text") or "").split())
        dur = min(max(words * 0.38 + 0.6, 1.5), 60.0)
        freq = 140 + _h((inp.get("voice_id") or "anlatici") + (inp.get("language") or "")) % 140
        # periyodik olmayan, heceleri andıran ses zarfı (dudak senkronu ölçümü bununla sınanır)
        env = "0.06+0.85*abs(sin(9.7*t)*sin(4.1*t+1))*(0.6+0.4*sin(1.3*t+2))"
        await media.affmpeg([
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={dur:.2f}",
            "-af", f"volume='{env}':eval=frame,afade=t=in:d=0.05,afade=t=out:st={dur - 0.12:.2f}:d=0.1",
            "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", out])
        return out

    async def _image(self, inp: dict, out: Path, work: Path, square: bool) -> Path:
        w, h = (1024, 1024) if square else (1920, 1080)
        seed = _h(inp.get("prompt") or "x")
        label = self._drawtext(work, "MOCK GÖRSEL\n" + (inp.get("label") or "") + "\n" + (inp.get("prompt") or "")[:160], 38)
        src = (f"gradients=s={w}x{h}:c0={_color(seed)}:c1={_color(seed + 90, 0.2)}:"
               f"x0=0:y0=0:x1={w}:y1={h}:nb_colors=2:speed=0.00001")
        vf = ",".join(x for x in (label, "format=rgb24") if x)
        await media.affmpeg(["-f", "lavfi", "-i", src, "-vf", vf, "-frames:v", "1", out.resolve()], cwd=work)
        return out

    async def _video(self, inp: dict, out: Path, work: Path) -> Path:
        audio = self.ctx.path(inp["audio_path"]) if inp.get("audio_path") else None
        dur = float(inp.get("audio_duration") or 0) + 0.2 if audio else float(inp.get("duration") or 5)
        dur = max(dur, 1.0)
        label = self._drawtext(work, "MOCK VİDEO · " + (inp.get("label") or ""), 30, "h-text_h-60")
        args: list = []
        if inp.get("image_path") and self.ctx.path(inp["image_path"]).exists():
            args += ["-loop", "1", "-framerate", "24", "-t", f"{dur:.2f}", "-i", self.ctx.path(inp["image_path"]).resolve()]
            vf = ("scale=1408:792:force_original_aspect_ratio=increase,crop=1408:792,"
                  "crop=1280:720:x='64+60*sin(2*PI*t/4)':y='36+30*cos(2*PI*t/4)'")
            if audio:  # konuşan sahne: kamera sabit, yalnızca "ağız" hareket eder
                vf = "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"
        else:
            src = "color=c=0x2b3a4f:s=1280x720:r=24" if audio else "testsrc2=s=1280x720:r=24"
            args += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", src]
            vf = "null"
        vf = ",".join(x for x in (vf, label, "format=yuv420p") if x)
        if audio:
            args += ["-i", audio.resolve()]
        elif inp.get("native_audio"):
            args += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anoisesrc=color=pink:amplitude=0.08:sample_rate=44100"]
        has_audio = audio is not None or bool(inp.get("native_audio"))
        if audio:
            # konuşan sahne: sesle birlikte hareket eden bir "ağız" (dalga biçimi) çizilir
            fc = (f"[0:v]{vf}[bg];[1:a]asplit[aout][aw];"
                  "[aw]volume=7,showwaves=s=360x150:mode=cline:rate=24:colors=white[w];"
                  "[bg][w]overlay=(W-w)/2:H*0.56:shortest=0[v]")
            args += ["-filter_complex", fc, "-map", "[v]", "-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-vf", vf, "-map", "0:v"]
            if has_audio:
                args += ["-map", "1:a", "-c:a", "aac", "-b:a", "128k"]
        args += ["-t", f"{dur:.2f}", "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out.resolve()]
        await media.affmpeg(args, cwd=work)
        return out

    async def _omni(self, inp: dict, out: Path, work: Path) -> Path:
        src = self.ctx.path(inp["video_path"]).resolve()
        label = self._drawtext(work, "MOCK OMNİ DÜZELTME · " + (inp.get("instruction") or "")[:80], 28, "40")
        vf = ",".join(x for x in ("hue=h=60:s=1.2", label, "format=yuv420p") if x)
        await media.affmpeg(["-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", out.resolve()], cwd=work)
        return out

    async def _lipsync(self, inp: dict, out: Path) -> Path:
        await media.affmpeg(["-i", self.ctx.path(inp["video_path"]), "-i", self.ctx.path(inp["audio_path"]),
                             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", out])
        return out
