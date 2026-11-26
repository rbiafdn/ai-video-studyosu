"""Senaryo içe aktarma: yapıştırılan metin, CSV ya da JSON -> sahne satırları.

Sütunlar: sahne no, tür, karakter, replik, dil (boşsa otomatik algıla), görsel prompt, video prompt, süre
(+ isteğe bağlı sesli an, not). Başlıksız 7 sütunlu eski biçim (dil sütunu olmadan) da kabul edilir.
Satırlar services.write_scenes ile bölüme yazılır.
"""
from __future__ import annotations

import csv
import io
import json
import re

from . import languages
from .storage import slug

ALIASES = {
    "order_no": ("sahne_no", "sahne", "no", "sira", "sira_no", "scene", "order"),
    "type": ("tur", "tip", "type"),
    "characters": ("karakter", "karakterler", "character", "characters"),
    "dialogue": ("replik", "metin", "diyalog", "dialogue", "text"),
    "language": ("dil", "language", "lang"),
    "image_prompt": ("gorsel_prompt", "gorsel", "image_prompt", "image"),
    "video_prompt": ("video_prompt", "video", "hareket"),
    "duration": ("sure", "saniye", "duration"),
    "audio_moment": ("sesli_an", "sesli", "native_audio", "audio_moment"),
    "notes": ("not", "notlar", "notes"),
}
ORDER = ["order_no", "type", "characters", "dialogue", "language", "image_prompt", "video_prompt", "duration",
         "audio_moment", "notes"]
ORDER_OLD = [f for f in ORDER if f != "language"]
TYPES = {"konusan": "talking", "talking": "talking", "diyalog": "talking", "konusma": "talking",
         "konusmasiz": "silent", "sessiz": "silent", "silent": "silent",
         "anlatici": "narrator", "narrator": "narrator", "dis-ses": "narrator"}


def _key(name: str) -> str | None:
    k = slug(name).replace("-", "_")
    for field, names in ALIASES.items():
        if k in names:
            return field
    return None


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "evet", "e", "x", "var", "yes", "açık", "acik")


def _norm(raw: dict, index: int) -> dict:
    chars = raw.get("characters") or []
    if isinstance(chars, str):
        chars = [c.strip() for c in re.split(r"[,&+/]| ve ", chars) if c.strip()]
    typ = TYPES.get(slug(str(raw.get("type") or "")), "")
    dialogue = str(raw.get("dialogue") or "").strip()
    if not typ:
        typ = "talking" if dialogue and chars else ("narrator" if dialogue else "silent")
    try:
        duration = float(str(raw.get("duration") or "5").replace(",", ".").replace("sn", "").strip() or 5)
    except ValueError:
        duration = 5.0
    try:
        order_no = int(float(str(raw.get("order_no") or index)))
    except ValueError:
        order_no = index
    return {"order_no": order_no, "type": typ, "characters": chars, "dialogue": dialogue,
            "language": languages.normalize(str(raw.get("language") or "")),
            "image_prompt": str(raw.get("image_prompt") or "").strip(),
            "video_prompt": str(raw.get("video_prompt") or "").strip(),
            "duration": duration, "audio_moment": _truthy(raw.get("audio_moment") or ""),
            "notes": str(raw.get("notes") or "").strip()}


def _from_rows(rows: list[list[str]]) -> list[dict]:
    if not rows:
        return []
    header = [_key(c) for c in rows[0]]
    if sum(1 for h in header if h) >= 3:
        fields, body = header, rows[1:]
    else:
        width = max(len(r) for r in rows)
        # 5. sütun dil koduna benziyorsa yeni biçim; değilse dil sütunsuz eski biçim
        looks_lang = all(len(r) < 5 or not r[4].strip() or re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z]{2,4})?", r[4].strip())
                         for r in rows)
        fields, body = (ORDER if width >= 8 and looks_lang else ORDER_OLD), rows
    out = []
    for r in body:
        if not any(c.strip() for c in r):
            continue
        out.append({f: r[i].strip() for i, f in enumerate(fields) if f and i < len(r)})
    return out


def tr_title(name: str) -> str:
    """BÜYÜK HARFLİ adı Türkçe kurallarıyla 'Baş Harfi Büyük' yapar (İ/ı doğru çevrilir)."""
    if not name.isupper():
        return name
    words = []
    for w in name.replace("İ", "i").replace("I", "ı").lower().split():
        words.append(("İ" if w[0] == "i" else w[0].upper()) + w[1:])
    return " ".join(words)


def _from_script(text: str) -> list[dict]:
    """Serbest metin: 'AD: replik' konuşan sahne, 'ANLATICI: ...' anlatıcı, diğer satırlar konuşmasız."""
    out = []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        m = re.match(r"^([^:]{1,40}):\s*(.+)$", line)
        if m:
            name, said = m.group(1).strip(), m.group(2).strip()
            if slug(name) in ("anlatici", "narrator", "dis-ses"):
                out.append({"type": "narrator", "dialogue": said})
            else:
                out.append({"type": "talking", "characters": tr_title(name), "dialogue": said})
        else:
            desc = line.strip("[]() ")
            out.append({"type": "silent", "image_prompt": desc, "video_prompt": desc})
    return out


def parse(text: str, filename: str = "") -> list[dict]:
    text = (text or "").lstrip("﻿").strip()
    if not text:
        return []
    raws: list[dict]
    if filename.lower().endswith(".json") or text[0] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("scenes") or data.get("sahneler") or []
        raws = [{(_key(k) or k): v for k, v in row.items()} for row in data]
    else:
        lines = [l for l in text.splitlines() if l.strip()]
        # ayırıcı: en çok satırda (en az 3 kez) geçen; tablonun önündeki açıklama satırları atlanır
        counts = {d: sum(1 for l in lines if l.count(d) >= 3) for d in ("|", "\t", ";", ",")}
        delim = max(counts, key=counts.get) if max(counts.values()) >= max(1, len(lines) // 2) else None
        if delim:
            table = [l for l in lines if l.count(delim) >= 3]
            if delim == "|":
                raws = _from_rows([[c.strip() for c in l.strip().strip("|").split("|")] for l in table
                                   if not re.fullmatch(r"[\s|:\-]+", l)])
            else:
                raws = _from_rows(list(csv.reader(io.StringIO("\n".join(table)), delimiter=delim)))
        else:
            raws = _from_script(text)
    return [_norm(r, i) for i, r in enumerate(raws, 1)]


def preview(rows: list[dict], fallback_language: str) -> list[dict]:
    """Önizleme: dil sütunu boş satırlar için algılanan dili öneri olarak ekler."""
    out = []
    for r in rows:
        d = languages.detect(r["dialogue"], fallback_language) if r["dialogue"] and not r["language"] else None
        out.append({**r, "detected": d})
    return out
