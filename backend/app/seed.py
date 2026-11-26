"""Örnek veri: 2 karakter + anlatıcı sesi, 5 sahnelik (konuşan / konuşmasız / anlatıcı karışık) bir bölüm.
Kaynak dil Türkçe, ikinci hedef dil İngilizce."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import pipeline, services
from .models import Project

CHARACTERS = [
    dict(name="Elif", description="Woman in her early 30s, shoulder-length dark curly hair, olive coat, thoughtful brown eyes",
         voice_settings={"stability": 0.5, "similarity_boost": 0.75}),
    dict(name="Deniz", description="Man in his mid 30s, short black hair, light stubble, navy fisherman jacket",
         voice_settings={"stability": 0.45, "similarity_boost": 0.8}),
    dict(name="Anlatıcı", description="", is_narrator=True, voice_settings={"stability": 0.6, "similarity_boost": 0.7}),
]

SCENES = [
    dict(type="narrator", characters=[], duration=6, audio_moment=False,
         dialogue="Küçük bir sahil kasabasında, deniz feneri yıllardır yanmıyordu. Ta ki o sabaha kadar.",
         en="In a small seaside town, the lighthouse had been dark for years. Until that morning.",
         image_prompt="Wide establishing shot of a small Aegean fishing town at dawn, an old white lighthouse on a cliff, soft mist, cinematic",
         video_prompt="Slow aerial push-in toward the lighthouse, mist drifting over the water"),
    dict(type="talking", characters=["Elif"], duration=5, audio_moment=False,
         dialogue="Deniz, fenerin ışığını gördün mü? Dün gece biri onu yakmış.",
         en="Deniz, did you see the lighthouse beam? Someone lit it last night.",
         image_prompt="Medium close-up of Elif on the harbor pier at sunrise, looking toward the lighthouse, worried expression",
         video_prompt="She speaks calmly, slight head turn toward the lighthouse"),
    dict(type="silent", characters=["Deniz"], duration=5, audio_moment=True, dialogue="", en="",
         image_prompt="Deniz walking along the rocky shore toward the lighthouse, waves crashing, seagulls overhead",
         video_prompt="Tracking shot following him along the shore, waves crash on rocks, wind moves his jacket"),
    dict(type="talking", characters=["Deniz"], duration=5, audio_moment=False,
         dialogue="Kapı açık Elif. İçeride biri var, merdivenlerden ses geliyor.",
         en="The door is open, Elif. Someone is inside, I can hear footsteps on the stairs.",
         image_prompt="Close-up of Deniz at the open lighthouse door, half his face in shadow, tense",
         video_prompt="He whispers urgently, glancing over his shoulder into the dark"),
    dict(type="silent", characters=["Elif", "Deniz"], duration=5, audio_moment=False, dialogue="", en="",
         image_prompt="Elif and Deniz climbing the spiral staircase inside the lighthouse, warm light glowing from above",
         video_prompt="Camera rises with them up the spiral stairs, light flickers at the top"),
]


def seed(s: Session, force: bool = False) -> Project | None:
    """Örnek projeyi GEÇERLİ kullanıcı için ekler."""
    from . import tenancy
    uid = tenancy.require_uid()
    if not force and s.execute(select(func.count(Project.id)).where(Project.user_id == uid)).scalar():
        return None
    p = services.create_project(s, "Örnek Dizi: Fenerin Işığı", "Mock modda uçtan uca denemek için örnek proje.",
                                "cinematic, warm natural light, 35mm film look, consistent character design", "tr")
    # dil bazlı telaffuz sözlüğü örneği
    p.pronunciations = {"tr": {"rules": [["Dr.", "Doktor"]]}, "en": {"rules": [["Dr.", "Doctor"]]}}
    for c in CHARACTERS:
        services.create_character(s, p, **c)
    ep = services.create_episode(s, p, title="Yanan Fener", language="tr", langs=["tr", "en"])
    rows = [{k: v for k, v in d.items() if k != "en"} | {"order_no": i} for i, d in enumerate(SCENES, 1)]
    services.write_scenes(s, ep, rows)
    services.set_translations(s, ep, "en", [{"scene_no": i, "text": d["en"]} for i, d in enumerate(SCENES, 1) if d["en"]],
                              approve=True, origin="human")
    s.flush()
    return p
