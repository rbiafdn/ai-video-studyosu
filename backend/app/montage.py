"""5. adım: kurgu ve ses miksajı (ffmpeg). Her hedef dil için ayrı çıktı üretilir.

- Her sahne hedef biçime eşitlenir (çözünürlük, fps, 48 kHz stereo) ve önbelleğe alınır:
  yalnızca değişen sahnenin klibi yeniden üretilir; dilden bağımsız klipler diller arasında ortaktır.
- Geçişler (kesme / kararma / çapraz geçiş), müzik ve efekt katmanı, konuşma altında müziği
  otomatik kısma (sidechain ducking), -14 LUFS iki geçişli normalizasyon.
- Dil başına SRT altyazı; gömmede dile uygun yazı tipi (sağdan sola ve Latin dışı yazılar dahil).
- Sahne süresi o dilin ses süresine göre belirlenir; kaynak dile göre fark kaydedilir.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from . import languages, media, pipeline, storage
from .config import get_models
from .db import session_scope
from .models import Asset, Episode, Job

AFMT = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]


def _plan(s, ep: Episode, lang: str) -> list[dict]:
    plan = []
    src = pipeline.source_lang(s, ep)
    for sc in ep.scenes:
        el = pipeline.effective_lang(s, ep, sc, lang)
        v = pipeline.sel(s, sc, "scene_video", el)
        a = pipeline.sel(s, sc, "voice", el)
        a_src = pipeline.sel(s, sc, "voice", pipeline.effective_lang(s, ep, sc, src))
        plan.append({"scene_id": sc.id, "order_no": sc.order_no, "type": sc.type, "audio_moment": bool(sc.audio_moment),
                     "language": el, "dialogue": pipeline.text_of(s, sc, el),
                     "video": storage.abs_path(v.path), "video_id": v.id, "video_language": v.language,
                     "audio": storage.abs_path(a.path) if a else None, "audio_id": a.id if a else None,
                     "audio_language": a.language if a else "",
                     "source_audio_duration": float((a_src.meta or {}).get("duration", 0)) if a_src else 0.0})
    return plan


def normalize_clip(item: dict, work: Path, w: int, h: int, fps: int) -> dict:
    """Sahneyi hedef biçime çevirir; klip + yalnız-konuşma izi (ducking için) üretir."""
    vi = media.info(item["video"])
    ai = media.info(item["audio"]) if item["audio"] else None
    vdur = vi["video_duration"] or vi["duration"]
    adur = (ai or {}).get("duration", 0.0)
    typ = item["type"]
    speech_src, ambience, speech_dur = None, False, 0.0
    if typ == "talking":
        if vi["has_audio"]:
            speech_src, speech_dur = "0:a", vdur
        elif ai:
            speech_src, speech_dur = "1:a", adur
        dur = max(vdur, speech_dur)
    elif typ == "narrator":
        if ai:
            speech_src, speech_dur = "1:a", adur
        ambience = vi["has_audio"] and item["audio_moment"]
        dur = max(vdur, adur + 0.3)
    else:
        # konuşmasız: videonun kendi sesi yalnızca "sesli an" işaretliyse kullanılır
        # (Kling 4.0 sesi kapatamıyor; işaretsiz sahnede ses kurguda atılır)
        ambience = vi["has_audio"] and item["audio_moment"]
        dur = vdur
    dur = round(dur, 3)
    key = hashlib.md5(f"{item['video_id']}:{item['audio_id']}:{typ}:{item['audio_moment']}:{w}x{h}:{fps}".encode()).hexdigest()[:10]
    clip = work / f"clip_{item['scene_id']}_{key}.mp4"
    speech = work / f"speech_{item['scene_id']}_{key}.wav"
    result = {"clip": clip, "speech": speech, "speech_dur": min(speech_dur, dur), "cached": True,
              "source": {"width": vi["width"], "height": vi["height"], "fps": vi["fps"], "video_duration": vdur,
                         "audio_duration": adur, "has_audio": vi["has_audio"], "extended": dur > vdur + 0.05,
                         "source_audio_duration": item.get("source_audio_duration", 0.0)}}
    if clip.exists() and speech.exists():
        result["duration"] = media.info(clip)["duration"]
        return result

    args: list = ["-i", item["video"]]
    if item["audio"]:
        args += ["-i", item["audio"]]
    sil = 2 if item["audio"] else 1
    args += ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo"]
    vf = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
          f"setsar=1,fps={fps},format=yuv420p")
    if dur > vdur:
        vf += f",tpad=stop_mode=clone:stop_duration={dur - vdur + 0.5:.3f}"
    vf += "[v]"
    pad = f"{AFMT},apad=whole_dur={dur},atrim=0:{dur},asetpts=N/SR/TB"
    af = f"[{speech_src or f'{sil}:a'}]{pad},asplit=2[sp1][sp2]"
    if ambience:
        af += f";[0:a]{pad},volume=0.7[amb];[sp1][amb]amix=inputs=2:normalize=0:duration=first[a]"
    else:
        af += ";[sp1]anull[a]"
    args += ["-filter_complex", f"{vf};{af}",
             "-map", "[v]", "-map", "[a]", "-t", f"{dur}", "-r", str(fps), *X264, *AAC, clip,
             "-map", "[sp2]", "-t", f"{dur}", "-c:a", "pcm_s16le", speech]
    media.ffmpeg(args)
    result["cached"] = False
    result["duration"] = media.info(clip)["duration"]
    return result


def build_timeline(clips: list[dict], transition: str, t: float) -> tuple[list[dict], float, float]:
    durs = [c["duration"] for c in clips]
    if transition == "xfade" and len(clips) > 1:
        t = max(0.05, min(t, min(durs) / 3))
    elif transition == "fade":
        t = max(0.05, min(t, min(durs) / 4))
    else:
        t = 0.0
    overlap = t if transition == "xfade" else 0.0
    timeline, pos = [], 0.0
    for i, c in enumerate(clips):
        start = pos
        end = start + c["duration"]
        timeline.append({"scene_id": c["scene_id"], "order_no": c["order_no"], "type": c["type"],
                         "start": round(start, 3), "end": round(end, 3),
                         "speech_end": round(start + c["speech_dur"], 3) if c["speech_dur"] else None})
        pos = end - (overlap if i < len(clips) - 1 else 0.0)
    return timeline, round(pos, 3), t


def _video_graph(n: int, durs: list[float], transition: str, t: float) -> str:
    if transition == "xfade" and n > 1:
        parts, prev, off = [], "0:v", 0.0
        for i in range(1, n):
            off += durs[i - 1] - t
            out = "vout" if i == n - 1 else f"x{i}"
            parts.append(f"[{prev}][{i}:v]xfade=transition=fade:duration={t:.3f}:offset={off:.3f}[{out}]")
            prev = out
        return ";".join(parts)
    parts, labels = [], ""
    for i in range(n):
        if transition == "fade":
            parts.append(f"[{i}:v]fade=t=in:st=0:d={t:.3f},fade=t=out:st={max(durs[i] - t, 0):.3f}:d={t:.3f}[v{i}]")
            labels += f"[v{i}]"
        else:
            labels += f"[{i}:v]"
    parts.append(f"{labels}concat=n={n}:v=1:a=0[vout]")
    return ";".join(parts)


def _audio_chain(idx: list[int], durs: list[float], transition: str, t: float, out: str, tag: str) -> str:
    n = len(idx)
    if transition == "xfade" and n > 1:
        parts, prev = [], f"{idx[0]}:a"
        for k in range(1, n):
            o = out if k == n - 1 else f"{tag}x{k}"
            parts.append(f"[{prev}][{idx[k]}:a]acrossfade=d={t:.3f}:c1=tri:c2=tri[{o}]")
            prev = o
        return ";".join(parts)
    parts, labels = [], ""
    for k, i in enumerate(idx):
        if transition == "fade":
            parts.append(f"[{i}:a]afade=t=in:st=0:d={t:.3f},afade=t=out:st={max(durs[k] - t, 0):.3f}:d={t:.3f}[{tag}{k}]")
            labels += f"[{tag}{k}]"
        else:
            labels += f"[{i}:a]"
    parts.append(f"{labels}concat=n={n}:v=0:a=1[{out}]")
    return ";".join(parts)


def _audio_graph(n: int, durs: list[float], transition: str, t: float, total: float, music_idx: int | None,
                 music_vol: float, ducking: bool, sfx: list[tuple[int, float, float]], norm: str) -> str:
    g = [_audio_chain(list(range(n)), durs, transition, t, "prog", "a")]
    mix = ["[prog]"]
    if music_idx is not None:
        fade = f",afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5" if total > 3 else ""
        g.append(f"[{music_idx}:a]{AFMT},volume={music_vol:.3f},atrim=0:{total:.3f},asetpts=N/SR/TB{fade}[mus]")
        if ducking:
            g.append(_audio_chain(list(range(n, 2 * n)), durs, transition, t, "side", "s"))
            g.append("[mus][side]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=500[duck]")
            mix.append("[duck]")
        else:
            mix.append("[mus]")
    for k, (i, at, vol) in enumerate(sfx):
        g.append(f"[{i}:a]{AFMT},volume={vol:.3f},adelay={int(max(at, 0) * 1000)}:all=1[fx{k}]")
        mix.append(f"[fx{k}]")
    if len(mix) > 1:
        g.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first[mix]")
        src = "[mix]"
    else:
        src = "[prog]"
    g.append(f"{src}{norm}[aout]")
    return ";".join(g)


def _measure(stderr: str) -> dict | None:
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not m:
        return None
    d = json.loads(m[-1])
    try:
        if float(d["input_i"]) < -69:
            return None
    except ValueError:
        return None  # "-inf": tamamen sessiz
    return d


def split_cues(text: str, start: float, end: float, max_chars: int = 84) -> list[tuple[float, float, str]]:
    words = text.split()
    if len(words) <= 1 and len(text) > max_chars:  # boşluksuz yazılar (Çince, Japonca)
        words = [text[i:i + 14] for i in range(0, len(text), 14)]
        joiner = ""
    else:
        joiner = " "
    chunks, cur = [], ""
    for w in words:
        if cur and len(cur) + len(joiner) + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur}{joiner}{w}" if cur else w
    if cur:
        chunks.append(cur)
    total = sum(len(c) for c in chunks) or 1
    cues, pos = [], start
    for c in chunks:
        d = (end - start) * len(c) / total
        lines = c
        if len(c) > 42 and joiner:  # iki satıra böl
            mid = c.rfind(" ", 0, len(c) // 2 + 8)
            if mid > 0:
                lines = c[:mid] + "\n" + c[mid + 1:]
        cues.append((pos, pos + d, lines))
        pos += d
    return cues


def srt_time(sec: float) -> str:
    ms = int(round(max(sec, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(plan: list[dict], timeline: list[dict]) -> str:
    out, n = [], 0
    for item, tl in zip(plan, timeline):
        if not item["dialogue"] or not tl["speech_end"]:
            continue
        start = tl["start"] + 0.05
        end = max(min(tl["speech_end"], tl["end"]) - 0.05, start + 0.5)
        for a, b, text in split_cues(item["dialogue"], start, end):
            n += 1
            out.append(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n")
    return "\n".join(out)


def sub_filter(lang: str, vertical: bool = False) -> str:
    """Altyazı gömme süzgeci: dile uygun yazı tipi. Yön (sağdan sola) libass tarafından metinden belirlenir."""
    font = languages.font_for(lang).replace("'", "")
    size, margin = (13, 60) if vertical else (20, 28)
    return f"subtitles=altyazi.srt:force_style='FontName={font},FontSize={size},Outline=1,Shadow=0,MarginV={margin}'"


def verify_languages(plan: list[dict], lang: str) -> None:
    """Kurgu öncesi son doğrulama: hiçbir sahnede başka dilin sesi ya da konuşan videosu kullanılamaz."""
    bad = []
    for it in plan:
        if it["audio"] and it["audio_language"] != it["language"]:
            bad.append(f"Sahne {it['order_no']}: ses {it['audio_language']}, beklenen {it['language']}")
        if it["type"] == "talking" and it["video_language"] != it["language"]:
            bad.append(f"Sahne {it['order_no']}: konuşan video {it['video_language'] or 'dilsiz'}, beklenen {it['language']}")
    if bad:
        raise pipeline.StepBlocked(f"Dil uyuşmazlığı ({lang}): " + "; ".join(bad))


def run(job_id: int, progress: Callable[[float], None] = lambda v: None) -> int:
    target = get_models()["montage"]
    w, h, fps = target["width"], target["height"], target["fps"]
    with session_scope() as s:
        job = s.get(Job, job_id)
        ep = s.get(Episode, job.episode_id)
        lang = job.language or pipeline.source_lang(s, ep)
        blockers = pipeline.montage_blockers(s, ep, lang)
        if blockers:
            raise pipeline.StepBlocked(f"Kurgu ({lang}) yapılamıyor: " + "; ".join(blockers))
        cfg = pipeline.ep_settings(ep)
        plan = _plan(s, ep, lang)
        signature = pipeline.montage_signature(s, ep, lang)
        ep_dir = storage.episode_dir(s, ep)
        extra = s.scalars(select(Asset).where(Asset.episode_id == ep.id, Asset.role.in_(("music", "sfx")))
                          .order_by(Asset.id)).all()
        music = [a for a in extra if a.role == "music"]
        music_path = storage.abs_path(music[-1].path) if music else None
        sfx_items = [(storage.abs_path(a.path), float((a.params or {}).get("at", 0)), float((a.params or {}).get("volume", 1)))
                     for a in extra if a.role == "sfx"]
        project_id, ep_id, owner_id = ep.project_id, ep.id, job.user_id
    verify_languages(plan, lang)
    cache = ep_dir / "ortak" / "kurgu_onbellek"  # klipler diller arasında paylaşılır
    cache.mkdir(parents=True, exist_ok=True)
    work = ep_dir / storage.lang_dir(lang) / "kurgu" / "_is"
    work.mkdir(parents=True, exist_ok=True)

    # 1) sahne kliplerini eşitle (değişmeyenler ve diğer dilde üretilmiş ortak klipler önbellekten)
    clips, rebuilt = [], []
    for i, item in enumerate(plan):
        c = normalize_clip(item, cache, w, h, fps)
        c.update(scene_id=item["scene_id"], order_no=item["order_no"], type=item["type"])
        if not c["cached"]:
            rebuilt.append(item["order_no"])
        clips.append(c)
        progress(0.05 + 0.45 * (i + 1) / len(plan))
    # önbellek temizliği: hiçbir dilin son kurgusunda kullanılmayan klipler silinir
    (cache / f"kullanim_{lang}.json").write_text(json.dumps([c["clip"].name for c in clips] + [c["speech"].name for c in clips]),
                                                 encoding="utf-8")
    keep: set[str] = set()
    for f in cache.glob("kullanim_*.json"):
        keep |= set(json.loads(f.read_text(encoding="utf-8")))
    for f in list(cache.glob("clip_*")) + list(cache.glob("speech_*")):
        if f.name not in keep:
            f.unlink(missing_ok=True)

    transition = cfg["transition"] if cfg["transition"] in ("cut", "fade", "xfade") else "fade"
    timeline, total, t = build_timeline(clips, transition, float(cfg["transition_dur"]))
    durs = [c["duration"] for c in clips]
    n = len(clips)

    # 2) girdiler: klipler, konuşma izleri, müzik, efektler
    inputs: list = []
    for c in clips:
        inputs += ["-i", c["clip"]]
    idx = n
    use_duck = bool(music_path and cfg["ducking"])
    if use_duck:
        for c in clips:
            inputs += ["-i", c["speech"]]
        idx = 2 * n
    music_idx = None
    if music_path:
        inputs += ["-stream_loop", "-1", "-i", music_path]
        music_idx, idx = idx, idx + 1
    sfx = []
    for path, at, vol in sfx_items:
        inputs += ["-i", path]
        sfx.append((idx, at, vol))
        idx += 1

    def agraph(norm: str) -> str:
        return _audio_graph(n, durs, transition, t, total, music_idx, float(cfg["music_volume"]), use_duck, sfx, norm)

    # 3) ses yüksekliği: iki geçişli loudnorm (-14 LUFS)
    lufs, tp = target["loudness_lufs"], target["true_peak_db"]
    base = f"loudnorm=I={lufs}:TP={tp}:LRA=11"
    err = media.ffmpeg([*inputs, "-filter_complex", agraph(base + ":print_format=json"), "-map", "[aout]", "-vn", "-f", "null", "-"])
    m = _measure(err)
    progress(0.6)
    if m:
        norm = (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
                f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,{AFMT}")
    else:
        norm = "anull"  # tümüyle sessiz bölüm: normalize edilecek bir şey yok

    # 4) ana kurgu
    master = work / "master.mp4"
    media.ffmpeg([*inputs, "-filter_complex", _video_graph(n, durs, transition, t) + ";" + agraph(norm),
                  "-map", "[vout]", "-map", "[aout]", "-t", f"{total}", "-r", str(fps), *X264, *AAC,
                  "-movflags", "+faststart", master])
    progress(0.8)

    # 5) altyazı ve çıktı dosyaları (dil başına)
    srt_text = build_srt(plan, timeline) if cfg["subtitles"] else ""
    burn = bool(srt_text and cfg["burn_subtitles"])
    if srt_text:
        (work / "altyazi.srt").write_text(srt_text, encoding="utf-8")
    with session_scope() as s:
        ep = s.get(Episode, ep_id)
        version = storage.next_version(s, role="final", episode_id=ep_id, language=lang)
        final = storage.target_path(s, role="final", version=version, ext=".mp4", episode=ep, language=lang)
    if burn:
        media.ffmpeg(["-i", "master.mp4", "-vf", sub_filter(lang), *X264, "-c:a", "copy", "-movflags", "+faststart",
                      final.resolve()], cwd=work)
    else:
        shutil.copyfile(master, final)
    progress(0.9)
    vertical = None
    if cfg["vertical"]:
        vertical = final.with_name(f"final_dikey_v{version}.mp4")
        vw, vh = h, w
        g = (f"[0:v]split[a][b];[a]scale={vw}:{vh}:force_original_aspect_ratio=increase,crop={vw}:{vh},boxblur=20:5[bg];"
             f"[b]scale={vw}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2" + (f",{sub_filter(lang, True)}" if burn else "") + "[v]")
        media.ffmpeg(["-i", "master.mp4", "-filter_complex", g, "-map", "[v]", "-map", "0:a", *X264, "-c:a", "copy",
                      "-movflags", "+faststart", vertical.resolve()], cwd=work)
    info = media.validate(final, "video")
    meta = {**info, "timeline": timeline, "signature": signature, "transition": transition, "language": lang,
            "rebuilt_scenes": rebuilt, "target": {"width": w, "height": h, "fps": fps, "lufs": lufs},
            "loudness_in": m, "music": bool(music_path), "ducking": use_duck,
            "subtitle_font": languages.font_for(lang) if burn else "", "rtl": languages.is_rtl(lang),
            "sources": {c["scene_id"]: c["source"] for c in clips}}

    with session_scope() as s:
        def add(role: str, kind: str, path: Path, meta_: dict) -> Asset:
            storage.publish(path)
            a = Asset(user_id=owner_id, project_id=project_id, episode_id=ep_id, kind=kind, role=role, language=lang,
                      path=storage.rel_path(path), version=version, step=5, provider="ffmpeg", mode="local",
                      status="approved", approved_by="auto", job_id=job_id, meta=meta_, params=cfg)
            s.add(a)
            s.flush()
            return a
        fa = add("final", "video", final, meta)
        if vertical:
            add("final_vertical", "video", vertical, media.validate(vertical, "video"))
        if srt_text:
            srt = final.with_name(f"altyazi_{lang}_v{version}.srt")
            srt.write_text(srt_text, encoding="utf-8")
            add("subtitles", "text", srt, {"language": lang, "rtl": languages.is_rtl(lang)})
        pipeline.emit(s, "refresh", episode_id=ep_id)
        return fa.id
