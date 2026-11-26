"""Uçtan uca test (çok dilli): mock modda örnek bölüm iki dilde 1'den 6'ya çalışır; dil başına
oynatılabilir mp4, altyazı ve kalite raporu çıkar; konuşmasız sahneler ikinci dilde yeniden üretilmez."""
import asyncio

import httpx
import pytest

from app import connections, media, pipeline, storage
from app.db import session_scope
from app.main import app
from app.models import Asset, Episode, Job, Scene


async def wait_for(fn, timeout: float = 900, every: float = 0.4):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while True:
        v = await fn()
        if v:
            return v
        if loop.time() > end:
            raise AssertionError("zaman aşımı")
        await asyncio.sleep(every)


async def idle(c: httpx.AsyncClient, eid: int, lang: str = "tr"):
    async def check():
        ep = (await c.get(f"/api/episodes/{eid}", params={"lang": lang})).json()
        return ep if not ep["active_jobs"] else None
    return await wait_for(check)


def path_of(url: str):
    return storage.abs_path(url.removeprefix("/media/"))


async def test_seed_episode_runs_in_two_languages(client):
    c = client
    proj = (await c.post("/api/seed")).json()
    assert proj["default_language"] == "tr"
    assert len([ch for ch in proj["characters"] if not ch["is_narrator"]]) == 2
    eid = proj["episodes"][0]["id"]
    ep = (await c.get(f"/api/episodes/{eid}")).json()
    assert ep["source_language"] == "tr" and ep["languages"] == ["tr", "en"]
    assert len(ep["scenes"]) == 5 and {s["type"] for s in ep["scenes"]} == {"talking", "silent", "narrator"}
    # replikler dil başına ayrı alanlarda
    talking = next(s for s in ep["scenes"] if s["type"] == "talking")
    assert set(talking["texts"]) == {"tr", "en"} and talking["texts"]["en"]["text"].startswith("Deniz, did you")
    assert talking["texts"]["tr"]["detected"] == "tr" and talking["texts"]["en"]["detected"] == "en"

    # bağımlılık kuralı: o dilin sesi ve görsel onaysızken HeyGen adımı hiçbir iş başlatmaz
    r = (await c.post(f"/api/episodes/{eid}/steps/3/run", json={"mode": "all", "lang": "en"})).json()
    assert r["created"] == [] and len(r["skipped"]) == 2
    assert (await c.post(f"/api/episodes/{eid}/steps/5/run", json={"lang": "tr"})).status_code == 409

    # otomatik mod: iki dilde 1 -> 6
    assert (await c.post(f"/api/episodes/{eid}/auto", json={"on": True})).json()["auto_state"] == "running"

    async def finished():
        e = (await c.get(f"/api/episodes/{eid}")).json()
        return e if e["auto_state"] in ("done", "error") else None

    done = await wait_for(finished)
    assert done["auto_state"] == "done", done["auto_message"]

    views = {l: (await c.get(f"/api/episodes/{eid}", params={"lang": l})).json() for l in ("tr", "en")}
    for lang, ep in views.items():
        assert all(s["complete"] for s in ep["steps"] if s["n"] <= 6), (lang, ep["steps"])
        # dil başına oynatılabilir 1080p mp4
        info = media.validate(path_of(ep["final"]["url"]), "video")
        assert (info["width"], info["height"], info["fps"]) == (1920, 1080, 30) and info["has_audio"] and info["duration"] > 20
        assert ep["final"]["language"] == lang and f"/{lang}/kurgu/" in ep["final"]["url"]
        media.ffmpeg(["-v", "error", "-i", path_of(ep["final"]["url"]), "-f", "null", "-"])
        # dil başına altyazı ve kalite raporu
        srt = path_of(ep["subtitles"]["url"]).read_text(encoding="utf-8")
        assert "-->" in srt and ("fener" in srt.lower() if lang == "tr" else "lighthouse" in srt.lower())
        rep = ep["report"]
        assert rep["summary"]["language"] == lang and rep["final_asset_id"] == ep["final"]["id"]
        assert rep["summary"]["errors"] == 0, rep["findings"]
        assert abs(rep["summary"]["loudness"]["integrated"] - (-14)) <= 1.5
        assert not [f for f in rep["findings"] if f["code"] == "dil_uyumu"]
        # dudak senkronu konuşan sahnelerde ölçüldü ve eşiğin altında
        assert set(rep["summary"]["lipsync"]) == {"2", "4"}
        for m in rep["summary"]["lipsync"].values():
            assert m["measurable"] and m["confidence"] > 0.3 and abs(m["offset_ms"]) <= 120, m
        scoped = [f for f in rep["findings"] if f["scene_id"]]
        assert scoped and all(f["timecode"] and f["scene_no"] for f in scoped)

    tr, en = views["tr"], views["en"]
    assert (await c.get(tr["final"]["url"], headers={"Range": "bytes=0-99"})).status_code == 206
    for a, b in zip(tr["scenes"], en["scenes"]):
        assert a["image_id"] == b["image_id"], "sahne görselleri diller arasında ortak"
        if a["type"] == "talking":
            assert a["video_id"] != b["video_id"] and a["audio_id"] != b["audio_id"]
        else:
            assert a["video_id"] == b["video_id"], "konuşmasız/anlatıcı videosu ikinci dilde yeniden üretilmez"
        if a["type"] != "silent":
            assert a["audio_id"] != b["audio_id"]

    with session_scope() as s:
        kinds: dict = {}
        for j in s.query(Job).filter(Job.episode_id == eid, Job.status == "succeeded"):
            kinds[(j.kind, j.language)] = kinds.get((j.kind, j.language), 0) + 1
        # ortak varlıklar bir kez; dile bağlı olanlar dil başına
        assert kinds[("silent_video", "")] == 3 and ("silent_video", "en") not in kinds and ("silent_video", "tr") not in kinds
        assert kinds[("scene_image", "")] == 5 and kinds[("character_image", "")] == 2
        assert kinds[("voice", "tr")] == 3 and kinds[("voice", "en")] == 3
        assert kinds[("talking_video", "tr")] == 2 and kinds[("talking_video", "en")] == 2
        assert kinds[("montage", "tr")] == 1 and kinds[("montage", "en")] == 1
        # konuşan video her zaman o dilin sesinden üretilir
        for v in s.query(Asset).filter(Asset.episode_id == eid, Asset.role == "scene_video", Asset.language != ""):
            audio = s.get(Asset, v.params["src"]["audio_id"])
            assert audio.language == v.language
        # konuşan sahne görsellerine dudak senkronu şartları otomatik eklenir
        ep_obj = s.get(Episode, eid)
        for sc in ep_obj.scenes:
            img = pipeline.sel(s, sc, "scene_image", "tr")
            assert ("lip sync" in img.prompt) == (sc.type == "talking")
        # kurgu dil doğrulaması: tr sahnesine en sesi seçilirse kurgu başlamaz
        sc = next(x for x in ep_obj.scenes if x.type == "talking")
        en_audio = pipeline.sel(s, sc, "voice", "en")
        tr_audio = pipeline.sel(s, sc, "voice", "tr")
        pipeline.set_sel(s, sc, "voice", "tr", en_audio.id)
        assert any("dilinde" in b for b in pipeline.montage_blockers(s, ep_obj, "tr"))
        pipeline.set_sel(s, sc, "voice", "tr", tr_audio.id)
        assert pipeline.montage_blockers(s, ep_obj, "tr") == []

    # maliyet: dil bazında toplam
    by_lang = {x["language"]: x for x in tr["cost_by_language"]}
    assert by_lang[""]["mock_usd"] > 0 and by_lang["tr"]["mock_usd"] > 0 and by_lang["en"]["mock_usd"] > 0

    # --- 7. adım: ortak (konuşmasız) sahnedeki düzeltme iki dilin kurgusunu da yeniler, yalnız o sahne işlenir
    target = tr["scenes"][2]
    r = await c.post(f"/api/scenes/{target['id']}/fix", json={"method": "omni", "instruction": "gökyüzünü turuncu yap", "lang": "tr"})
    assert r.status_code == 200, r.text
    ep_tr = await idle(c, eid, "tr")
    sc = next(s for s in ep_tr["scenes"] if s["id"] == target["id"])
    fix = next(a for a in sc["assets"] if a["role"] == "scene_video" and a["status"] == "draft")
    assert fix["version"] == 2 and fix["language"] == "" and sc["video_id"] == target["video_id"]
    await c.post(f"/api/assets/{fix['id']}/approve")

    async def refreshed():
        out = {}
        for l in ("tr", "en"):
            e = (await c.get(f"/api/episodes/{eid}", params={"lang": l})).json()
            if e["active_jobs"] or not e["report"] or e["report"]["id"] == views[l]["report"]["id"]:
                return None
            out[l] = e
        return out

    after = await wait_for(refreshed)
    assert after["tr"]["final"]["version"] == 2 and after["en"]["final"]["version"] == 2
    assert after["tr"]["final"]["meta"]["rebuilt_scenes"] == [target["order_no"]]
    assert after["en"]["final"]["meta"]["rebuilt_scenes"] == [], "ortak klip diğer dilin kurgusundan önbellekten gelir"
    versions = [a["version"] for a in next(s for s in after["tr"]["scenes"] if s["id"] == target["id"])["assets"]
                if a["role"] == "scene_video"]
    assert sorted(versions) == [1, 2], "eski sürüm silinmez"
    await c.post(f"/api/assets/{fix['id']}/reject")
    assert (await c.get(f"/api/scenes/{target['id']}")).json()["video_id"] == target["video_id"], "ret: önceki sürüme dönülür"

    # --- replik değişince o dilin sesi ve konuşan videosu "güncel değil" olur; diğer dil etkilenmez
    t = next(s for s in en["scenes"] if s["type"] == "talking")
    await c.patch(f"/api/scenes/{t['id']}", params={"lang": "en"}, json={"texts": {"en": "Deniz, look! The lighthouse is shining again."}})
    sc = (await c.get(f"/api/scenes/{t['id']}", params={"lang": "en"})).json()
    assert sc["voice_stale"] and "güncel değil" in sc["stale"] and sc["texts"]["en"]["status"] == "draft"
    r = (await c.post(f"/api/episodes/{eid}/steps/1/run", json={"mode": "missing", "lang": "en"})).json()
    assert r["created"] == [] and "onaylanmamış" in r["skipped"][0]["reason"]  # çeviri onaylanmadan ses üretilmez
    await c.post(f"/api/scenes/{t['id']}/texts/en/approve", json={"approved": True})
    en_now = (await c.get(f"/api/episodes/{eid}", params={"lang": "en"})).json()
    assert any("güncel değil" in b for b in en_now["steps"][4]["blockers"]), "eski sesle kurgu yapılmaz"
    assert not en_now["steps"][0]["complete"] and en_now["steps"][0]["stale"] == 1
    r = (await c.post(f"/api/episodes/{eid}/steps/1/run", json={"mode": "missing", "lang": "*"})).json()
    assert len(r["created"]) == 1  # yalnızca değişen sahnenin İngilizce sesi
    ep_en = await idle(c, eid, "en")
    sc = next(s for s in ep_en["scenes"] if s["id"] == t["id"])
    draft = next(a for a in sc["assets"] if a["role"] == "voice" and a["status"] == "draft")
    assert draft["language"] == "en" and draft["version"] == 2
    await c.post(f"/api/assets/{draft['id']}/approve")
    r = (await c.post(f"/api/episodes/{eid}/steps/3/run", json={"mode": "missing", "lang": "*"})).json()
    assert len(r["created"]) == 1, "yalnızca o sahnenin İngilizce konuşan videosu yeniden üretilir"
    ep_en = await idle(c, eid, "en")
    v = next(a for a in next(s for s in ep_en["scenes"] if s["id"] == t["id"])["assets"] if a["role"] == "scene_video" and a["status"] == "draft")
    assert v["language"] == "en" and v["params"]["src"]["audio_id"] == draft["id"]
    # konuşan videonun süresi o dilin ses süresine göre belirlenir
    assert abs(v["meta"]["duration"] - draft["meta"]["duration"]) < 0.6
    tr_scene = (await c.get(f"/api/scenes/{t['id']}", params={"lang": "tr"})).json()
    assert not tr_scene["voice_stale"] and not tr_scene["stale"], "Türkçe sürüm etkilenmez"



async def test_step_by_step_mode_requires_approval(client):
    """Varsayılan adım adım modda taslaklar onay bekler; onaysız bağımlı adım başlamaz."""
    c = client
    proj = (await c.post("/api/seed")).json()
    eid = proj["episodes"][0]["id"]
    r = (await c.post(f"/api/episodes/{eid}/steps/1/run", json={"mode": "missing", "lang": "tr"})).json()
    assert len(r["created"]) == 3  # 2 konuşan + 1 anlatıcı, yalnız Türkçe
    ep = await idle(c, eid)
    assert all(s["audio_id"] is None for s in ep["scenes"])
    st1 = ep["steps"][0]
    assert st1["draft"] == 3 and st1["approved"] == 0 and not st1["complete"]
    assert (await c.post(f"/api/episodes/{eid}/steps/1/run", json={"mode": "missing", "lang": "tr"})).json()["created"] == []
    # "tüm diller" yalnızca eksik olan İngilizce sesleri ekler
    assert len((await c.post(f"/api/episodes/{eid}/steps/1/run", json={"mode": "missing", "lang": "*"})).json()["created"]) == 3
    talking = next(s for s in ep["scenes"] if s["type"] == "talking")
    assert (await c.post(f"/api/scenes/{talking['id']}/generate", json={"kind": "audio", "lang": "tr"})).status_code == 200
    ep = await idle(c, eid)
    audios = [a for a in next(s for s in ep["scenes"] if s["id"] == talking["id"])["assets"] if a["role"] == "voice"]
    assert sorted(a["version"] for a in audios) == [1, 2] and all(a["language"] == "tr" for a in audios)
    await c.post(f"/api/assets/{audios[0]['id']}/approve")
    sc = (await c.get(f"/api/scenes/{talking['id']}", params={"lang": "tr"})).json()
    assert sc["audio_id"] == audios[0]["id"] and sc["assets"][0]["approved_by"] == "user"
    en_view = (await c.get(f"/api/scenes/{talking['id']}", params={"lang": "en"})).json()
    assert en_view["audio_id"] is None, "Türkçe onay İngilizce sürümü etkilemez"
    r = await c.post(f"/api/scenes/{talking['id']}/generate", json={"kind": "image", "lang": "tr"})
    assert r.status_code == 409 and "ana görseli onaylı değil" in r.json()["detail"]
    r = await c.post(f"/api/scenes/{talking['id']}/generate", json={"kind": "video", "lang": "tr"})
    assert r.status_code == 409 and "görseli onaylı değil" in r.json()["detail"]


async def test_languages_import_and_settings(client):
    c = client
    proj = (await c.post("/api/projects", json={"name": "İçe aktarma", "default_language": "tr"})).json()
    ep = (await c.post(f"/api/projects/{proj['id']}/episodes", json={})).json()
    csv_text = ("sahne_no;tur;karakter;replik;dil;gorsel_prompt;video_prompt;sure\n"
                "1;anlatıcı;;Bir zamanlar küçük bir kasabada yaşayan bir çocuk vardı.;;geniş plan;yavaş kaydırma;4\n"
                "2;konuşan;Ada;Good morning, my friend! How are you today?;en;yakın plan;hafif gülümser;3\n"
                "3;konuşan;Ada;Evet;;yakın plan;başını sallar;2\n"
                "4;konuşmasız;Ada;;;yürüyüş;takip planı;5\n")
    prev = (await c.post(f"/api/episodes/{ep['id']}/import", json={"text": csv_text, "preview": True})).json()
    assert [r["type"] for r in prev["rows"]] == ["narrator", "talking", "talking", "silent"]
    assert prev["rows"][0]["detected"]["language"] == "tr" and prev["rows"][1]["language"] == "en"
    assert prev["rows"][2]["detected"]["fallback"] is True  # kısa metin: proje dili kullanılır
    res = (await c.post(f"/api/episodes/{ep['id']}/import", json={"text": csv_text})).json()
    assert res["scenes"] == 4 and res["new_characters"] == ["Ada"]
    scenes = res["episode"]["scenes"]
    assert scenes[1]["fixed_language"] and scenes[1]["effective_language"] == "en" and scenes[1]["dialogue"].startswith("Good morning")
    assert not scenes[2]["fixed_language"] and scenes[2]["effective_language"] == "tr"

    # dil ekleme: bilinmeyen dil reddedilir; desteklemeyen sağlayıcı için üretimden önce uyarı
    assert (await c.post(f"/api/episodes/{ep['id']}/languages", json={"language": "xx"})).status_code == 400
    await c.put("/api/prefs", json={"models": {"elevenlabs_tts": "eleven_flash_v2_5"}})
    r = (await c.post(f"/api/episodes/{ep['id']}/languages", json={"language": "he"})).json()
    assert r["languages"] == ["tr", "he"] and any("eleven_v4" in w for w in r["warnings"])
    view = (await c.get(f"/api/episodes/{ep['id']}", params={"lang": "he"})).json()
    he_row = next(x for x in view["language_rows"] if x["code"] == "he")
    assert he_row["rtl"] and he_row["warnings"]
    # çeviri taslakken ses üretilmez
    await c.post(f"/api/episodes/{ep['id']}/translations", json={"language": "he", "items": [{"scene_no": 1, "text": "היה היה פעם ילד קטן"}]})
    r = (await c.post(f"/api/episodes/{ep['id']}/steps/1/run", json={"mode": "missing", "lang": "he"})).json()
    # (2. sahne İngilizceye sabitlenmiş: her dil sürümünde kendi dilinde seslendirilir; yalnızca o iş açılır)
    assert len(r["created"]) == 1 and any("onaylanmamış" in k["reason"] for k in r["skipped"])
    # anahtar yokken otomatik çeviri önerisi yok: alan elle doldurulur
    r = await c.post(f"/api/episodes/{ep['id']}/translations/suggest", json={"language": "he"})
    assert r.status_code == 409 and "elle" in r.json()["detail"]
    await c.put("/api/prefs", json={"models": {"elevenlabs_tts": "eleven_v4"}})

    # bağlantılar: anahtar kaydedilir, yanıtta yalnızca son 4 karakter döner
    connections.transport_override = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"data": {"billing_type": "wallet", "wallet": {"remaining_balance": 12, "currency": "usd"}}}
        if "users/me" in str(req.url) else {"data": []}))
    r = await c.put("/api/connections/heygen", json={"values": {"api_key": "hg-cok-gizli-anahtar-9876"}})
    assert "hg-cok-gizli-anahtar" not in r.text
    conn = r.json()["connection"]
    assert r.json()["result"]["ok"] and conn["set"] and conn["last4"] == "9876" and conn["status"] == "connected"
    r = await c.put("/api/prefs", json={"concurrency": {"kling_video": 3}, "budget_usd": 12.5, "plan_approval": True, "output_format": "9:16"})
    st = r.json()
    assert "hg-cok-gizli-anahtar" not in r.text
    assert st["prefs"]["concurrency"]["kling_video"] == 3 and st["prefs"]["budget_usd"] == 12.5 and st["prefs"]["plan_approval"] is True
    assert st["prefs"]["output_format"] == "9:16"
    assert st["claude"]["mode"] == "mock" and st["claude"]["model"] == "claude-fable-5-2"
    assert next(p for p in st["providers"] if p["key"] == "heygen_avatar")["mode"] == "real"
    assert next(p for p in st["providers"] if p["key"] == "kling_video")["mode"] == "mock"
    he = next(l for l in st["languages"] if l["code"] == "he")
    assert he["rtl"] and "eleven_multilingual_v2" not in he["elevenlabs"] and "eleven_v4" in he["elevenlabs"]
    assert "hg-cok-gizli" not in (await c.get("/api/config")).text + (await c.get("/api/connections")).text
    assert (await c.put("/api/prefs", json={"output_format": "4:3"})).status_code == 400
    await c.put("/api/prefs", json={"budget_usd": 25, "plan_approval": False, "output_format": "16:9", "concurrency": {"kling_video": 2}})
    assert (await c.get("/api/projects", headers={"Host": "kotu.example.com"})).status_code == 400
