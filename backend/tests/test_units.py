"""İçe aktarıcı, zaman çizelgesi, altyazı ve kalite raporu yardımcılarının birim testleri."""
from pathlib import Path

from app import importer, montage, qc

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def test_import_example_csv_and_json():
    rows = importer.parse((EXAMPLES / "ornek_senaryo.csv").read_text(encoding="utf-8"), "ornek_senaryo.csv")
    assert [r["type"] for r in rows] == ["narrator", "talking", "silent", "talking", "silent"]
    assert rows[1]["characters"] == ["Elif"] and rows[4]["characters"] == ["Elif", "Deniz"]
    assert rows[2]["audio_moment"] is True and rows[0]["duration"] == 6
    assert rows[0]["language"] == "" and rows[1]["language"] == "tr"  # dil sütunu: boşsa otomatik algılanır
    rows = importer.parse((EXAMPLES / "ornek_senaryo.json").read_text(encoding="utf-8"), "x.json")
    assert len(rows) == 3 and rows[2]["audio_moment"] is True and rows[1]["dialogue"].startswith("Deniz")


def test_import_pasted_pipe_table_and_free_script():
    rows = importer.parse("1 | konuşan | Elif | Merhaba | yakın plan | gülümser | 4\n2 | konuşmasız | | | sahil | kaydırma | 5,5")
    assert rows[0]["type"] == "talking" and rows[0]["dialogue"] == "Merhaba" and rows[1]["duration"] == 5.5
    rows = importer.parse((EXAMPLES / "ornek_senaryo.txt").read_text(encoding="utf-8"))
    assert [r["type"] for r in rows] == ["narrator", "talking", "silent", "talking"]
    assert rows[1]["characters"] == ["Elif"] and rows[3]["characters"] == ["Deniz"]
    assert rows[2]["image_prompt"].startswith("Deniz walking")
    assert importer.tr_title("IŞIK İNCE") == "Işık İnce"
    # yeni 8 sütunlu başlıksız biçim (dil sütunlu) ve tablonun önünde serbest metin
    rows = importer.parse("Şunu çek:\n1 | konuşan | Elif | Hello there | en | close-up | smiles | 4\n2 | konuşmasız | | | | beach | pan | 5")
    assert rows[0]["language"] == "en" and rows[0]["image_prompt"] == "close-up" and rows[1]["duration"] == 5
    assert importer.preview(rows, "tr")[0]["detected"] is None
    assert importer.parse("") == []


def test_timeline_and_transitions():
    clips = [{"scene_id": i, "order_no": i, "type": "silent", "duration": d, "speech_dur": 0}
             for i, d in ((1, 4.0), (2, 6.0), (3, 5.0))]
    tl, total, t = montage.build_timeline(clips, "fade", 0.3)
    assert total == 15.0 and [x["start"] for x in tl] == [0.0, 4.0, 10.0] and t == 0.3
    tl, total, t = montage.build_timeline(clips, "xfade", 0.5)
    assert total == 14.0 and [x["start"] for x in tl] == [0.0, 3.5, 9.0]
    assert "xfade=transition=fade:duration=0.500:offset=3.500" in montage._video_graph(3, [4, 6, 5], "xfade", 0.5)
    assert montage._video_graph(2, [4, 6], "cut", 0) == "[0:v][1:v]concat=n=2:v=1:a=0[vout]"
    g = montage._audio_graph(2, [4, 6], "fade", 0.3, 10, 4, 0.25, True, [(5, 2.5, 1.0)], "anull")
    assert "sidechaincompress" in g and "adelay=2500" in g and "amix=inputs=3" in g and g.endswith("[aout]")


def test_srt_generation():
    assert montage.srt_time(3661.5) == "01:01:01,500"
    cues = montage.split_cues("kelime " * 40, 0, 10)
    assert len(cues) > 1 and cues[0][0] == 0 and abs(cues[-1][1] - 10) < 1e-6
    plan = [{"dialogue": "Merhaba dünya"}, {"dialogue": ""}]
    tl = [{"start": 0, "end": 4, "speech_end": 3}, {"start": 4, "end": 9, "speech_end": None}]
    assert montage.build_srt(plan, tl).startswith("1\n00:00:00,050 --> 00:00:02,950\nMerhaba dünya")


def test_qc_parsers_and_scene_mapping():
    tl = [{"scene_id": 1, "order_no": 1, "start": 0, "end": 5, "type": "talking"},
          {"scene_id": 2, "order_no": 2, "start": 5, "end": 10, "type": "silent"}]
    assert qc.scene_at(tl, 1)["order_no"] == 1
    assert qc.scene_at(tl, 4.8, 9.9)["order_no"] == 2  # aralığın çoğu 2. sahnede
    assert qc.scene_at(tl, 10)["order_no"] == 2 and qc.scene_at([], 1) is None
    assert qc.parse_black("[blackdetect @ 0x1] black_start:1.5 black_end:2.75 black_duration:1.25") == [(1.5, 2.75)]
    assert qc.parse_freeze("lavfi.freezedetect.freeze_start: 3.2\nlavfi.freezedetect.freeze_duration: 2.1") == [(3.2, 2.1)]
    assert qc.parse_silence("silence_start: 7.5\nsilence_end: 9 | silence_duration: 1.5\nsilence_start: 12", 15) == [(7.5, 9.0), (12.0, 15)]
