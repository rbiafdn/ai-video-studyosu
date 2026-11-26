"""Dil desteği: algılama, sağlayıcı desteği, adaptörlerde dil parametresi, telaffuz sözlüğü, dudak senkronu ölçümü."""
import json

import httpx
import pytest

from app import languages, media, pipeline, qc
from app.providers.elevenlabs import ElevenLabsTTS
from app.providers.mock import MockProvider


# --------------------------------------------------------------------- dil algılama
@pytest.mark.parametrize("text,expected", [
    ("Deniz, fenerin ışığını gördün mü? Dün gece biri onu yakmış.", "tr"),
    ("The door is open. Someone is inside, I can hear footsteps on the stairs.", "en"),
    ("Guten Morgen, wie geht es dir heute? Ich habe gut geschlafen.", "de"),
    ("مرحبا كيف حالك اليوم يا صديقي العزيز", "ar"),
    ("今日はとても良い天気ですね。散歩に行きましょう。", "ja"),
])
def test_detects_language(text, expected):
    d = languages.detect(text, "tr")
    assert d["language"] == expected and d["detected"] == expected and not d["fallback"] and d["confidence"] >= 0.6


def test_short_or_uncertain_text_falls_back_to_project_language():
    for text in ("Evet", "OK", "Hmm...", ""):
        d = languages.detect(text, "de")
        assert d["language"] == "de" and d["fallback"] is True
    # proje dili aynı dilin bölgeli hâliyse korunur
    d = languages.detect("Buenos días, ¿cómo estás hoy? Espero que muy bien.", "es-MX")
    assert d["language"] == "es-MX" and d["detected"] == "es" and not d["fallback"]


def test_language_registry_and_provider_support():
    assert languages.normalize("es_mx") == "es-MX" and languages.base("es-MX") == "es"
    assert languages.known("es-MX") and not languages.known("xx")
    assert languages.is_rtl("ar") and languages.is_rtl("he") and not languages.is_rtl("tr")
    # İbranice ve Farsça yalnızca v4/v3'te; diğerleri dört modelde de var
    assert languages.tts_models("he") == ["eleven_v4", "eleven_v3"]
    assert "eleven_flash_v2_5" in languages.tts_models("tr")
    assert languages.tts_model_for("tr", "eleven_flash_v2_5") == "eleven_flash_v2_5"
    assert languages.tts_model_for("he", "eleven_flash_v2_5") == "eleven_v4"  # model değişir, dil değişmez
    assert languages.support("elevenlabs_tts", "he")[0] and not languages.support("elevenlabs_tts", "xx")[0]
    ok, note = languages.support("heygen_avatar", "ar")
    assert ok and "belgelenmemiş" in note
    assert languages.font_for("ar") and languages.font_for("ja") != languages.font_for("tr")
    m = {l["code"]: l for l in languages.matrix()}
    assert m["ar"]["rtl"] and m["fa"]["elevenlabs"] == ["eleven_v4", "eleven_v3"]


# --------------------------------------------------------------------- adaptörde dil parametresi
async def test_elevenlabs_sends_language_code_and_supported_model(make_ctx, samples):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, content=samples["audio"].read_bytes())

    p = ElevenLabsTTS(make_ctx("elevenlabs_tts", handler, model="eleven_flash_v2_5"))
    await p.submit({"text": "Hello", "voice_id": "v", "language_code": "en"})
    await p.submit({"text": "שלום", "voice_id": "v", "language_code": "he", "model": "eleven_v4"})
    await p.submit({"text": "Merhaba", "voice_id": "v", "language_code": "tr", "model": "eleven_multilingual_v2",
                    "pronunciation_locators": [{"pronunciation_dictionary_id": "D1", "version_id": "V1"}]})
    assert bodies[0]["model_id"] == "eleven_flash_v2_5" and bodies[0]["language_code"] == "en"
    assert bodies[1]["model_id"] == "eleven_v4" and bodies[1]["language_code"] == "he"
    # multilingual_v2 language_code parametresini desteklemez: gönderilmez
    assert bodies[2]["model_id"] == "eleven_multilingual_v2" and "language_code" not in bodies[2]
    assert bodies[2]["pronunciation_dictionary_locators"] == [{"pronunciation_dictionary_id": "D1", "version_id": "V1"}]


def test_pronunciation_rules_and_per_language_voice():
    from app.models import Character, Project
    proj = Project(name="p", pronunciations={"tr": {"rules": [["Dr.", "Doktor"], ["TBMM", "Türkiye Büyük Millet Meclisi"]]},
                                             "en": {"rules": [["Dr.", "Doctor"]]}})
    assert pipeline.apply_pronunciation(proj, "tr", "Dr. Ayşe TBMM'ye gitti.") == "Doktor Ayşe Türkiye Büyük Millet Meclisi'ye gitti."
    assert pipeline.apply_pronunciation(proj, "en", "Dr. Smith") == "Doctor Smith"
    assert pipeline.apply_pronunciation(proj, "de", "Dr. Braun") == "Dr. Braun"
    ch = Character(name="Elif", voice_id="varsayilan", voice_settings={"stability": 0.5},
                   voices={"en": {"voice_id": "ingilizce-ses", "voice_settings": {"stability": 0.3}}})
    assert pipeline.voice_for(ch, "en") == ("ingilizce-ses", {"stability": 0.3}, True)
    assert pipeline.voice_for(ch, "en-GB")[0] == "ingilizce-ses"
    assert pipeline.voice_for(ch, "de") == ("varsayilan", {"stability": 0.5}, False)  # tanımlı değilse varsayılan ses


# --------------------------------------------------------------------- altyazı ve dudak senkronu
def test_subtitle_filter_uses_script_font_and_cjk_split():
    from app import montage
    assert "FontName=" + languages.font_for("ar") in montage.sub_filter("ar")
    assert montage.sub_filter("tr") != montage.sub_filter("ja")
    cues = montage.split_cues("今日はとても良い天気ですね。" * 12, 0, 12)
    assert len(cues) > 1 and all(len(c[2]) <= 90 for c in cues)


async def test_lipsync_measurement_detects_shift(make_ctx, tmp_path):
    """Sahte konuşan videoda 'ağız' sesle birlikte hareket eder: kayma ~0. Ses kaydırılınca kayma ölçülür."""
    voice = MockProvider(make_ctx("elevenlabs_tts", lambda r: httpx.Response(500), kind="voice", job_id=901), "audio")
    voice.inp = {"text": "bir iki üç dört beş altı yedi sekiz dokuz on on bir on iki", "voice_id": "x", "language": "tr"}
    audio = await voice.fetch("mock:0", tmp_path / "ses.bin")
    rel = audio.resolve().relative_to(voice.ctx.storage_dir.resolve()) if voice.ctx.storage_dir.resolve() in audio.resolve().parents else None
    if rel is None:  # geçici klasör depo dışında: depoya kopyala
        import shutil
        dest = voice.ctx.storage_dir / "unit" / "lipsync.mp3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audio, dest)
        rel = "unit/lipsync.mp3"
    vid = MockProvider(make_ctx("heygen_avatar", lambda r: httpx.Response(500), kind="talking_video", job_id=902), "video")
    vid.inp = {"audio_path": str(rel).replace("\\", "/"), "audio_duration": media.info(audio)["duration"], "label": "test"}
    video = await vid.fetch("mock:0", tmp_path / "konusan.bin")
    good = media.lipsync_offset(video)
    assert good["measurable"] and good["confidence"] > 0.3 and abs(good["offset_ms"]) <= 80, good
    assert qc.lipsync_finding(good, 120, 0.3) is None

    # sesi 320 ms geciktir: ölçüm kaymayı ve yönünü bulmalı, bulgu dudak senkronu düzeltmesi önermeli
    shifted = tmp_path / "kaymis.mp4"
    media.ffmpeg(["-i", video, "-itsoffset", "0.32", "-i", video, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", shifted])
    bad = media.lipsync_offset(shifted)
    assert bad["confidence"] > 0.3 and 200 <= bad["offset_ms"] <= 440, bad
    sev, msg = qc.lipsync_finding(bad, 120, 0.3)
    assert sev == "warn" and "geride" in msg
    assert qc.lipsync_finding({"measurable": True, "offset_ms": 300, "confidence": 0.1}, 120, 0.3)[0] == "info"
    assert media.speech_onset(audio) >= 0
