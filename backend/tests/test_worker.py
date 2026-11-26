"""İş kuyruğu: üstel bekleme ile yeniden deneme, kalıcı hata, bozuk dosya, yeniden başlatmada devam, elle adım."""
import asyncio

import httpx
import pytest

import helpers

from app import connections, pipeline, settings_store as store, worker as worker_mod
from app.db import session_scope
from app.events import bus
from app.models import Asset, Episode, Job, Notification, Project, Scene
from app.providers import registry
from app.worker import Worker, backoff


@pytest.fixture
def scene_ids():
    with session_scope() as s:
        p = Project(name="Kuyruk testi", user_id=helpers.UID)
        s.add(p)
        s.flush()
        ep = Episode(project_id=p.id, number=1, title="b")
        s.add(ep)
        s.flush()
        sc = Scene(episode_id=ep.id, order_no=1, type="silent", image_prompt="x", video_prompt="y")
        s.add(sc)
        s.flush()
        return p.id, ep.id, sc.id


def add_job(ids, *, kind, provider, inp, mode="real", status="queued", external_id="") -> int:
    with session_scope() as s:
        j = Job(kind=kind, provider=provider, mode=mode, step=1, user_id=helpers.UID, project_id=ids[0], episode_id=ids[1], scene_id=ids[2],
                input=inp, status=status, external_id=external_id)
        s.add(j)
        s.flush()
        return j.id


async def run_until_done(job_id: int, timeout: float = 30) -> Job:
    bus.bind(asyncio.get_running_loop())
    w = Worker()
    w.tick = 0.02
    await w.start()
    try:
        for _ in range(int(timeout / 0.05)):
            with session_scope() as s:
                j = s.get(Job, job_id)
                if j.status in ("succeeded", "failed", "waiting_manual", "blocked"):
                    s.expunge(j)
                    return j
            await asyncio.sleep(0.05)
        raise AssertionError("iş bitmedi")
    finally:
        await w.stop()


def test_backoff_is_exponential_and_respects_retry_after():
    a, b, c = (backoff(n, 5.0) for n in (1, 2, 3))
    assert 5 <= a <= 6 and 10 <= b <= 12 and 20 <= c <= 24
    assert backoff(1, 5.0, retry_after=60) == 60
    assert backoff(20, 5.0) <= 360


async def test_transient_errors_are_retried_then_succeed(scene_ids, samples):
    helpers.set_key("elevenlabs_api_key", "el-test-key-000111")
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(429, json={"detail": {"code": "rate_limit_exceeded", "message": "yavaşla"}})
        return httpx.Response(200, content=samples["audio"].read_bytes(), headers={"character-cost": "9"})

    registry.transport_override = httpx.MockTransport(handler)
    jid = add_job(scene_ids, kind="voice", provider="elevenlabs_tts", inp={"text": "selam", "voice_id": "v1"})
    j = await run_until_done(jid)
    assert j.status == "succeeded" and j.attempts == 2 and len(calls) == 3
    assert j.cost_units == 9 and j.error == ""
    with session_scope() as s:
        a = s.get(Asset, j.result_asset_id)
        assert a.role == "voice" and a.version == 1 and a.status == "draft" and a.mode == "real"
        assert a.model == "eleven_v4" and a.prompt == "selam" and a.meta["duration"] > 1


async def test_gives_up_after_max_attempts(scene_ids):
    helpers.set_key("elevenlabs_api_key", "el-test-key-000111")
    store.put("max_attempts", 3)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, text="bakım")

    registry.transport_override = httpx.MockTransport(handler)
    jid = add_job(scene_ids, kind="voice", provider="elevenlabs_tts", inp={"text": "selam", "voice_id": "v1"})
    try:
        j = await run_until_done(jid)
    finally:
        store.put("max_attempts", 4)
    assert j.status == "failed" and len(calls) == 3 and "3 denemeden sonra" in j.error


async def test_permanent_error_fails_immediately_without_leaking_key(scene_ids):
    helpers.set_key("elevenlabs_api_key", "el-test-key-000111")
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"detail": {"code": "bad_request", "message": "bad input el-test-key-000111"}})

    registry.transport_override = httpx.MockTransport(handler)
    jid = add_job(scene_ids, kind="voice", provider="elevenlabs_tts", inp={"text": "selam", "voice_id": "v1"})
    j = await run_until_done(jid)
    assert j.status == "failed" and len(calls) == 1 and j.attempts == 0
    assert "400" in j.error and "el-test-key-000111" not in j.error


async def test_invalid_key_blocks_job_and_resumes_after_connection_is_fixed(scene_ids, samples):
    """Anahtar geçersiz/kredisiz: iş başarısız sayılmaz, durur; kullanıcıya bildirilir; anahtar yenilenince sürer."""
    helpers.set_key("elevenlabs_api_key", "el-eski-anahtar-000111")
    seen = []

    def handler(req):
        key = req.headers.get("xi-api-key")
        seen.append(key)
        if key != "el-yeni-anahtar-222333":
            return httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "Invalid API key el-eski-anahtar-000111"}})
        if req.url.path.endswith("/subscription"):
            return httpx.Response(200, json={"tier": "creator", "character_count": 10, "character_limit": 1000})
        if req.url.path.endswith("/voices"):
            return httpx.Response(200, json={"voices": [{"voice_id": "v1", "name": "Ada"}]})
        return httpx.Response(200, content=samples["audio"].read_bytes(), headers={"content-type": "audio/mpeg"})

    registry.transport_override = connections.transport_override = httpx.MockTransport(handler)
    jid = add_job(scene_ids, kind="voice", provider="elevenlabs_tts", inp={"text": "selam", "voice_id": "v1"})
    j = await run_until_done(jid)
    assert j.status == "blocked" and "el-eski-anahtar" not in j.error, (j.status, j.error)
    with session_scope() as s:
        n = s.query(Notification).filter(Notification.user_id == helpers.UID).order_by(Notification.id.desc()).first()
        assert n is not None and "ElevenLabs" in n.text and "el-eski-anahtar" not in n.text
        row = connections._row(s, helpers.UID, "elevenlabs")
        assert row.status == "error"
    # başka bir kullanıcının anahtarı düzeltmesi bu işi etkilemez
    other = helpers.make_user("baska-kuyruk@ornek.com")
    helpers.set_key("elevenlabs_api_key", "el-yeni-anahtar-222333", user_id=other)
    assert (await connections.test(other, "elevenlabs"))["ok"]
    with session_scope() as s:
        assert s.get(Job, jid).status == "blocked"
    # sahibi anahtarını yeniler ve sınar: iş kaldığı yerden sürer
    helpers.set_key("elevenlabs_api_key", "el-yeni-anahtar-222333")
    res = await connections.test(helpers.UID, "elevenlabs")
    assert res["ok"] and res["info"]["voice_count"] == 1
    with session_scope() as s:
        assert s.get(Job, jid).status == "queued"
    j = await run_until_done(jid)
    assert j.status == "succeeded" and seen[-1] == "el-yeni-anahtar-222333"


async def test_fair_scheduling_and_per_user_limits(scene_ids):
    """Bir kullanıcının kuyruğu diğerini bekletmez: sıra kullanıcılar arasında döner, kişi başı sınır aşılmaz."""
    other = helpers.make_user("adil-sira@ornek.com")
    with session_scope() as s:
        mine = [Job(kind="image", provider="kling_image", mode="mock", step=2, user_id=helpers.UID, project_id=scene_ids[0],
                    episode_id=scene_ids[1], scene_id=scene_ids[2], input={"prompt": "x"}, status="queued") for _ in range(6)]
        s.add_all(mine)
        s.flush()
        theirs = Job(kind="image", provider="kling_image", mode="mock", step=2, user_id=other, input={"prompt": "y"}, status="queued")
        s.add(theirs)
        s.flush()
        mine_ids, their_id = [j.id for j in mine], theirs.id
    w = Worker()
    started: list[tuple[int, int]] = []

    async def fake_run(job_id, user_id):
        started.append((job_id, user_id))
        await asyncio.sleep(3600)

    w._run = fake_run
    try:
        w._dispatch()
        await asyncio.sleep(0)
        users = [u for j, u in started if j in mine_ids + [their_id]]
        assert users.count(helpers.UID) == 2, started      # kişi başı kling_image sınırı: 2
        assert users.count(other) == 1, started             # 6 iş sıradayken bile diğer kullanıcı hemen başlar
    finally:
        for _, task in list(w.tasks.values()):
            task.cancel()
        await asyncio.sleep(0)
        with session_scope() as s:
            for j in s.query(Job).filter(Job.id.in_(mine_ids + [their_id])):
                j.status = "canceled"


async def test_corrupt_download_fails_job(scene_ids):
    helpers.set_key("elevenlabs_api_key", "el-test-key-000111")
    registry.transport_override = httpx.MockTransport(lambda req: httpx.Response(200, content=b"bu bir ses degil"))
    jid = add_job(scene_ids, kind="voice", provider="elevenlabs_tts", inp={"text": "selam", "voice_id": "v1"})
    j = await run_until_done(jid)
    assert j.status == "failed" and "bozuk" in j.error
    with session_scope() as s:
        assert s.query(Asset).filter(Asset.job_id == jid).count() == 0


async def test_restart_resumes_polling_without_resubmitting(scene_ids, samples):
    """Uygulama kapanırken 'çalışıyor' kalan iş yeniden gönderilmez; sorgulanıp tamamlanır."""
    helpers.set_key("kling_api_key", "kl-test-key-000111")
    methods = []

    def handler(req):
        methods.append((req.method, req.url.path))
        if req.url.path == "/tasks":
            return httpx.Response(200, json={"code": 0, "data": [{"id": "T77", "status": "succeeded", "outputs": [
                {"type": "video", "id": "o1", "url": "https://cdn.kling.test/v.mp4"}]}]})
        return httpx.Response(200, content=samples["video"].read_bytes())

    registry.transport_override = httpx.MockTransport(handler)
    running = add_job(scene_ids, kind="silent_video", provider="kling_video", inp={"prompt": "x", "duration": 5},
                      status="running", external_id="T77")
    j = await run_until_done(running)
    assert j.status == "succeeded"
    assert all(m == "GET" for m, _ in methods), methods
    with session_scope() as s:
        a = s.get(Asset, j.result_asset_id)
        assert a.kind == "video" and a.meta["source_url"] == "https://cdn.kling.test/v.mp4"

    # gönderim sırasında kesilen iş ise kuyruğa döner
    stuck = add_job(scene_ids, kind="silent_video", provider="kling_video", inp={"prompt": "x"}, status="submitting")
    Worker().recover()
    with session_scope() as s:
        assert s.get(Job, stuck).status == "queued"
        s.get(Job, stuck).status = "canceled"


async def test_omni_without_remote_url_becomes_manual_step(scene_ids, samples):
    """Kaynak videonun uzak adresi yoksa Omni 'elle adım' olur: talimat + indirme + yükleme."""
    helpers.set_key("kling_api_key", "kl-test-key-000111")
    assert registry.resolve_mode("kling_omni", {"video_url": ""}) == "manual"
    assert registry.resolve_mode("kling_omni", {"video_url": "https://cdn/x.mp4"}) == "real"
    store.put("mock_mode", True)
    assert registry.resolve_mode("kling_omni", {"video_url": "https://cdn/x.mp4"}) == "mock"
    store.put("mock_mode", False)

    jid = add_job(scene_ids, kind="omni_fix", provider="kling_omni", mode="manual",
                  inp={"instruction": "ışığı düzelt", "video_path": "unit/x.mp4", "video_url": ""})
    j = await run_until_done(jid)
    assert j.status == "waiting_manual"

    from app.main import app
    bus.bind(asyncio.get_running_loop())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await helpers.login(c)
        info = next(x for x in (await c.get("/api/jobs")).json()["jobs"] if x["id"] == jid)["manual"]
        assert info["steps"] and info["inputs"][0]["url"] == "/media/unit/x.mp4" and info["instruction"] == "ışığı düzelt"
        bad = await c.post(f"/api/jobs/{jid}/manual-upload", files={"file": ("x.mp4", b"bozuk", "video/mp4")})
        assert bad.status_code == 400
        ok = await c.post(f"/api/jobs/{jid}/manual-upload",
                          files={"file": ("sonuc.mp4", samples["video"].read_bytes(), "video/mp4")})
        assert ok.status_code == 200 and ok.json()["status"] == "running"
    j = await run_until_done(jid)
    assert j.status == "succeeded"
    with session_scope() as s:
        a = s.get(Asset, j.result_asset_id)
        assert a.role == "scene_video" and a.mode == "manual" and a.status == "draft"
