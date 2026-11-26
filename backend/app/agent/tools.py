"""Yönetmen ajanının araçları.

Her araç, arayüz düğmeleriyle AYNI servis katmanını (services.py / pipeline.py) çağırır.
Ajan yalnızca burada tanımlı araçları kullanabilir: kabuk komutu, serbest dosya erişimi ya da
rastgele ağ isteği aracı yoktur. Her araç yalnızca sohbetin kendi projesi üzerinde çalışır.
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from sqlalchemy import select

from .. import connections, languages, media, pipeline, services, settings_store as store, storage, tenancy
from ..config import get_models
from ..db import session_scope
from ..models import AgentRun, Asset, Character, ChatMessage, Conversation, Episode, Job, Project, Scene, now

PRODUCTION_TOOLS = ("adim_calistir", "kurgu_yap", "duzeltme_baslat")
STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
LANG = {"type": "string", "description": "BCP-47 dil kodu (tr, en, de, ar, es-MX ...)"}
LANG_SCOPE = {"type": "string", "description": "Dil kodu ya da '*' (tüm hedef diller). Varsayılan '*'."}


def _tool(name: str, description: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description, "eager_input_streaming": True,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or [],
                             "additionalProperties": False}}


def definitions() -> list[dict]:
    """Araç tanımları. Sıra ve içerik sabittir (önbellek ve geçmiş bütünlüğü için)."""
    scene = {"type": "object", "additionalProperties": False, "required": ["sahne_no", "tur"], "properties": {
        "sahne_no": INT,
        "tur": {"type": "string", "enum": ["konuşan", "konuşmasız", "anlatıcı"]},
        "karakterler": {"type": "array", "items": STR, "description": "Karakter adları. Konuşan sahnede ilk karakter konuşur."},
        "replik": {"type": "string", "description": "Kaynak dildeki replik ya da anlatıcı metni (konuşmasız sahnede boş)."},
        "dil": {**LANG, "description": "Yalnızca bu sahne bölümün dilinden FARKLI bir dilde kalacaksa verin."},
        "gorsel_prompt": {"type": "string", "description": "İngilizce, dilden bağımsız. İçinde replik ya da okunacak yazı olmasın."},
        "video_prompt": {"type": "string", "description": "İngilizce hareket/kamera tarifi."},
        "sure": {"type": "number", "description": "Saniye. Konuşan sahnede süreyi ses belirler."},
        "sesli_an": {"type": "boolean", "description": "Konuşmasız sahnede ortam sesi (dalga, kapı...) isteniyorsa true."},
        "gorseldeki_metin": {"type": "object", "additionalProperties": STR,
                             "description": "Görselde okunacak yazı varsa dil başına: {\"tr\": \"...\", \"en\": \"...\"}. Sahneyi dile bağlı yapar."},
        "not": STR}}
    return [
        _tool("proje_olustur", "Yeni proje oluşturur ve bu sohbete bağlar. Her sohbette bir kez çağrılır.",
              {"ad": STR, "aciklama": STR, "dil": {**LANG, "description": "Projenin varsayılan (kaynak) dili."},
               "gorsel_uslup": {"type": "string", "description": "Tüm görsellere eklenecek ortak üslup (İngilizce)."}}, ["ad", "dil"]),
        _tool("bolum_olustur", "Projede yeni bölüm oluşturur ve sohbete bağlar.",
              {"baslik": STR, "diller": {"type": "array", "items": LANG, "description": "Hedef diller (kaynak dil dahil)."}}),
        _tool("karakter_olustur", "Projeye karakter ekler. Anlatıcı sesi için anlatici=true (görseli olmaz).",
              {"ad": STR, "aciklama": {"type": "string", "description": "Görünüş tarifi (İngilizce): yaş, saç, kıyafet, ayırt edici özellikler."},
               "anlatici": BOOL}, ["ad"]),
        _tool("karakter_guncelle", "Karakterin adını ya da görünüş açıklamasını değiştirir.",
              {"karakter_id": INT, "ad": STR, "aciklama": STR}, ["karakter_id"]),
        _tool("ses_listele", "ElevenLabs ses kütüphanesini listeler (ad, cinsiyet, yaş, aksan, dil). Karakterlere ses seçmek için.",
              {"dil": LANG}),
        _tool("karaktere_ses_ata", "Karaktere ses atar. dil verilirse yalnızca o dil için ayrı ses tanımlanır; verilmezse varsayılan ses olur.",
              {"karakter_id": INT, "ses_kimligi": STR, "dil": LANG}, ["karakter_id", "ses_kimligi"]),
        _tool("sahneleri_yaz", "Sahneleri toplu ekler ya da günceller (sahne_no ile eşleşir). Prompt düzeltmek için de kullanılır; "
              "yalnızca değişen alanları gönderebilirsiniz.",
              {"sahneler": {"type": "array", "items": scene, "minItems": 1},
               "hepsini_degistir": {"type": "boolean", "description": "true: mevcut sahneleri silip baştan yazar."}}, ["sahneler"]),
        _tool("dil_ekle", "Bölüme hedef dil ekler. Sonra replik_cevir ile çevirileri yazın. Yalnızca dile bağlı varlıklar üretilir.",
              {"dil": LANG}, ["dil"]),
        _tool("replik_cevir", "Bir hedef dil için çevirdiğiniz replikleri yazar ve onaylar. Çeviriyi siz yaparsınız.",
              {"dil": LANG, "ceviriler": {"type": "array", "minItems": 1, "items": {
                  "type": "object", "additionalProperties": False, "required": ["sahne_no", "metin"],
                  "properties": {"sahne_no": INT, "metin": STR}}}}, ["dil", "ceviriler"]),
        _tool("adim_calistir", "Üretim adımını başlatır: 1 sesler, 2 görseller, 3 konuşan sahneler, 4 konuşmasız sahneler. "
              "İşler arka planda çalışır; bitmesini isleri_bekle ile bekleyin.",
              {"adim": {"type": "integer", "enum": [1, 2, 3, 4]},
               "kapsam": {"type": "string", "enum": ["eksikler", "tumu", "tek_sahne"],
                          "description": "eksikler: üretilmemiş ve güncel olmayanlar. tumu: hepsi için yeni sürüm. tek_sahne: yalnızca sahne_no."},
               "sahne_no": INT, "dil": LANG_SCOPE}, ["adim", "kapsam"]),
        _tool("isleri_bekle", "Çalışan işler bitene kadar bekler ve sonucu döndürür: biten, hata veren, elle bekleyen işler ve onay bekleyen varlıklar.",
              {"en_fazla_saniye": {"type": "integer", "minimum": 5, "maximum": 3600}}),
        _tool("baglanti_durumu", "Kullanıcının hangi servisleri bağladığını gösterir: bağlı, ortak anahtar ya da deneme modu (mock). "
              "Üretime başlamadan önce bir kez çağırın; deneme modundaki servislerin etkilediği adımları kullanıcıya söyleyin."),
        _tool("durum_getir", "Bölüm hattının özeti: dil başına adım durumu, onay bekleyen varlıklar, süren ve hatalı işler, sahneler, maliyet."),
        _tool("varlik_onayla", "Varlıkları onaylar; onaylanan sürüm o sahne ve dil için seçili olur.",
              {"varlik_idleri": {"type": "array", "items": INT, "minItems": 1}}, ["varlik_idleri"]),
        _tool("varlik_reddet", "Varlığı reddeder (silinmez; seçili sürüm bir önceki onaylıya döner).",
              {"varlik_id": INT, "neden": STR}, ["varlik_id"]),
        _tool("varlik_incele", "Varlığı gösterir: görseli doğrudan, videoyu birkaç kare olarak. Ses için yalnızca süre ve dil bilgisi döner.",
              {"varlik_id": INT}, ["varlik_id"]),
        _tool("kurgu_yap", "5. adım: sahneleri birleştirir (geçiş, müzik, -14 LUFS, altyazı). Her dil için ayrı çıktı.", {"dil": LANG_SCOPE}),
        _tool("kontrol_calistir", "6. adım: kalite raporunu üretir (dil başına).", {"dil": LANG_SCOPE}),
        _tool("kontrol_raporu_getir", "Bir dilin kalite raporunu getirir: sahneye bağlı, zaman kodlu bulgular ve önerilen düzeltme.",
              {"dil": LANG, "bilgi_notlari": {"type": "boolean", "description": "true: 'bilgi' düzeyindeki notları da getirir."}}, ["dil"]),
        _tool("duzeltme_baslat", "7. adım: sahneyi düzeltir. omni: videoya yazılı talimat. dudak_senkronu: konuşan sahnede ağız düzeltmesi. "
              "ses_yeniden: sesi yeniden üret. sahne_yeniden: ses, görsel ve videoyu baştan üret.",
              {"sahne_no": INT, "yontem": {"type": "string", "enum": ["omni", "dudak_senkronu", "ses_yeniden", "sahne_yeniden"]},
               "talimat": {"type": "string", "description": "omni için zorunlu: neyin nasıl değişeceği."}, "dil": LANG},
              ["sahne_no", "yontem"]),
        _tool("maliyet_getir", "Bu çalışmanın ve bölümün maliyeti: sağlayıcı işleri, Claude API, dil bazında toplam, kalan bütçe."),
        _tool("kullaniciya_sor", "Kullanıcıya soru sorar ve yanıtını bekler. Yalnızca onsuz ilerlenemiyorsa kullanın.",
              {"soru": STR, "secenekler": {"type": "array", "items": STR}}, ["soru"]),
        _tool("plan_onayi_iste", "Sahne listesini ve tahmini maliyeti kullanıcıya gösterir, tek bir onay bekler. "
              "Yalnızca bir araç 'plan onayı gerekli' dediğinde çağırın.", {"ozet": {"type": "string", "description": "Planın kısa özeti."}}, ["ozet"]),
        _tool("kurgu_ayarla", "Kurgu ayarlarını değiştirir.",
              {"altyazi": BOOL, "altyaziyi_gom": BOOL, "dikey": {"type": "boolean", "description": "Dikey 9:16 çıktı da üret."},
               "gecis": {"type": "string", "enum": ["fade", "xfade", "cut"]}, "muzik_seviyesi": {"type": "number", "minimum": 0, "maximum": 1}}),
        _tool("ek_kullan", "Kullanıcının sohbete eklediği dosyayı kullanır: karakter referans görseli, müzik ya da efekt.",
              {"ek_id": INT, "amac": {"type": "string", "enum": ["karakter_referansi", "muzik", "efekt"]},
               "karakter_id": INT, "saniye": {"type": "number", "description": "efekt için: bölümde çalacağı saniye."}}, ["ek_id", "amac"]),
        _tool("sahne_isaretle", "Sahneyi 'elle bakılmalı' olarak işaretler (düzeltilemeyen sorun için).",
              {"sahne_no": INT, "not": STR}, ["sahne_no", "not"]),
    ]


class ToolError(Exception):
    """Ajana hata olarak dönen (is_error) iş kuralı ihlali."""


@dataclass
class ToolResult:
    content: Any                       # str ya da içerik blokları listesi
    is_error: bool = False
    media: list[dict] = field(default_factory=list)   # sohbette oynatılacak varlıklar (Claude'a gitmez)
    special: str = ""                  # "ask": kullanıcı yanıtı beklenir · "budget": bütçe sınırı
    payload: dict = field(default_factory=dict)


def _json(data: Any, limit: int = 9000) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + ' …(kısaltıldı)"'


def _media(a: Asset, label: str = "") -> dict:
    return {"asset_id": a.id, "kind": a.kind, "url": f"/media/{a.path}", "role": a.role, "language": a.language,
            "version": a.version, "label": label}


class Ctx:
    """Araç bağlamı: sohbetin projesi ve bölümü. Başka projeye erişim reddedilir."""

    def __init__(self, s, conv_id: int, run_id: int | None):
        self.s = s
        self.conv: Conversation = s.get(Conversation, conv_id)
        # kullanıcı kimliği araç girdisinden değil, sunucudaki sohbet kaydından gelir
        if self.conv is None or self.conv.user_id != tenancy.uid():
            raise ToolError("Bu sohbet geçerli kullanıcıya ait değil")
        self.run: AgentRun | None = s.get(AgentRun, run_id) if run_id else None

    def project(self) -> Project:
        p = self.s.get(Project, self.conv.project_id) if self.conv.project_id else None
        if p is None:
            raise ToolError("Bu sohbete bağlı proje yok. Önce proje_olustur.")
        return p

    def episode(self) -> Episode:
        ep = self.s.get(Episode, self.conv.episode_id) if self.conv.episode_id else None
        if ep is None:
            raise ToolError("Bu sohbete bağlı bölüm yok. Önce bolum_olustur.")
        return ep

    def scene(self, no: int) -> Scene:
        try:
            return services.scene_by_no(self.episode(), int(no))
        except services.ServiceError as e:
            raise ToolError(str(e))

    def character(self, cid: int) -> Character:
        c = self.s.get(Character, cid)
        if c is None or c.project_id != self.conv.project_id:
            raise ToolError(f"Karakter {cid} bu projede yok")
        return c

    def asset(self, aid: int) -> Asset:
        a = self.s.get(Asset, aid)
        if a is None or a.project_id != self.conv.project_id:
            raise ToolError(f"Varlık {aid} bu projede yok")
        return a

    def state(self) -> dict:
        return dict(self.run.state or {}) if self.run else {}

    def save_state(self, st: dict) -> None:
        if self.run:
            self.run.state = st

    def lang(self, value: str | None) -> str | None:
        return value if value not in (None, "") else "*"

    # ---- kodda zorlanan sınırlar
    def require_plan(self) -> None:
        if self.run and store.get("plan_approval") and not self.run.plan_approved:
            raise ToolError("Plan onayı gerekli: üretimden önce plan_onayi_iste aracını çağırın.")

    def count_retry(self, sc: Scene) -> None:
        limit = int(get_models()["claude"].get("max_scene_retries", 3))
        st = self.state()
        retries = dict(st.get("retries") or {})
        n = int(retries.get(str(sc.order_no), 0)) + 1
        if n > limit:
            with session_scope() as s2:  # hata oturumu geri alır; işaret ayrı oturumda kalıcı yazılır
                row = s2.get(Scene, sc.id)
                row.needs_review = True
                row.review_note = row.review_note or f"Ajan {limit} denemede düzeltemedi"
            raise ToolError(f"Sahne {sc.order_no} için {limit} yeniden deneme hakkı doldu; sahne 'elle bakılmalı' olarak "
                            "işaretlendi. Eldeki en iyi sürümü onaylayıp devam edin.")
        retries[str(sc.order_no)] = n
        st["retries"] = retries
        self.save_state(st)


# ------------------------------------------------------------------ araç işleyicileri
def t_proje_olustur(c: Ctx, i: dict):
    if c.conv.project_id:
        p = c.project()
        return {"proje_id": p.id, "ad": p.name, "not": "Bu sohbete zaten bir proje bağlı; yenisi oluşturulmadı."}
    p = services.create_project(c.s, i["ad"], i.get("aciklama", ""), i.get("gorsel_uslup", ""), i["dil"])
    c.conv.project_id = p.id
    if c.conv.title in ("", "Yeni sohbet"):
        c.conv.title = p.name
    return {"proje_id": p.id, "ad": p.name, "dil": p.default_language}


def t_bolum_olustur(c: Ctx, i: dict):
    p = c.project()
    ep = services.create_episode(c.s, p, title=i.get("baslik"), language=p.default_language, langs=i.get("diller") or [])
    c.conv.episode_id = ep.id
    langs = pipeline.ep_langs(c.s, ep)
    return {"bolum_id": ep.id, "baslik": ep.title, "diller": langs,
            "dil_uyarilari": {l: w for l in langs if (w := pipeline.language_warnings(c.s, ep, l))}}


def t_karakter_olustur(c: Ctx, i: dict):
    ch = services.create_character(c.s, c.project(), i["ad"], i.get("aciklama", ""), bool(i.get("anlatici")))
    return {"karakter_id": ch.id, "ad": ch.name, "anlatici": ch.is_narrator}


def t_karakter_guncelle(c: Ctx, i: dict):
    ch = services.update_character(c.s, c.character(i["karakter_id"]),
                                   {"name": i.get("ad"), "description": i.get("aciklama")})
    return {"karakter_id": ch.id, "ad": ch.name}


async def t_ses_listele(conv_id: int, run_id: int | None, i: dict) -> ToolResult:
    try:
        res = await services.list_voices(i.get("dil"))
    except Exception as e:
        raise ToolError(f"Ses listesi alınamadı: {e}")
    voices = [{"ses_kimligi": v.get("voice_id"), "ad": v.get("name"), **({"etiketler": v["labels"]} if v.get("labels") else {}),
               **({"diller": v["languages"]} if v.get("languages") else {})} for v in res["voices"][:60]]
    return ToolResult(_json({"mod": res["mode"], "sesler": voices}))


def t_karaktere_ses_ata(c: Ctx, i: dict):
    ch = services.assign_voice(c.s, c.character(i["karakter_id"]), i["ses_kimligi"], i.get("dil"))
    return {"karakter_id": ch.id, "ad": ch.name, "varsayilan_ses": ch.voice_id, "dil_basina": ch.voices or {}}


def t_sahneleri_yaz(c: Ctx, i: dict):
    ep = c.episode()
    rows = []
    for sc in i["sahneler"]:
        row = {"order_no": sc["sahne_no"], "type": sc.get("tur"), "origin": "agent"}
        for src, dst in (("karakterler", "characters"), ("replik", "dialogue"), ("dil", "language"),
                         ("gorsel_prompt", "image_prompt"), ("video_prompt", "video_prompt"), ("sure", "duration"),
                         ("sesli_an", "audio_moment"), ("gorseldeki_metin", "image_text"), ("not", "notes")):
            if src in sc:
                row[dst] = sc[src]
        rows.append(row)
    res = services.write_scenes(c.s, ep, rows, bool(i.get("hepsini_degistir")))
    return {"yazilan": len(res["scenes"]), "toplam_sahne": len(ep.scenes), "yeni_karakterler": res["new_characters"],
            **({"dil_onerileri": res["language_suggestions"]} if res["language_suggestions"] else {})}


def t_dil_ekle(c: Ctx, i: dict):
    res = services.add_language(c.s, c.episode(), i["dil"])
    return {"diller": res["languages"], "uyarilar": res["warnings"],
            "sonraki": "Replikleri bu dile çevirip replik_cevir ile yazın; sonra adim_calistir (eksikler)."}


def t_replik_cevir(c: Ctx, i: dict):
    ep = c.episode()
    res = services.set_translations(c.s, ep, i["dil"], [{"scene_no": x["sahne_no"], "text": x["metin"]} for x in i["ceviriler"]],
                                    approve=True, origin="agent")
    src = pipeline.source_lang(c.s, ep)
    diff = []
    for x in i["ceviriler"]:
        base = len(pipeline.text_of(c.s, services.scene_by_no(ep, x["sahne_no"]), src)) or 1
        ratio = len(x["metin"]) / base
        if ratio > 1.35 or ratio < 0.65:
            diff.append({"sahne_no": x["sahne_no"], "oran": round(ratio, 2)})
    return {"dil": res["language"], "yazilan": res["written"], "onayli": True,
            **({"uzunluk_farki": diff, "not": "Bu sahnelerin süresi bu dilin sesine göre ayarlanacak."} if diff else {})}


def t_adim_calistir(c: Ctx, i: dict):
    c.require_plan()
    ep = c.episode()
    mode = {"eksikler": "missing", "tumu": "all", "tek_sahne": "all"}[i["kapsam"]]
    scene_id = None
    if i["kapsam"] == "tek_sahne":
        if "sahne_no" not in i:
            raise ToolError("tek_sahne kapsamı için sahne_no gerekli")
        sc = c.scene(i["sahne_no"])
        scene_id = sc.id
        role = {1: "voice", 2: "scene_image", 3: "scene_video", 4: "scene_video"}[i["adim"]]
        if c.s.scalars(select(Asset.id).where(Asset.scene_id == sc.id, Asset.role == role)).first():
            c.count_retry(sc)  # mevcut sürümü olan sahneyi yeniden üretmek bir denemedir
    res = services.run_step(c.s, ep, int(i["adim"]), mode, scene_id, c.lang(i.get("dil")))
    return {"baslatilan_is": len(res["created"]), "atlanan": res["skipped"],
            "sonraki": "isleri_bekle" if res["created"] else "Yeni iş yok; atlananların nedenine bakın."}


def t_durum_getir(c: Ctx, i: dict):
    if not c.conv.project_id:
        return {"proje": None, "not": "Bu sohbete henüz proje bağlı değil."}
    if not c.conv.episode_id:
        p = c.project()
        return {"proje": {"id": p.id, "ad": p.name}, "bolum": None,
                "karakterler": [{"id": ch.id, "ad": ch.name, "anlatici": ch.is_narrator} for ch in p.characters]}
    return services.episode_summary(c.s, c.episode())


def t_baglanti_durumu(c: Ctx, i: dict):
    summ = connections.summary(c.conv.user_id)
    step_names = {1: "sesler", 2: "görseller", 3: "konuşan sahneler", 4: "konuşmasız sahneler", 7: "düzeltme"}
    rows = [{"servis": x["label"], "durum": {"connected": "bağlı", "shared": "ortak (kurum) anahtarıyla bağlı", "demo": "deneme modu (mock: gerçek üretim yok)"}[x["mode"]],
             **({"sorun": "son test başarısız"} if x["status"] == "error" else {}),
             "etkiledigi_adimlar": [step_names[n] for n in x["steps"]]} for x in summ["services"] if x["service"] != "anthropic"]
    return {"servisler": rows, "deneme_modundaki_adimlar": [step_names[n] for n in summ["demo_steps"]],
            "kurulum_sayfasi": "/ayarlar/baglantilar",
            "not": "Deneme modundaki adımlar sahte (ffmpeg) çıktı üretir. Kullanıcıya bunu baştan söyleyin ve kurulum sayfasının bağlantısını verin."
                   if summ["demo_steps"] else "Tüm üretim servisleri bağlı."}


def t_varlik_onayla(c: Ctx, i: dict):
    out, mediax = [], []
    for aid in i["varlik_idleri"]:
        a = c.asset(aid)
        pipeline.approve(c.s, a, "agent")
        out.append({"varlik_id": a.id, "rol": a.role, "dil": a.language or None, "surum": a.version})
        mediax.append(_media(a, "Onaylandı"))
    return ToolResult(_json({"onaylanan": out}), media=mediax[:8])


def t_varlik_reddet(c: Ctx, i: dict):
    a = c.asset(i["varlik_id"])
    pipeline.reject(c.s, a)
    if i.get("neden"):
        a.meta = {**(a.meta or {}), "reject_reason": i["neden"][:300]}
    return {"reddedilen": a.id, "rol": a.role}


def t_kurgu_yap(c: Ctx, i: dict):
    c.require_plan()
    ids = services.start_montage(c.s, c.episode(), c.lang(i.get("dil")))
    return {"baslatilan_is": len(ids), "sonraki": "isleri_bekle"}


def t_kontrol_calistir(c: Ctx, i: dict):
    ep = c.episode()
    scope = services.lang_scope(c.s, ep, c.lang(i.get("dil")))
    st = c.state()
    rounds = dict(st.get("qc_rounds") or {})
    for l in (pipeline.ep_langs(c.s, ep) if scope == "*" else [scope]):
        rounds[l] = int(rounds.get(l, 0)) + 1
    st["qc_rounds"] = rounds
    c.save_state(st)
    ids = services.start_qc(c.s, ep, scope)
    return {"baslatilan_is": len(ids), "sonraki": "isleri_bekle, sonra kontrol_raporu_getir"}


def t_kontrol_raporu_getir(c: Ctx, i: dict):
    ep = c.episode()
    lang = services.check_lang(i["dil"])
    rep = pipeline.latest_report(c.s, ep.id, lang)
    if rep is None:
        raise ToolError(f"{lang} için kalite raporu yok; önce kontrol_calistir.")
    final = pipeline.latest_final(c.s, ep.id, lang)
    keep = ("severity", "code", "message", "scene_no", "timecode", "suggest", "offset_ms")
    findings = [{k: f[k] for k in keep if f.get(k) not in (None, "")} for f in rep.findings
                if i.get("bilgi_notlari") or f["severity"] != "info"]
    return {"dil": lang, "ozet": {k: rep.summary.get(k) for k in ("verdict", "errors", "warnings", "infos", "duration", "loudness")},
            "guncel_kurguya_ait": bool(final and rep.final_asset_id == final.id), "bulgular": findings[:40],
            "onem_aciklamasi": "error/warn düzeltilmeli; info bilgi notudur"}


def t_duzeltme_baslat(c: Ctx, i: dict):
    c.require_plan()
    ep = c.episode()
    sc = c.scene(i["sahne_no"])
    limit = int(get_models()["claude"].get("max_fix_rounds", 2))
    rounds = (c.state().get("qc_rounds") or {})
    if rounds and max(rounds.values()) > limit:
        raise ToolError(f"En fazla {limit} düzeltme turu yapılabilir; kalan bulguları son raporunuzda listeleyin.")
    c.count_retry(sc)
    method = {"omni": "omni", "dudak_senkronu": "lipsync", "ses_yeniden": "regen_audio", "sahne_yeniden": "regen_scene"}[i["yontem"]]
    lang = i.get("dil") or pipeline.source_lang(c.s, ep)
    ids = services.fix(c.s, ep, sc, method, i.get("talimat", ""), lang)
    jobs = [c.s.get(Job, j) for j in ids]
    manual = [j.id for j in jobs if j.mode == "manual"]
    return {"baslatilan_is": len(ids), "elle_adim": bool(manual), "sonraki": "isleri_bekle; çıktıyı inceleyip onaylayın, "
            "sonra o dilin kurgusunu ve kontrolünü yeniden çalıştırın."}


def t_maliyet_getir(c: Ctx, i: dict):
    out: dict = {}
    if c.run:
        spent = pipeline.run_spent(c.s, c.run.id)
        out["bu_calisma"] = {"harcanan_usd": round(spent, 3), "claude_usd": round(c.run.claude_usd, 3),
                             "butce_usd": c.run.budget_usd, "kalan_usd": round(max(c.run.budget_usd - spent, 0), 3)}
    if c.conv.episode_id:
        ep = c.episode()
        out["bolum"] = services.cost_of(c.s, episode_id=ep.id)
        out["dil_bazinda"] = [{"dil": x["language"] or "ortak", "gercek_usd": x["real_usd"], "mock_karsiligi_usd": x["mock_usd"]}
                              for x in services.cost_by_language(c.s, ep)]
        out["not"] = "mock işlerde ücret kesilmez; 'mock karşılığı' gerçek modda yaklaşık tutardır"
    return out


def t_kurgu_ayarla(c: Ctx, i: dict):
    patch = {dst: i[src] for src, dst in (("altyazi", "subtitles"), ("altyaziyi_gom", "burn_subtitles"), ("dikey", "vertical"),
                                          ("gecis", "transition"), ("muzik_seviyesi", "music_volume")) if src in i}
    return {"ayarlar": services.update_episode_settings(c.s, c.episode(), patch)}


def t_sahne_isaretle(c: Ctx, i: dict):
    sc = c.scene(i["sahne_no"])
    sc.needs_review, sc.review_note = True, i["not"][:500]
    return {"sahne_no": sc.order_no, "elle_bakilmali": True}


def t_ek_kullan(c: Ctx, i: dict):
    m = c.s.get(ChatMessage, i["ek_id"])
    if m is None or m.conversation_id != c.conv.id or m.kind != "attachment":
        raise ToolError(f"Ek {i['ek_id']} bu sohbette yok")
    f = (m.content or [{}])[0]
    src = storage.abs_path(f["path"])
    if i["amac"] == "karakter_referansi":
        if f["kind"] != "image":
            raise ToolError("Karakter referansı görsel olmalı")
        ch = c.character(i.get("karakter_id") or 0)
        v = storage.next_version(c.s, role="character_ref", character_id=ch.id)
        dest = storage.target_path(c.s, role="character_ref", version=v, ext=src.suffix, character=ch)
        shutil.copyfile(src, dest)
        a = Asset(user_id=c.conv.user_id, project_id=ch.project_id, character_id=ch.id, kind="image", role="character_ref",
                  path=storage.rel_path(dest), version=v, step=2, provider="upload", mode="manual", status="approved",
                  approved_by="user", meta=media.validate(dest, "image"))
    else:
        if f["kind"] != "audio":
            raise ToolError("Müzik ve efekt ses dosyası olmalı")
        ep = c.episode()
        role = "music" if i["amac"] == "muzik" else "sfx"
        v = storage.next_version(c.s, role=role, episode_id=ep.id)
        dest = storage.episode_dir(c.s, ep) / "ortak" / "yuklemeler" / f"{role}_v{v}{src.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        a = Asset(user_id=c.conv.user_id, project_id=ep.project_id, episode_id=ep.id, kind="audio", role=role, path=storage.rel_path(dest),
                  version=v, step=5, provider="upload", mode="manual", status="approved", approved_by="user",
                  meta={**media.validate(dest, "audio"), "name": f.get("name", "")},
                  params={"at": float(i.get("saniye") or 0), "volume": 1.0})
    c.s.add(a)
    c.s.flush()
    storage.publish(dest)
    return {"varlik_id": a.id, "rol": a.role}


def t_kullaniciya_sor(c: Ctx, i: dict):
    return ToolResult("", special="ask", payload={"kind": "question", "question": i["soru"], "options": i.get("secenekler") or []})


def t_plan_onayi_iste(c: Ctx, i: dict):
    ep = c.episode()
    est = services.estimate_plan(c.s, ep)
    scenes = [{"no": sc.order_no, "type": services.TYPE_TR[sc.type], "text": pipeline.text_of(c.s, sc, pipeline.source_lang(c.s, ep))[:120]}
              for sc in ep.scenes]
    return ToolResult("", special="ask", payload={"kind": "plan", "question": i["ozet"], "estimate": est, "scenes": scenes,
                                                   "options": ["Onaylıyorum, başla", "Vazgeç"]})


SYNC = {"baglanti_durumu": t_baglanti_durumu, "proje_olustur": t_proje_olustur, "bolum_olustur": t_bolum_olustur, "karakter_olustur": t_karakter_olustur,
        "karakter_guncelle": t_karakter_guncelle, "karaktere_ses_ata": t_karaktere_ses_ata, "sahneleri_yaz": t_sahneleri_yaz,
        "dil_ekle": t_dil_ekle, "replik_cevir": t_replik_cevir, "adim_calistir": t_adim_calistir, "durum_getir": t_durum_getir,
        "varlik_onayla": t_varlik_onayla, "varlik_reddet": t_varlik_reddet, "kurgu_yap": t_kurgu_yap,
        "kontrol_calistir": t_kontrol_calistir, "kontrol_raporu_getir": t_kontrol_raporu_getir,
        "duzeltme_baslat": t_duzeltme_baslat, "maliyet_getir": t_maliyet_getir, "kurgu_ayarla": t_kurgu_ayarla,
        "sahne_isaretle": t_sahne_isaretle, "ek_kullan": t_ek_kullan, "kullaniciya_sor": t_kullaniciya_sor,
        "plan_onayi_iste": t_plan_onayi_iste}
ASYNC = ("ses_listele", "isleri_bekle", "varlik_incele")
NAMES = tuple(d["name"] for d in definitions())


def _run_sync(conv_id: int, run_id: int | None, name: str, inp: dict) -> ToolResult:
    tok_a, tok_r = pipeline.ACTOR.set("agent"), pipeline.RUN_ID.set(run_id)
    try:
        with session_scope() as s:
            res = SYNC[name](Ctx(s, conv_id, run_id), inp)
            pipeline.emit(s, "refresh", conversation_id=conv_id)
            return res if isinstance(res, ToolResult) else ToolResult(_json(res))
    finally:
        pipeline.ACTOR.reset(tok_a)
        pipeline.RUN_ID.reset(tok_r)


async def _varlik_incele(conv_id: int, run_id: int | None, inp: dict) -> ToolResult:
    def work() -> ToolResult:
        with session_scope() as s:
            c = Ctx(s, conv_id, run_id)
            a = c.asset(inp["varlik_id"])
            data = services.inspect_asset(s, a)
            blocks: list[dict] = [{"type": "text", "text": _json(data["info"])}]
            for img in data["images"]:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                           "data": base64.standard_b64encode(img).decode()}})
            return ToolResult(blocks, media=[_media(a, "İncelendi")])
    return await asyncio.to_thread(work)


async def _isleri_bekle(conv_id: int, run_id: int | None, inp: dict) -> ToolResult:
    """Bloklamadan bekler: olay döngüsü serbest kalır, durum veritabanından izlenir."""
    limit = float(inp.get("en_fazla_saniye") or 1800)
    t0, started = time.monotonic(), now()
    with session_scope() as s:
        c = Ctx(s, conv_id, run_id)
        if not c.conv.project_id:
            raise ToolError("Bu sohbete bağlı proje yok")
        project_id, ep_id = c.conv.project_id, c.conv.episode_id
        if c.run and c.run.status == "running":
            c.run.status = "waiting_jobs"
            pipeline.emit(s, "chat", conversation_id=conv_id, event="status")
    timed_out, cancelled = False, False
    try:
        while True:
            with session_scope() as s:
                run = s.get(AgentRun, run_id) if run_id else None
                if run is not None and run.status == "stopped":
                    raise asyncio.CancelledError()
                q = select(Job.status).where(Job.project_id == project_id, Job.kind != "claude",
                                             Job.status.in_(("queued", "submitting", "running", "blocked")))
                states = set(s.scalars(q).all())
                busy = bool(states)
                blocked = "blocked" in states
                if run is not None and blocked and run.status == "waiting_jobs":
                    # bir bağlantı yenilenmeli: kullanıcı düzeltince işler ve bu bekleme kendiliğinden sürer
                    run.status = "waiting_connection"
                    run.state = {**(run.state or {}), "waiting_service": None}
                    pipeline.emit(s, "chat", conversation_id=conv_id, event="status")
                elif run is not None and not blocked and run.status == "waiting_connection":
                    run.status = "waiting_jobs"
                    pipeline.emit(s, "chat", conversation_id=conv_id, event="status")
            if not busy:
                break
            if blocked:
                await asyncio.sleep(0.6)
                continue
            if time.monotonic() - t0 > limit:
                timed_out = True
                break
            await asyncio.sleep(0.4)
    except asyncio.CancelledError:
        cancelled = True  # durdurma: durumu "durduruldu" yazan istekle yarışmamak için dokunma
        raise
    finally:
        if not cancelled:
            with session_scope() as s:
                run = s.get(AgentRun, run_id) if run_id else None
                if run is not None and run.status in ("waiting_jobs", "waiting_connection"):
                    run.status = "running"
                    pipeline.emit(s, "chat", conversation_id=conv_id, event="status")
    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.project_id == project_id, Job.kind != "claude",
                                           Job.finished_at >= started).order_by(Job.id)).all()
        scene_no = {sc.id: sc.order_no for sc in (s.get(Episode, ep_id).scenes if ep_id else [])}
        done, failed, mediax = [], [], []
        for j in jobs:
            row = {"is": services.KIND_TITLES.get(j.kind, j.kind), "sahne_no": scene_no.get(j.scene_id), "dil": j.language or None}
            if j.status == "succeeded":
                a = s.get(Asset, j.result_asset_id) if j.result_asset_id else None
                if a is not None:
                    row.update(varlik_id=a.id, durum={"draft": "onay bekliyor", "approved": "onaylı"}.get(a.status, a.status))
                    if a.kind in ("audio", "image", "video"):
                        mediax.append(_media(a, row["is"] + (f" · Sahne {row['sahne_no']}" if row["sahne_no"] else "")))
                done.append(row)
            elif j.status == "failed":
                failed.append({**row, "hata": j.error[:300]})
        manual = s.scalars(select(Job).where(Job.project_id == project_id, Job.status == "waiting_manual")).all()
        out = {"biten": done[:60], "hatali": failed,
               "elle_bekleyen": [{"is_id": j.id, "is": services.KIND_TITLES.get(j.kind, j.kind), "sahne_no": scene_no.get(j.scene_id),
                                  "not": "Kullanıcı İşler ekranından girdileri indirip sonucu yüklemeli."} for j in manual],
               **({"zaman_asimi": "Bazı işler hâlâ sürüyor; yeniden isleri_bekle çağırın."} if timed_out else {})}
        if ep_id:
            summ = services.episode_summary(s, s.get(Episode, ep_id))
            out["onay_bekleyen"] = summ["pending_approval"]
    return ToolResult(_json(out), media=mediax[:12])


def validate(name: str, inp: Any) -> str | None:
    """Araç girdisini şemasına göre doğrular (akışta gelen girdiler sunucuda doğrulanmaz)."""
    if name not in NAMES:
        return f"Bilinmeyen araç: {name}"
    if not isinstance(inp, dict):
        return "Araç girdisi bir JSON nesnesi olmalı"
    schema = next(d["input_schema"] for d in definitions() if d["name"] == name)
    try:
        jsonschema.validate(inp, schema)
    except jsonschema.ValidationError as e:
        return f"Geçersiz girdi: {e.message[:300]} (alan: {'/'.join(str(p) for p in e.absolute_path) or 'kök'})"
    return None


async def execute(conv_id: int, run_id: int | None, name: str, inp: Any) -> ToolResult:
    """Tek bir araç çağrısını çalıştırır. Hatalar ajana is_error sonucu olarak döner."""
    err = validate(name, inp)
    if err:
        return ToolResult(err, is_error=True)
    # araç, sohbetin sahibi olan kullanıcının bağlamında çalışır (kimlik sunucudaki kayıttan alınır)
    with session_scope() as s:
        conv = s.get(Conversation, conv_id)
        owner = conv.user_id if conv else None
    if owner is None:
        return ToolResult("Sohbet bulunamadı", is_error=True)
    tok = tenancy.USER_ID.set(owner)
    try:
        return await _execute(conv_id, run_id, name, inp)
    finally:
        tenancy.USER_ID.reset(tok)


async def _execute(conv_id: int, run_id: int | None, name: str, inp: Any) -> ToolResult:
    try:
        if name == "ses_listele":
            return await t_ses_listele(conv_id, run_id, inp)
        if name == "isleri_bekle":
            return await _isleri_bekle(conv_id, run_id, inp)
        if name == "varlik_incele":
            return await _varlik_incele(conv_id, run_id, inp)
        return await asyncio.to_thread(_run_sync, conv_id, run_id, name, inp)
    except pipeline.BudgetExceeded as e:
        return ToolResult(f"Bütçe sınırı: {e}. Çalışma durduruldu; kullanıcı bütçeyi artırırsa devam edilir.",
                          is_error=True, special="budget")
    except (ToolError, services.ServiceError, pipeline.StepBlocked, media.MediaError) as e:
        return ToolResult(str(e), is_error=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # beklenmeyen hata ajanı düşürmesin
        return ToolResult(f"Araç hatası ({type(e).__name__}): {e}", is_error=True)
