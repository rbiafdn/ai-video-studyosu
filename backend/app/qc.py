"""6. adım: otomatik kalite raporu (dil başına). Her bulgu ilgili sahneye bağlı ve zaman kodludur.

Teknik denetimler ffprobe/ffmpeg ile yapılır. Ek olarak:
- Dil uyumu: her sahnede kullanılan sesin ve konuşan videonun dili beklenen dille aynı mı
  (varlık üst verisi + sağlayıcıya gönderilen dil kodu + metnin algılanan dili). Yerelde konuşma
  tanıma yoktur; ses içeriği dinlenerek doğrulanmaz.
- Dudak senkronu: konuşan sahnelerde ses zarfı ile ağız bölgesindeki hareketin kayması ölçülür;
  eşiği aşan sahneler "Kling dudak senkronu ile düzelt" önerisiyle listelenir.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from . import languages, media, pipeline, storage
from .config import get_models
from .db import session_scope
from .models import Asset, Episode, Job, QCReport

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


def scene_at(timeline: list[dict], start: float, end: float | None = None) -> dict | None:
    """Zaman aralığıyla en çok örtüşen sahne (aralık iki sahneye yayılıyorsa ağırlıklı olanı)."""
    if not timeline:
        return None
    if end is None or end <= start:
        end = start + 0.001
    best, best_ov = None, 0.0
    for tl in timeline:
        ov = min(end, tl["end"]) - max(start, tl["start"])
        if ov > best_ov:
            best, best_ov = tl, ov
    return best or (timeline[-1] if start >= timeline[-1]["end"] - 0.05 else None)


def parse_black(err: str) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", err)]


def parse_freeze(err: str) -> list[tuple[float, float]]:
    starts = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", err)]
    durs = [float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", err)]
    return [(st, durs[i] if i < len(durs) else 0.0) for i, st in enumerate(starts)]


def parse_silence(err: str, total: float) -> list[tuple[float, float]]:
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
    return [(max(st, 0.0), ends[i] if i < len(ends) else total) for i, st in enumerate(starts)]


def lipsync_finding(measure: dict, threshold_ms: float, min_conf: float) -> tuple[str, str] | None:
    """Ölçümü (önem, ileti) çiftine çevirir; sorun yoksa None."""
    if not measure.get("measurable"):
        return "info", "Dudak senkronu ölçülemedi (video çok kısa)"
    off, conf = measure["offset_ms"], measure["confidence"]
    if conf < min_conf:
        return "info", f"Dudak senkronu güvenilir ölçülemedi (ilişki {conf:.2f}); gözle kontrol edin"
    if abs(off) > threshold_ms:
        yon = "geride" if off > 0 else "önde"
        return "warn", f"Ses ağız hareketinden {abs(off)} ms {yon} (eşik {threshold_ms:.0f} ms, güven {conf:.2f})"
    return None


def run(job_id: int, progress: Callable[[float], None] = lambda v: None) -> int:
    cfg_all = get_models()
    target, qcfg = cfg_all["montage"], cfg_all.get("qc") or {}
    findings: list[dict] = []

    def add(sev: str, code: str, message: str, tl: dict | None = None, t: float | None = None, **extra) -> None:
        when = t if t is not None else (tl["start"] if tl else None)
        findings.append({"severity": sev, "code": code, "message": message,
                         "scene_id": tl["scene_id"] if tl else None, "scene_no": tl["order_no"] if tl else None,
                         "time": round(when, 3) if when is not None else None,
                         "timecode": media.timecode(when) if when is not None else "", **extra})

    with session_scope() as s:
        job = s.get(Job, job_id)
        ep = s.get(Episode, job.episode_id)
        lang = job.language or pipeline.source_lang(s, ep)
        final = pipeline.latest_final(s, ep.id, lang)
        if final is None:
            raise pipeline.StepBlocked(f"Kontrol için {lang} kurgu çıktısı yok")
        path = storage.abs_path(final.path)
        meta = dict(final.meta or {})
        timeline: list[dict] = meta.get("timeline") or []
        by_scene = {tl["scene_id"]: tl for tl in timeline}
        fresh = meta.get("signature") == pipeline.montage_signature(s, ep, lang)
        final_id, ep_id, project_id, owner_id = final.id, ep.id, ep.project_id, job.user_id
        scenes = []
        for sc in ep.scenes:
            el = pipeline.effective_lang(s, ep, sc, lang)
            v, a = pipeline.sel(s, sc, "scene_video", el), pipeline.sel(s, sc, "voice", el)
            row = pipeline.text_row(s, sc, el)
            scenes.append({"id": sc.id, "no": sc.order_no, "type": sc.type, "audio_moment": sc.audio_moment,
                           "planned": float(sc.duration or 0), "has_video": v is not None, "lang": el,
                           "vdur": (v.meta or {}).get("duration", 0) if v else 0,
                           "adur": (a.meta or {}).get("duration", 0) if a else 0,
                           "video_path": storage.abs_path(v.path) if v else None,
                           "audio_path": storage.abs_path(a.path) if a else None,
                           "video_lang": v.language if v else "", "audio_lang": a.language if a else None,
                           "audio_code": (a.params or {}).get("language_code") if a else None,
                           "audio_mode": a.mode if a else "",
                           "text_detected": row.detected_language if row else "",
                           "text_conf": row.confidence if row else 0.0})
        out_dir = storage.episode_dir(s, ep) / storage.lang_dir(lang) / "kurgu"

    # 1) eksik sahne
    for sc in scenes:
        if by_scene.get(sc["id"]) is None:
            findings.append({"severity": "error", "code": "eksik_sahne", "scene_id": sc["id"], "scene_no": sc["no"],
                             "message": f"Sahne {sc['no']} kurguda yok" + ("" if sc["has_video"] else " (onaylı videosu yok)"),
                             "time": None, "timecode": ""})
    if not fresh:
        add("warn", "kurgu_guncel_degil", "Kurgu son onaylanan sürümlerle eşleşmiyor; kurguyu yeniden çalıştırın")

    # 2) çıktı dosyası: süre / çözünürlük / fps / ses-video farkı
    info = media.info(path)
    if (info["width"], info["height"]) != (target["width"], target["height"]):
        add("error", "cozunurluk", f"Çıktı {info['width']}x{info['height']}; hedef {target['width']}x{target['height']}")
    if abs(info["fps"] - target["fps"]) > 0.01:
        add("error", "fps", f"Çıktı {info['fps']} fps; hedef {target['fps']} fps")
    expected = timeline[-1]["end"] if timeline else 0.0
    if abs(info["duration"] - expected) > 0.25:
        add("warn", "sure", f"Çıktı süresi {info['duration']:.2f} sn; zaman çizelgesi {expected:.2f} sn")
    if not info["has_audio"]:
        add("error", "ses_yok", "Çıktıda ses akışı yok")
    elif abs(info["video_duration"] - info["audio_duration"]) > 0.1:
        add("warn", "ses_video_sure_farki",
            f"Ses {info['audio_duration']:.2f} sn, görüntü {info['video_duration']:.2f} sn (fark {abs(info['video_duration'] - info['audio_duration']):.2f} sn)")

    # 3) sahne kaynakları, dil uyumu ve çeviri süre farkı
    sources = meta.get("sources") or {}
    ratio = float(qcfg.get("duration_diff_ratio", 0.25))
    for sc in scenes:
        tl = by_scene.get(sc["id"])
        src = sources.get(str(sc["id"])) or sources.get(sc["id"])
        if tl is None or not src:
            continue
        if (src["width"], src["height"]) != (target["width"], target["height"]) or abs(src["fps"] - target["fps"]) > 0.01:
            add("info", "kaynak_bicim", f"Kaynak {src['width']}x{src['height']} @ {src['fps']:g} fps; kurguda "
                f"{target['width']}x{target['height']} @ {target['fps']} fps'e eşitlendi", tl)
        # dil uyumu: sesin dili sahnenin dili mi?
        if sc["audio_lang"] is not None:
            if sc["audio_lang"] != sc["lang"]:
                add("error", "dil_uyumu", f"Ses {sc['audio_lang']} dilinde; sahnenin dili {sc['lang']}", tl, suggest="regen_audio")
            elif sc["audio_mode"] == "real" and sc["audio_code"] and sc["audio_code"] != languages.base(sc["lang"]):
                add("error", "dil_uyumu", f"Ses sağlayıcıya '{sc['audio_code']}' dil koduyla gönderilmiş; beklenen "
                    f"'{languages.base(sc['lang'])}'", tl, suggest="regen_audio")
            elif (sc["text_detected"] and sc["text_conf"] >= 0.85 and len(sc["text_detected"]) == 2
                  and sc["text_detected"] != languages.base(sc["lang"])):
                add("warn", "dil_uyumu", f"Replik metni {languages.name(sc['text_detected'])} gibi görünüyor "
                    f"(güven {sc['text_conf']:.2f}); sahnenin dili {sc['lang']}", tl)
        if sc["type"] == "talking" and sc["video_lang"] != sc["lang"]:
            add("error", "dil_uyumu", f"Konuşan video {sc['video_lang'] or 'dilsiz'}; sahnenin dili {sc['lang']}", tl,
                suggest="regen_scene")
        if sc["type"] == "talking" and sc["adur"] and abs(sc["adur"] - src["video_duration"]) > 0.5:
            add("warn", "ses_video_sure_farki", f"Replik sesi {sc['adur']:.2f} sn, konuşan video {src['video_duration']:.2f} sn", tl)
        if src.get("extended"):
            add("info", "son_kare_donduruldu", f"Anlatıcı sesi videodan uzun ({sc['adur']:.2f} sn > {src['video_duration']:.2f} sn); "
                "son kare donduruldu", tl, t=tl["start"] + src["video_duration"])
        base_dur = float(src.get("source_audio_duration") or 0)
        if base_dur and sc["adur"] and abs(sc["adur"] - base_dur) > max(0.8, ratio * base_dur):
            add("info", "ceviri_suresi", f"{sc['lang']} sesi {sc['adur']:.1f} sn; kaynak dilde {base_dur:.1f} sn "
                f"({sc['adur'] - base_dur:+.1f} sn). Sahne süresi bu dile göre ayarlandı", tl)
        clip = tl["end"] - tl["start"]
        if sc["planned"] and abs(clip - sc["planned"]) > max(1.5, 0.3 * sc["planned"]):
            add("info", "plan_suresi", f"Planlanan süre {sc['planned']:.1f} sn, gerçekleşen {clip:.1f} sn", tl)
    progress(0.2)

    # 4) dudak senkronu: konuşan sahnelerde ses ile ağız hareketi arasındaki kayma
    thr, min_conf = float(qcfg.get("lipsync_threshold_ms", 120)), float(qcfg.get("lipsync_min_confidence", 0.3))
    sync = {}
    for sc in scenes:
        tl = by_scene.get(sc["id"])
        if sc["type"] != "talking" or tl is None or not sc["video_path"]:
            continue
        try:
            m = media.lipsync_offset(sc["video_path"])
            if sc["audio_path"]:
                a0, v0 = media.speech_onset(sc["audio_path"]), media.speech_onset(sc["video_path"])
                m["onset_diff_ms"] = int(round((v0 - a0) * 1000)) if a0 >= 0 and v0 >= 0 else None
        except media.MediaError as e:
            add("info", "dudak_senkronu", f"Dudak senkronu ölçülemedi: {e}", tl)
            continue
        sync[sc["no"]] = m
        res = lipsync_finding(m, thr, min_conf)
        if res:
            add(res[0], "dudak_senkronu", res[1], tl, offset_ms=m["offset_ms"], confidence=m["confidence"],
                **({"suggest": "lipsync", "suggest_label": "Kling dudak senkronu ile düzelt"} if res[0] == "warn" else {}))
        od = m.get("onset_diff_ms")
        if od is not None and abs(od) > max(thr, 250):
            add("warn", "dudak_senkronu", f"Videodaki ses, ses dosyasına göre {abs(od)} ms {'geç' if od > 0 else 'erken'} başlıyor",
                tl, suggest="lipsync", suggest_label="Kling dudak senkronu ile düzelt")
    progress(0.4)

    # 5) siyah kare ve donmuş kare
    err = media.ffmpeg(["-i", path, "-vf", "scale=480:-2,blackdetect=d=0.4:pix_th=0.10,freezedetect=n=-50dB:d=1.5",
                        "-an", "-f", "null", "-"])
    for st, en in parse_black(err):
        add("warn", "siyah_kare", f"{en - st:.2f} sn siyah görüntü", scene_at(timeline, st, en), t=st, duration=round(en - st, 3))
    for st, d in parse_freeze(err):
        tl = scene_at(timeline, st, st + d)
        expected_freeze = bool(tl and (sources.get(str(tl["scene_id"])) or {}).get("extended"))
        # konuşan sahnede kadraj sabittir ve yalnızca ağız oynar; tüm kareye bakan donma tespiti bunu
        # "donmuş" sayabilir. Ağız hareketi sesle ilişkiliyse (dudak ölçümü güvenilir) uyarı verilmez.
        m = sync.get(tl["order_no"]) if tl else None
        mouth_moves = bool(tl and tl["type"] == "talking" and m and m.get("confidence", 0) >= min_conf)
        note = (" (anlatıcı sesi için uzatılan son kare)" if expected_freeze else
                " (konuşan sahne: kadraj sabit, ağız hareketi var)" if mouth_moves else "")
        add("info" if expected_freeze or mouth_moves else "warn", "donmus_kare", f"{d:.2f} sn donmuş görüntü" + note,
            tl, t=st, duration=round(d, 3))
    progress(0.6)

    # 6) sessizlik, ses patlaması, ses yüksekliği
    loud = None
    if info["has_audio"]:
        err = media.ffmpeg(["-i", path, "-af", "silencedetect=n=-45dB:d=1.5,ebur128=peak=true", "-vn", "-f", "null", "-"])
        for st, en in parse_silence(err, info["duration"]):
            tl = scene_at(timeline, st, en)
            quiet_ok = bool(tl and tl["type"] == "silent" and not meta.get("music"))
            add("info" if quiet_ok else "warn", "sessizlik",
                f"{en - st:.2f} sn sessizlik" + (" (konuşmasız sahne, müzik yok)" if quiet_ok else ""),
                tl, t=st, duration=round(en - st, 3))
        summary_txt = err[err.rfind("Summary:"):] if "Summary:" in err else ""
        mi = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", summary_txt)
        mp = re.search(r"Peak:\s*(-?[\d.]+)\s*dBFS", summary_txt)
        loud = {"integrated": float(mi.group(1)) if mi else None, "true_peak": float(mp.group(1)) if mp else None}
        if loud["integrated"] is not None and loud["integrated"] > -69 and abs(loud["integrated"] - target["loudness_lufs"]) > 1.0:
            add("warn", "ses_yuksekligi", f"Bütünleşik yükseklik {loud['integrated']:.1f} LUFS; hedef {target['loudness_lufs']} LUFS")
        progress(0.8)
        for tl in timeline:
            err = media.ffmpeg(["-ss", f"{tl['start']}", "-t", f"{tl['end'] - tl['start']}", "-i", path,
                                "-af", "volumedetect", "-vn", "-f", "null", "-"])
            mv = re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
            if mv and float(mv.group(1)) >= -0.1:
                add("warn", "ses_patlamasi", f"Ses tavana vuruyor (en yüksek {float(mv.group(1)):.1f} dB): kırpılma olabilir", tl)
    progress(0.9)

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["time"] if f["time"] is not None else -1))
    summary = {"language": lang, "duration": round(info["duration"], 3), "width": info["width"], "height": info["height"],
               "fps": info["fps"], "scenes": len(timeline), "loudness": loud, "lipsync": sync, "final_version": None,
               "errors": sum(1 for f in findings if f["severity"] == "error"),
               "warnings": sum(1 for f in findings if f["severity"] == "warn"),
               "infos": sum(1 for f in findings if f["severity"] == "info")}
    summary["verdict"] = "hata" if summary["errors"] else ("uyarı" if summary["warnings"] else "temiz")

    with session_scope() as s:
        fa = s.get(Asset, final_id)
        summary["final_version"] = fa.version
        rep = QCReport(episode_id=ep_id, language=lang, final_asset_id=final_id, summary=summary, findings=findings)
        s.add(rep)
        out = out_dir / f"kalite_raporu_{lang}_v{fa.version}.json"
        out.write_text(json.dumps({"summary": summary, "findings": findings}, ensure_ascii=False, indent=2), encoding="utf-8")
        storage.publish(out)
        a = Asset(user_id=owner_id, project_id=project_id, episode_id=ep_id, kind="text", role="qc_report", language=lang,
                  path=storage.rel_path(out), version=fa.version, step=6, provider="ffmpeg", mode="local",
                  status="approved", approved_by="auto", job_id=job_id, meta=summary)
        s.add(a)
        s.flush()
        pipeline.emit(s, "refresh", episode_id=ep_id)
        return a.id
