"""Uçtan uca test 4 (yalıtım): iki kullanıcı aynı anda üretim yapar; her iş kendi anahtarıyla çalışır ve
kimse diğerinin projesine, dosyasına, işine, sohbetine ya da bağlantısına erişemez (hepsi 404)."""
import asyncio

import httpx

import helpers
from app import pipeline, settings_store as store, tenancy
from app.agent import runner, tools
from app.db import session_scope
from app.events import bus
from app.main import app
from app.models import AgentRun, Asset, Connection, Conversation, Job, Project
from app.providers import registry

KEY_A, KEY_B = "el-ANAHTAR-A-aaaa1111", "el-ANAHTAR-B-bbbb2222"
SMALL = ("1 | anlatıcı | | Eski değirmen yıllardır dönmüyordu, ta ki o geceye kadar. | Old stone windmill on a hill at dusk | Slow push-in | 4\n"
         "2 | konuşmasız | Ada | | Ada opening the creaking mill door, dust in the light | Door opens slowly | 4\n")


async def wait_for(fn, timeout: float = 600, every: float = 0.3):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while True:
        v = await fn()
        if v:
            return v
        if loop.time() > end:
            raise AssertionError("zaman aşımı")
        await asyncio.sleep(every)


async def prepare(c: httpx.AsyncClient, voice: str) -> dict:
    """Örnek projeyi kurar ve bütün karakterlere kullanıcıya özgü bir ses kimliği atar."""
    proj = (await c.post("/api/seed")).json()
    for ch in proj["characters"]:
        r = await c.patch(f"/api/characters/{ch['id']}", json={"voice_id": voice})
        assert r.status_code == 200, r.text
    return proj


async def test_two_users_produce_at_the_same_time_with_their_own_keys(client, samples):
    a = client
    uid_b = helpers.make_user("ikinci@ornek.com")
    helpers.set_key("elevenlabs_api_key", KEY_A)
    helpers.set_key("elevenlabs_api_key", KEY_B, user_id=uid_b)
    seen: list[tuple[str, str]] = []
    in_flight = {"now": 0, "max": 0, "users": set()}

    async def handler(req: httpx.Request):
        key, voice = req.headers.get("xi-api-key"), req.url.path.rsplit("/", 1)[-1]
        seen.append((key, voice))
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        if in_flight["now"] > 1:
            in_flight["users"].add(key)
        await asyncio.sleep(0.15)
        in_flight["now"] -= 1
        return httpx.Response(200, content=samples["audio"].read_bytes(), headers={"content-type": "audio/mpeg"})

    registry.transport_override = httpx.MockTransport(handler)
    async with helpers.raw_client(app) as b:
        await helpers.login(b, "ikinci@ornek.com")
        pa, pb = await prepare(a, "ses-A"), await prepare(b, "ses-B")
        ea, eb = pa["episodes"][0]["id"], pb["episodes"][0]["id"]

        # iki kullanıcı aynı anda 1. adımı (sesler) başlatır
        ra, rb = await asyncio.gather(a.post(f"/api/episodes/{ea}/steps/1/run", json={"mode": "all", "lang": "tr"}),
                                      b.post(f"/api/episodes/{eb}/steps/1/run", json={"mode": "all", "lang": "tr"}))
        ja, jb = ra.json()["created"], rb.json()["created"]
        assert len(ja) == 3 and len(jb) == 3, (ra.text, rb.text)

        async def idle():
            x = (await a.get(f"/api/episodes/{ea}")).json()["active_jobs"]
            y = (await b.get(f"/api/episodes/{eb}")).json()["active_jobs"]
            return not x and not y
        await wait_for(idle)

        # her istek yalnızca işin sahibinin anahtarıyla gitti
        assert len(seen) == 6 and {k for k, _ in seen} == {KEY_A, KEY_B}
        assert all((k == KEY_A) == (v == "ses-A") for k, v in seen), seen
        assert in_flight["max"] >= 2 and in_flight["users"] == {KEY_A, KEY_B}  # gerçekten eşzamanlı
        with session_scope() as s:
            for uid, ep_id, key in ((helpers.UID, ea, KEY_A), (uid_b, eb, KEY_B)):
                jobs = s.query(Job).filter(Job.episode_id == ep_id, Job.kind == "voice").all()
                assert len(jobs) == 3 and all(j.user_id == uid and j.status == "succeeded" and j.mode == "real"
                                              and j.cred_source == "own" for j in jobs)
                assets = s.query(Asset).filter(Asset.episode_id == ep_id).all()
                assert assets and all(x.user_id == uid and x.path.startswith(f"u{uid}/") for x in assets)
                assert not any(key in str(j.input) + (j.error or "") for j in jobs)
            asset_a = s.query(Asset).filter(Asset.episode_id == ea).first()
            asset_b = s.query(Asset).filter(Asset.episode_id == eb).first()
            aid_a, path_a, aid_b, path_b = asset_a.id, asset_a.path, asset_b.id, asset_b.path
            scene_a = asset_a.scene_id
            char_a = pa["characters"][0]["id"]

        # sohbetler de aynı anda (sahte yönetmen) çalışır
        ca = (await a.post("/api/conversations", json={})).json()["id"]
        cb = (await b.post("/api/conversations", json={})).json()["id"]
        await asyncio.gather(a.post(f"/api/conversations/{ca}/messages", json={"text": "A'nın kısa bölümü:\n" + SMALL}),
                             b.post(f"/api/conversations/{cb}/messages", json={"text": "B'nin kısa bölümü:\n" + SMALL}))

        async def both_done():
            x = (await a.get(f"/api/conversations/{ca}")).json()
            y = (await b.get(f"/api/conversations/{cb}")).json()
            ok = all(v["run"] and v["run"]["status"] in ("done", "failed", "paused_budget", "waiting_user") for v in (x, y))
            return (x, y) if ok else None
        xa, xb = await wait_for(both_done, timeout=900)
        assert xa["run"]["status"] == "done" and xb["run"]["status"] == "done", (xa["run"], xb["run"])
        with session_scope() as s:
            assert s.get(Project, xa["project_id"]).user_id == helpers.UID and s.get(Project, xb["project_id"]).user_id == uid_b
            for conv, uid in ((xa, helpers.UID), (xb, uid_b)):
                jobs = s.query(Job).filter(Job.project_id == conv["project_id"]).all()
                assert jobs and all(j.user_id == uid for j in jobs)
                assert all(x.user_id == uid and x.path.startswith(f"u{uid}/") for x in s.query(Asset).filter(Asset.episode_id == conv["episode_id"]))
        card_a = next(m for m in xa["messages"] if m["type"] == "note" and m["result"])["result"]
        final_a = card_a["languages"][0]["video_url"] if "languages" in card_a else None

        # ---------------------------------------------------------------- IDOR: B, A'nın hiçbir şeyine erişemez
        job_a = ja[0]["id"] if isinstance(ja[0], dict) else ja[0]
        gets = [f"/api/projects/{pa['id']}", f"/api/episodes/{ea}", f"/api/scenes/{scene_a}", f"/api/conversations/{ca}",
                f"/media/{path_a}", f"/api/costs?project_id={pa['id']}" if False else f"/api/episodes/{ea}?lang=en"]
        if final_a:
            gets.append(final_a)
        for url in gets:
            r = await b.get(url)
            assert r.status_code == 404, (url, r.status_code)
        writes = [("patch", f"/api/projects/{pa['id']}", {"name": "ele geçirildi"}), ("delete", f"/api/projects/{pa['id']}", None),
                  ("post", f"/api/projects/{pa['id']}/characters", {"name": "Sızan"}), ("post", f"/api/projects/{pa['id']}/episodes", {}),
                  ("patch", f"/api/characters/{char_a}", {"voice_id": "ses-B"}), ("delete", f"/api/characters/{char_a}", None),
                  ("post", f"/api/characters/{char_a}/generate-image", {}),
                  ("patch", f"/api/episodes/{ea}", {"title": "x"}), ("delete", f"/api/episodes/{ea}", None),
                  ("post", f"/api/episodes/{ea}/steps/1/run", {"mode": "all"}), ("post", f"/api/episodes/{ea}/auto", {"on": True}),
                  ("post", f"/api/episodes/{ea}/languages", {"language": "de"}), ("post", f"/api/episodes/{ea}/import", {"text": SMALL}),
                  ("post", f"/api/episodes/{ea}/scenes", {"type": "silent"}),
                  ("patch", f"/api/scenes/{scene_a}", {"dialogue": "x"}), ("delete", f"/api/scenes/{scene_a}", None),
                  ("post", f"/api/scenes/{scene_a}/generate", {"kind": "voice", "lang": "tr"}),
                  ("post", f"/api/scenes/{scene_a}/fix", {"method": "voice"}),
                  ("post", f"/api/assets/{aid_a}/approve", None), ("post", f"/api/assets/{aid_a}/reject", None),
                  ("delete", f"/api/assets/{aid_a}", None),
                  ("post", f"/api/jobs/{job_a}/retry", None), ("post", f"/api/jobs/{job_a}/cancel", None),
                  ("post", f"/api/conversations/{ca}/messages", {"text": "merhaba"}), ("post", f"/api/conversations/{ca}/stop", None),
                  ("post", f"/api/conversations/{ca}/resume", {}), ("delete", f"/api/conversations/{ca}", None)]
        for method, url, body in writes:
            r = await b.request(method.upper(), url, json=body) if body is not None else await b.request(method.upper(), url)
            assert r.status_code == 404, (method, url, r.status_code, r.text[:200])
        up = await b.post(f"/api/conversations/{ca}/attachments", files={"file": ("a.txt", b"x", "text/plain")})
        assert up.status_code == 404
        up = await b.post(f"/api/jobs/{job_a}/manual-upload", files={"file": ("a.mp3", samples["audio"].read_bytes(), "audio/mpeg")})
        assert up.status_code == 404

        # listeler yalnızca kendi kayıtlarını içerir
        assert pa["id"] not in [p["id"] for p in (await b.get("/api/projects")).json()]
        b_jobs = (await b.get("/api/jobs", params={"limit": 500})).json()["jobs"]
        with session_scope() as s:
            mine = {j.id for j in s.query(Job.id).filter(Job.user_id == uid_b)}
        assert b_jobs and {j["id"] for j in b_jobs} <= mine
        assert ca not in [x["id"] for x in (await b.get("/api/conversations")).json()]
        costs_b, costs_a = (await b.get("/api/costs")).json(), (await a.get("/api/costs")).json()
        assert str(pa["id"]) not in {str(r.get("project_id")) for r in costs_b.get("projects", [])} or costs_b != costs_a
        # bağlantılar: B yalnızca kendi anahtarının son 4 karakterini görür; A'nınki hiçbir yanıtta yok
        conn_b = (await b.get("/api/connections")).text
        assert "2222" in conn_b and "1111" not in conn_b and KEY_A not in conn_b and KEY_B not in conn_b
        assert "1111" in (await a.get("/api/connections")).text
        # B kendi anahtarını silince A'nınki durur
        assert (await b.delete("/api/connections/elevenlabs")).status_code == 200
        with session_scope() as s:
            assert s.query(Connection).filter(Connection.user_id == helpers.UID, Connection.service == "elevenlabs").count() == 1

        # A'nın verisi yerinde ve A kendi dosyasını indirebilir; B de kendisininkini
        assert (await a.get(f"/api/projects/{pa['id']}")).json()["name"] == pa["name"]
        assert (await a.get(f"/media/{path_a}")).status_code == 200 and (await b.get(f"/media/{path_b}")).status_code == 200
        assert (await a.get(f"/media/{path_b}")).status_code == 404  # yönetici de başkasının dosyasını göremez
        assert (await a.get(f"/api/projects/{pb['id']}")).status_code == 404
        assert (await a.get(f"/api/conversations/{cb}")).status_code == 404

    # ---------------------------------------------------------------- ajan araçları: kimlik sunucudaki kayıttan gelir
    with session_scope() as s:
        run_a = s.query(AgentRun).filter(AgentRun.conversation_id == ca).order_by(AgentRun.id.desc()).first().id
    r = await tools.execute(ca, run_a, "varlik_onayla", {"varlik_idleri": [aid_b]})
    assert r.is_error, r.content   # A'nın ajanı B'nin varlığına dokunamaz
    with session_scope() as s:
        assert s.get(Asset, aid_b).status != "approved" or s.get(Asset, aid_b).approved_by != "agent"
    # araç girdisiyle kullanıcı/proje seçilemez
    assert "Geçersiz girdi" in tools.validate("maliyet_getir", {"kullanici_id": uid_b})
    assert "Geçersiz girdi" in tools.validate("baglanti_durumu", {"user_id": uid_b})
    # araç, çağıranın bağlamı ne olursa olsun sohbetin sahibi adına çalışır
    with tenancy.as_user(uid_b):
        r = await tools.execute(ca, run_a, "baglanti_durumu", {})
        assert not r.is_error and KEY_A not in r.content and "1111" not in r.content
        with session_scope() as s:
            try:
                tools.Ctx(s, ca, run_a)
                raise AssertionError("başkasının sohbeti açılmamalıydı")
            except tools.ToolError:
                pass
        # B bağlamında oluşturulan sohbet B'nindir
        with session_scope() as s:
            assert runner.create_conversation(s).user_id == uid_b
    # ayarlar kullanıcıya özeldir
    store.put_prefs(uid_b, {"budget_usd": 3.0})
    assert store.prefs(helpers.UID)["budget_usd"] == 25.0 and store.prefs(uid_b)["budget_usd"] == 3.0
    with tenancy.as_user(uid_b):
        assert store.get("budget_usd") == 3.0 and store.get_secret("elevenlabs_api_key") == ""
    assert store.get_secret("elevenlabs_api_key") == KEY_A


async def test_events_reach_only_their_owner():
    bus.bind(asyncio.get_running_loop())
    other = helpers.make_user("olay@ornek.com")
    qa, qb = bus.subscribe(helpers.UID), bus.subscribe(other)
    try:
        bus.publish("job", id=1)                      # geçerli kullanıcı bağlamı (A)
        bus.publish("chat", user_id=other, conversation_id=9)
        with session_scope() as s:
            pipeline.emit(s, "job", id=2, status="queued", user_id=other)
        await asyncio.sleep(0.05)
        a_msgs = [qa.get_nowait() for _ in range(qa.qsize())]
        b_msgs = [qb.get_nowait() for _ in range(qb.qsize())]
        assert len(a_msgs) == 1 and '"id": 1' in a_msgs[0]
        assert len(b_msgs) == 2 and all('"id": 1' not in m for m in b_msgs)
        token = tenancy.USER_ID.set(None)
        try:
            bus.publish("job", id=3)                  # sahibi belli olmayan olay kimseye gitmez
        finally:
            tenancy.USER_ID.reset(token)
        await asyncio.sleep(0.02)
        assert qa.qsize() == 0 and qb.qsize() == 0
    finally:
        bus.unsubscribe(qa)
        bus.unsubscribe(qb)
