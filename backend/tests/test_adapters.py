"""Adaptör birim testleri: istek biçimi, hata sınıflandırma ve yeniden deneme davranışı.

Gerçek servislere gidilmez; httpx.MockTransport ile resmi dokümandaki biçimler doğrulanır.
"""
import base64
import json
import shutil

import httpx
import pytest

from app.config import get_settings
from app.providers.base import PermanentError, TransientError
from app.providers.elevenlabs import ElevenLabsTTS
from app.providers.heygen import HeyGenAvatar
from app.providers.kling import KlingImage, KlingLipSync, KlingOmni, KlingVideo, clamp_duration
from app.providers.manual import ManualProvider
from app.providers.mock import MockProvider


def put(samples, kind: str, name: str) -> str:
    """Örnek dosyayı depoya kopyalar, göreli yolunu döndürür."""
    dest = get_settings().storage_dir / "unit" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(samples[kind], dest)
    return f"unit/{name}"


# --------------------------------------------------------------------- ElevenLabs
async def test_elevenlabs_request_format_and_cost(make_ctx, samples, tmp_path):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["req"] = req
        return httpx.Response(200, content=samples["audio"].read_bytes(), headers={"character-cost": "17"})

    p = ElevenLabsTTS(make_ctx("elevenlabs_tts", handler, job_id=7))
    sub = await p.submit({"text": " Merhaba dünya ", "voice_id": "VOICE1",
                          "voice_settings": {"stability": 0.4, "bilinmeyen": 1}})
    req = seen["req"]
    assert req.method == "POST"
    assert req.url.path == "/v1/text-to-speech/VOICE1"
    assert req.url.host == "api.elevenlabs.io"
    assert req.url.params["output_format"] == "mp3_44100_128"
    assert req.headers["xi-api-key"] == "el-SECRET-123456"
    body = json.loads(req.content)
    assert body == {"text": "Merhaba dünya", "model_id": "eleven_v4", "voice_settings": {"stability": 0.4}}
    assert sub.cost_units == 17
    assert (await p.poll(sub.external_id)).status == "succeeded"
    out = await p.fetch(sub.external_id, tmp_path / "ses.bin")
    assert out.suffix == ".mp3" and out.stat().st_size > 0
    assert (await p.poll(sub.external_id)).status == "lost"  # geçici dosya taşındı


@pytest.mark.parametrize("status,exc", [(429, TransientError), (500, TransientError), (503, TransientError),
                                         (401, PermanentError), (402, PermanentError), (422, PermanentError)])
async def test_elevenlabs_error_classes(make_ctx, status, exc):
    def handler(req):
        return httpx.Response(status, json={"detail": {"code": "x", "message": "anahtar el-SECRET-123456 hatalı"}})

    p = ElevenLabsTTS(make_ctx("elevenlabs_tts", handler))
    with pytest.raises(exc) as e:
        await p.submit({"text": "a", "voice_id": "v"})
    assert "el-SECRET-123456" not in str(e.value)  # anahtar hata metnine sızmaz


async def test_elevenlabs_timeout_is_transient_and_validation(make_ctx):
    def handler(req):
        raise httpx.ReadTimeout("yavaş", request=req)

    p = ElevenLabsTTS(make_ctx("elevenlabs_tts", handler))
    with pytest.raises(TransientError):
        await p.submit({"text": "a", "voice_id": "v"})
    with pytest.raises(PermanentError):
        await p.submit({"text": "a", "voice_id": ""})
    with pytest.raises(PermanentError):
        await p.submit({"text": "", "voice_id": "v"})
    p3 = ElevenLabsTTS(make_ctx("elevenlabs_tts", handler, model="eleven_v3"))
    with pytest.raises(PermanentError):  # model karakter sınırı
        await p3.submit({"text": "x" * 5001, "voice_id": "v"})


def test_elevenlabs_cost_estimate():
    from app.config import get_models
    c = ElevenLabsTTS.estimate_cost({"text": "a" * 500}, get_models()["providers"]["elevenlabs_tts"], "eleven_v4")
    assert c.usd == pytest.approx(0.04) and c.units == 500


# --------------------------------------------------------------------- Kling
def kling_ok(data):
    return httpx.Response(200, json={"code": 0, "message": "SUCCEED", "request_id": "r", "data": data})


async def test_kling_image_request_poll_fetch(make_ctx, samples, tmp_path):
    ref = put(samples, "image", "ref.png")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.method == "POST":
            return kling_ok({"id": "T1", "status": "submitted"})
        if req.url.path == "/tasks":
            return kling_ok([{"id": "T1", "status": "succeeded",
                              "outputs": [{"type": "image", "url": "https://cdn.kling.test/out/a.png"}]}])
        return httpx.Response(200, content=samples["image"].read_bytes())

    p = KlingImage(make_ctx("kling_image", handler))
    sub = await p.submit({"prompt": "liman", "ref_image_paths": [ref], "aspect_ratio": "16:9"})
    post = calls[0]
    assert post.url.host == "api-singapore.klingai.com" and post.url.path == "/image-generation/kling-4.0"
    assert post.headers["authorization"] == "Bearer kl-SECRET-123456"
    body = json.loads(post.content)
    assert body["contents"][0]["type"] == "prompt" and "@image_1" in body["contents"][0]["text"]
    assert body["contents"][1] == {"type": "refer_image", "id": "image_1",
                                   "url": base64.b64encode(samples["image"].read_bytes()).decode()}
    assert body["settings"] == {"result_type": "single", "n": 1, "resolution": "2k", "aspect_ratio": "16:9"}
    assert sub.external_id == "T1"
    assert (await p.poll("T1")).status == "succeeded"
    assert calls[1].url.params["task_ids"] == "T1"
    out = await p.fetch("T1", tmp_path / "g.bin")
    assert out.suffix == ".png" and out.read_bytes() == samples["image"].read_bytes()
    assert p.result_meta["source_url"] == "https://cdn.kling.test/out/a.png"


@pytest.mark.parametrize("http,code,exc", [
    (429, 1303, TransientError),   # eşzamanlılık sınırı: yeniden dene
    (429, 1302, TransientError),   # hız sınırı
    (500, 5000, TransientError),
    (429, 1102, PermanentError),   # kaynak paketi bitti: HTTP 429 olsa da kalıcı
    (429, 1101, PermanentError),
    (401, 1002, PermanentError),
    (400, 1301, PermanentError),   # içerik güvenliği
])
async def test_kling_error_codes(make_ctx, http, code, exc):
    def handler(req):
        return httpx.Response(http, json={"code": code, "message": "hata", "request_id": "r"})

    p = KlingImage(make_ctx("kling_image", handler))
    with pytest.raises(exc):
        await p.submit({"prompt": "x"})


async def test_kling_poll_states(make_ctx):
    state = {"s": "submitted", "msg": ""}

    def handler(req):
        return kling_ok([{"id": "T", "status": state["s"], "message": state["msg"], "outputs": []}])

    p = KlingVideo(make_ctx("kling_video", handler))
    assert (await p.poll("T")).status == "running"
    state["s"] = "processing"
    assert (await p.poll("T")).status == "running"
    state.update(s="failed", msg="içerik reddedildi")
    r = await p.poll("T")
    assert r.status == "failed" and "içerik reddedildi" in r.error


async def test_kling_video_native_audio_and_duration(make_ctx, samples):
    img = put(samples, "image", "frame.png")
    bodies = []

    def handler(req):
        bodies.append((req.url.path, json.loads(req.content)))
        return kling_ok({"id": "V1", "status": "submitted"})

    # 3.0: ses anahtarı var -> sesli an 'native', değilse 'off'
    p = KlingVideo(make_ctx("kling_video", handler, model="kling-3.0"))
    await p.submit({"prompt": "dalgalar", "image_path": img, "duration": 4.6, "native_audio": True})
    await p.submit({"prompt": "dalgalar", "image_path": img, "duration": 40, "native_audio": False})
    path, b = bodies[0]
    assert path == "/image-to-video/kling-3.0"
    assert b["contents"][0] == {"type": "prompt", "text": "dalgalar"}
    assert b["contents"][1]["type"] == "first_frame" and b["contents"][1]["url"]
    assert b["settings"] == {"duration": 5, "resolution": "1080p", "audio": "native"}
    assert bodies[1][1]["settings"]["audio"] == "off" and bodies[1][1]["settings"]["duration"] == 15
    # 4.0: ses hep açık, parametre gönderilmez
    p4 = KlingVideo(make_ctx("kling_video", handler, model="kling-4.0"))
    await p4.submit({"prompt": "x", "image_path": img, "duration": 5, "native_audio": False})
    assert bodies[2][0] == "/image-to-video/kling-4.0" and "audio" not in bodies[2][1]["settings"]
    # görselsiz: metinden videoya
    await p.submit({"prompt": "x", "duration": 5})
    assert bodies[3][0] == "/text-to-video/kling-3.0" and bodies[3][1]["prompt"] == "x"


def test_kling_duration_clamp_and_cost():
    from app.config import get_models
    cfg = get_models()["providers"]["kling_video"]
    assert clamp_duration({"durations": [5, 10]}, 7) == 5
    assert clamp_duration({"durations": [5, 10]}, 8) == 10
    assert clamp_duration({"min_duration": 3, "max_duration": 15}, 1) == 3
    silent = KlingVideo.estimate_cost({"duration": 5}, cfg, "kling-3.0")
    loud = KlingVideo.estimate_cost({"duration": 5, "native_audio": True}, cfg, "kling-3.0")
    assert silent.usd == pytest.approx(0.56) and loud.usd == pytest.approx(0.84)


async def test_kling_omni_request(make_ctx):
    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return kling_ok({"id": "O1"})

    p = KlingOmni(make_ctx("kling_omni", handler))
    with pytest.raises(PermanentError):  # video yalnızca URL ile verilebilir
        await p.submit({"instruction": "gökyüzünü düzelt", "video_path": "x.mp4"})
    sub = await p.submit({"instruction": "gökyüzünü düzelt", "video_url": "https://cdn/x.mp4", "video_duration": 5})
    assert sub.external_id == "O1" and seen["path"] == "/omni-video/kling-3.0-omni"
    assert seen["body"]["contents"] == [{"type": "prompt", "text": "gökyüzünü düzelt (@video_1)"},
                                        {"type": "base_video", "url": "https://cdn/x.mp4", "id": "video_1"}]
    assert seen["body"]["settings"] == {"audio": "original"}
    with pytest.raises(PermanentError):  # model sınırından uzun video
        await p.submit({"instruction": "x", "video_url": "https://cdn/x.mp4", "video_duration": 40})


async def test_kling_lipsync_two_step_flow(make_ctx, samples, tmp_path):
    audio = put(samples, "audio", "konusma.mp3")
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/v1/videos/identify-face":
            return kling_ok({"session_id": "S1", "face_data": [
                {"face_id": "kisa", "start_time": 0, "end_time": 500},
                {"face_id": "uzun", "start_time": 100, "end_time": 4000}]})
        if req.method == "POST":
            return kling_ok({"task_id": "L1", "task_status": "submitted"})
        if req.url.path.startswith("/v1/videos/advanced-lip-sync/"):
            return kling_ok({"task_id": "L1", "task_status": "succeed",
                             "task_result": {"videos": [{"id": "vid", "url": "https://cdn.kling.test/l.mp4"}]}})
        return httpx.Response(200, content=samples["video"].read_bytes())

    p = KlingLipSync(make_ctx("kling_lipsync", handler))
    sub = await p.submit({"video_url": "https://cdn/v.mp4", "audio_path": audio, "audio_duration": 2.0})
    assert calls[0][1:] == ("/v1/videos/identify-face", {"video_url": "https://cdn/v.mp4"})
    body = calls[1][2]
    assert calls[1][1] == "/v1/videos/advanced-lip-sync" and body["session_id"] == "S1"
    face = body["face_choose"][0]
    assert face["face_id"] == "uzun" and face["sound_end_time"] == 2000 and face["sound_insert_time"] == 100
    assert face["sound_file"] == base64.b64encode(samples["audio"].read_bytes()).decode()
    assert sub.external_id == "L1"
    assert (await p.poll("L1")).status == "succeeded"
    out = await p.fetch("L1", tmp_path / "l.bin")
    assert out.suffix == ".mp4" and out.stat().st_size > 0


# --------------------------------------------------------------------- HeyGen
async def test_heygen_upload_generate_poll_fetch(make_ctx, samples, tmp_path):
    img, aud = put(samples, "image", "karakter.png"), put(samples, "audio", "replik.mp3")
    calls = []
    state = {"status": "processing"}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path == "/v3/assets":
            return httpx.Response(200, json={"data": {"asset_id": f"A{len(calls)}"}})
        if req.url.path == "/v3/models/avatars":
            return httpx.Response(202, json={"data": {"video_id": "HV1", "status": "pending"}})
        if req.url.path == "/v3/models/videos/HV1":
            d = {"video_id": "HV1", "status": state["status"]}
            if state["status"] == "completed":
                d["video_url"] = "https://files.heygen.test/v.mp4?Expires=1"
            if state["status"] == "failed":
                d.update(failure_code="moderation_rejected", failure_message="içerik reddedildi")
            return httpx.Response(200, json={"data": d})
        return httpx.Response(200, content=samples["video"].read_bytes())

    p = HeyGenAvatar(make_ctx("heygen_avatar", handler, job_id=3))
    sub = await p.submit({"image_path": img, "audio_path": aud, "audio_duration": 2.0, "prompt": "sakin konuşur",
                          "idempotency_key": "abc123"})
    up1, up2, gen = calls
    for up in (up1, up2):
        assert up.url.host == "api.heygen.com" and up.headers["x-api-key"] == "hg-SECRET-123456"
        assert up.headers["content-type"].startswith("multipart/form-data")
        assert b'name="file"' in up.content
    assert gen.headers["idempotency-key"] == "abc123"
    assert json.loads(gen.content) == {
        "model": "heygen-avatar-6", "reference_images": [{"type": "asset_id", "asset_id": "A1"}],
        "audio": {"type": "asset_id", "asset_id": "A2"}, "resolution": "1080p", "motion_prompt": "sakin konuşur"}
    assert sub.external_id == "m:HV1"
    assert (await p.poll(sub.external_id)).status == "running"
    state["status"] = "failed"
    r = await p.poll(sub.external_id)
    assert r.status == "failed" and "moderation_rejected" in r.error
    state["status"] = "completed"
    assert (await p.poll(sub.external_id)).status == "succeeded"
    out = await p.fetch(sub.external_id, tmp_path / "v.bin")
    assert out.suffix == ".mp4" and out.stat().st_size > 0


async def test_heygen_avatar_iv_route_and_errors(make_ctx, samples):
    img, aud = put(samples, "image", "k2.png"), put(samples, "audio", "r2.mp3")
    seen = {}

    def ok(req):
        if req.url.path == "/v3/assets":
            return httpx.Response(200, json={"data": {"asset_id": "A"}})
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"data": {"video_id": "V9"}})

    p = HeyGenAvatar(make_ctx("heygen_avatar", ok, model="avatar_iv"))
    sub = await p.submit({"image_path": img, "audio_path": aud, "audio_duration": 2})
    assert sub.external_id == "v:V9" and seen["path"] == "/v3/videos"
    assert seen["body"]["type"] == "image" and seen["body"]["audio_asset_id"] == "A"
    assert seen["body"]["image"] == {"type": "asset_id", "asset_id": "A"}

    def limited(req):
        return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "yavaş"}},
                              headers={"Retry-After": "7"})

    with pytest.raises(TransientError) as e:
        await HeyGenAvatar(make_ctx("heygen_avatar", limited)).submit({"image_path": img, "audio_path": aud})
    assert e.value.retry_after == 7

    def broke(req):
        return httpx.Response(402, json={"error": {"code": "insufficient_credit", "message": "kredi yok"}})

    with pytest.raises(PermanentError) as e2:
        await HeyGenAvatar(make_ctx("heygen_avatar", broke)).submit({"image_path": img, "audio_path": aud})
    assert "insufficient_credit" in str(e2.value)
    with pytest.raises(PermanentError):  # ses sınırı
        await HeyGenAvatar(make_ctx("heygen_avatar", ok)).submit({"image_path": img, "audio_path": aud, "audio_duration": 601})


# --------------------------------------------------------------------- mock ve elle adım
async def test_mock_generates_valid_media(make_ctx, tmp_path):
    from app import media
    for kind, pk, inp, check in (
        ("voice", "elevenlabs_tts", {"text": "bir iki üç dört beş", "voice_id": "x"}, "audio"),
        ("scene_image", "kling_image", {"prompt": "liman", "label": "Sahne 1"}, "image"),
        ("silent_video", "kling_video", {"prompt": "dalga", "duration": 2, "native_audio": True}, "video"),
    ):
        p = MockProvider(make_ctx(pk, lambda r: httpx.Response(500), kind=kind, job_id=1), check)
        p.inp = inp
        sub = await p.submit(inp)
        assert (await p.poll(sub.external_id)).status in ("running", "succeeded")
        out = await p.fetch(sub.external_id, tmp_path / f"{kind}.bin")
        info = media.validate(out, check)
        if kind == "silent_video":
            assert info["has_audio"] and abs(info["duration"] - 2) < 0.3


async def test_manual_adapter_waits_for_upload(make_ctx, samples, tmp_path):
    ctx = make_ctx("kling_omni", lambda r: httpx.Response(500))
    p = ManualProvider(ctx, "video")
    sub = await p.submit({"instruction": "x"})
    assert sub.manual and (await p.poll(sub.external_id)).status == "running"
    shutil.copyfile(samples["video"], ctx.tmp_dir / "manual_1.mp4")
    assert (await p.poll("manual:manual_1.mp4")).status == "succeeded"
    out = await p.fetch("manual:manual_1.mp4", tmp_path / "sonuc.bin")
    assert out.suffix == ".mp4" and out.exists()
