import argparse
import json
from pathlib import Path
from fractions import Fraction

import pytest

import subocr


def test_repeated_caption_after_blank_is_a_separate_cue():
    timeline = subocr.Timeline()
    for t, text in [(0.033, "你来"), (0.067, "你来"), (0.133, ""), (0.2, "你来"), (0.233, "")]:
        timeline.push(t, subocr.Reading(text, 0.98))
    assert [(c["start"], c["end"], c["text"]) for c in timeline.cues] == [
        (0.033, 0.133, "你来"), (0.2, 0.233, "你来")]


def test_adjacent_one_character_difference_is_preserved():
    timeline = subocr.Timeline()
    timeline.push(10, subocr.Reading("是你", .99))
    timeline.push(10.033, subocr.Reading("是我", .99))
    timeline.finish(10.067)
    assert [c["text"] for c in timeline.cues] == ["是你", "是我"]
    assert timeline.cues[0]["end"] == timeline.cues[1]["start"]


def cue(start, end, text):
    return dict(start=start, end=end, text=text, confidence=.98, observations=1)


def test_stabilization_repairs_bracketed_glitches_and_retains_evidence():
    raw = [cue(0, 2, "入赘"), cue(2, 2.033, "人赘"), cue(2.033, 2.067, "厂赘"),
           cue(2.067, 3, "入赘"), cue(3, 3.033, "人赘"), cue(3.033, 4, "入赘")]
    result, repairs = subocr.stabilize(raw)
    assert len(result) == 1 and len(repairs) == 2
    assert result[0]["start"] == 0 and result[0]["end"] == 4
    assert [c["text"] for c in repairs[0]["readings"]] == ["人赘", "厂赘"]
    assert len(raw) == 6 and raw[0]["end"] == 2


def test_stabilization_never_crosses_blank_gap_or_long_changed_caption():
    raw = [cue(0, 1, "不是"), cue(1, 1.1, "是"), cue(1.2, 2, "不是")]
    assert len(subocr.stabilize(raw)[0]) == 3
    raw = [cue(0, 1, "不是"), cue(1, 1.5, "是"), cue(1.5, 2, "不是")]
    assert len(subocr.stabilize(raw)[0]) == 3
    raw = [cue(0, 1, "是你"), cue(1, 1.033, "是我")]
    assert len(subocr.stabilize(raw)[0]) == 2


def test_long_ocr_like_variants_are_flagged_without_changing_text_or_times():
    cues = [cue(0, 2, "断言"), cue(2, 2.4, "断盲"), cue(2.4, 3, "断言")]
    cues[1]["confidence"] = .95
    subocr.annotate_cues(cues, 0, 4, .033)
    assert all("similar_neighbor_check" in c["flags"] for c in cues)
    assert [(c["start"],c["end"],c["text"]) for c in cues] == [(0,2,"断言"),(2,2.4,"断盲"),(2.4,3,"断言")]


@pytest.mark.parametrize("value,expected", [(59.9996, "00:01:00,000"), (3600.001, "01:00:00,001"), (0.033, "00:00:00,033")])
def test_timestamp_rounding(value, expected):
    assert subocr.stamp(value) == expected


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "1:2:3:4"])
def test_bad_time(value):
    with pytest.raises(argparse.ArgumentTypeError):
        subocr.seconds(value)


def test_translation_preserves_timing_and_unicode(tmp_path):
    cues = [dict(start=0.033, end=1.067, text="是你"), dict(start=1.1, end=2, text="你好\n再见")]
    subocr.write_subtitles(cues, tmp_path / "zh.srt")
    subocr.write_subtitles(cues, tmp_path / "en.srt", ["It's you.", "Hello.\nGoodbye."])
    zh, en = [(tmp_path / name).read_text() for name in ["zh.srt", "en.srt"]]
    assert [x for x in zh.splitlines() if "-->" in x] == [x for x in en.splitlines() if "-->" in x]
    assert "你好\n再见" in zh


def test_overlap_rejected(tmp_path):
    with pytest.raises(ValueError, match="overlapping"):
        subocr.write_subtitles([dict(start=0, end=2, text="你"), dict(start=1, end=3, text="我")], tmp_path / "bad.srt")
    assert not (tmp_path / "bad.srt").exists()


def test_missing_mapping_does_not_silently_use_network(tmp_path):
    source = tmp_path / "ocr.json"
    source.write_text(json.dumps({"cues": [{"text": "你好"}]}))
    mapping = tmp_path / "mapping.json"
    mapping.write_text("{}")
    args = argparse.Namespace(input=source, mapping=mapping, target="en", work_dir=tmp_path)
    with pytest.raises(ValueError, match="missing"):
        subocr.translate(args)


def test_online_translation_glossary_and_cache_are_separate(tmp_path, monkeypatch):
    source = tmp_path / "ocr.json"
    source.write_text(json.dumps({"cues": [dict(id=1, start=1.1, end=2.2, text="花垣城", confidence=.99)]}))
    glossary = tmp_path / "glossary.json"
    glossary.write_text(json.dumps({"花垣": "Huayuan", "花垣城": "Huayuan City"}))
    calls = []
    monkeypatch.setattr(subocr.shutil, "which", lambda name: "/usr/bin/trans")
    def run(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "Huayuan City\n", "stderr": ""})()
    monkeypatch.setattr(subocr.subprocess, "run", run)
    args = subocr.parser().parse_args(["translate", str(source), "--work-dir", str(tmp_path), "--glossary", str(glossary), "--delay", "0"])
    subocr.translate(args)
    subocr.translate(args)
    assert len(calls) == 1
    assert calls[0][-1] == "Huayuan City"  # Longer term wins without recursive replacement.
    args.glossary = None
    subocr.translate(args)
    assert len(calls) == 2
    assert calls[-1][-1] == "花垣城"


def test_empty_online_translation_never_creates_subtitles(tmp_path, monkeypatch):
    source = tmp_path / "ocr.json"
    source.write_text(json.dumps({"cues": [dict(id=1, start=0, end=1, text="你好", confidence=.99)]}))
    monkeypatch.setattr(subocr.shutil, "which", lambda name: "/usr/bin/trans")
    monkeypatch.setattr(subocr.subprocess, "run", lambda *a, **k: type("Result", (), {"returncode": 0, "stdout": "", "stderr": "network error"})())
    monkeypatch.setattr(subocr.time, "sleep", lambda t: None)
    args = subocr.parser().parse_args(["translate", str(source), "--work-dir", str(tmp_path)])
    with pytest.raises(RuntimeError, match="Translation failed"):
        subocr.translate(args)
    assert not (tmp_path / "ocr.en.srt").exists()


def test_dense_credits_skip_recognition_and_cache_can_be_reused_without_limit():
    import sqlite3
    import numpy as np
    engine_calls = []
    box = [[0, 0], [10, 0], [10, 10], [0, 10]]
    def engine(crop, **kwargs):
        engine_calls.append(kwargs)
        if kwargs.get("use_rec") is False:
            return [box] * 6, None
        return [[box, "你好", .99]], None
    ocr = subocr.OCR.__new__(subocr.OCR)
    ocr.engine, ocr.identity, ocr.confidence = engine, "fixture", .7
    ocr.language = "zh"
    engine.get_crop_img_list = lambda crop, boxes: [crop for box in boxes]
    engine.text_rec = lambda crops: ([("你好", .99)] + [("", 0)] * (len(crops)-1), None)
    ocr.max_boxes, ocr.calls, ocr.hits = 4, 0, 0
    ocr.db = sqlite3.connect(":memory:")
    ocr.db.execute("CREATE TABLE frames (key TEXT PRIMARY KEY, result TEXT)")
    crop = np.zeros((64, 64, 3), dtype=np.uint8)
    assert ocr.read(0, crop).layout_rejected
    assert len(engine_calls) == 1
    def no_conversion():
        raise AssertionError("A cache hit should not convert the frame")
    assert ocr.read(0, no_conversion).layout_rejected
    ocr.max_boxes = 0
    assert ocr.read(0, crop).text == "你好"
    assert len(engine_calls) == 2
    ocr.max_center_offset = .15
    assert ocr.read(0, crop).layout_rejected
    ocr.max_center_offset = None
    assert ocr.read(0, crop).text == "你好"  # The raw cached reading was retained.
    assert len(engine_calls) == 2
    ocr.db.close()


def test_real_variable_rate_frame_timestamps(tmp_path):
    av = pytest.importorskip("av")
    import numpy as np
    path = tmp_path / "vfr.mkv"
    with av.open(str(path), "w") as c:
        s = c.add_stream("ffv1", rate=30)
        s.width = s.height = 64
        s.pix_fmt = "yuv420p"
        s.time_base = Fraction(1, 1000)
        s.codec_context.time_base = Fraction(1, 1000)
        for pts in [0, 33, 133, 167, 500]:
            f = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24")
            f.pts, f.time_base = pts, Fraction(1, 1000)
            for packet in s.encode(f):
                c.mux(packet)
        for packet in s.encode():
            c.mux(packet)
    times = [f[0] for f in subocr.frames(path, 0, None, (0, 0, 1, 1))]
    assert times == pytest.approx([0, .033, .133, .167, .5])
    assert [f[0] for f in subocr.frames(path, .1, .4, (0, 0, 1, 1))] == pytest.approx([.133, .167])
    eager = list(subocr.frames(path, 0, None, (.1, .1, .9, .9)))
    lazy = list(subocr.frames(path, 0, None, (.1, .1, .9, .9), True))
    for a, b in zip(eager, lazy):
        assert a[:3] == b[:3]
        assert np.array_equal(a[3], b[3]())
    regions = {"zh": (.1, .1, .9, .5), "en": (.1, .5, .9, .9)}
    multi = list(subocr.frames(path, 0, None, regions, True))
    for language, region in regions.items():
        single = list(subocr.frames(path, 0, None, region))
        assert all(np.array_equal(a[3]()[language], b[3]) for a, b in zip(multi, single))


@pytest.mark.parametrize("refine", ["linear", "bisect"])
def test_refinement_finds_transition_inside_sampling_window(tmp_path, monkeypatch, refine):
    import numpy as np
    # Coarse samples at .0 and .2 differ; the real change is at .133.
    observations = [(0, "甲"), (.033, "甲"), (.067, "甲"), (.1, "甲"),
                    (.133, "乙"), (.167, "乙"), (.2, "乙"), (.233, "")]
    crop = np.zeros((64, 64, 3), dtype=np.uint8)
    monkeypatch.setattr(subocr, "frames", lambda *a: iter((t, .033, i, crop) for i, (t, _) in enumerate(observations)))
    class FakeOCR:
        def __init__(self, *a):
            self.calls = self.hits = 0
            self.db = type("DB", (), {"close": lambda self: None})()
        def read(self, pts, image):
            self.calls += 1
            return subocr.Reading(observations[pts][1], .99)
    monkeypatch.setattr(subocr, "OCR", FakeOCR)
    video = tmp_path / "fake.mkv"
    video.write_bytes(b"fake")
    prefix = tmp_path / "out"
    args = subocr.parser().parse_args(["extract", str(video), "--sample-fps", "5", "--refine", refine, "--output", str(prefix), "--work-dir", str(tmp_path)])
    subocr.extract(args)
    data = json.loads(prefix.with_suffix(".json").read_text())
    assert [(c["start"], c["end"]) for c in data["cues"]] == [(0, .133), (.133, .233)]


def test_bilingual_union_preserves_independent_gaps_and_transitions():
    result = subocr.combine_tracks({"zh": [cue(1, 3, "你好"), cue(4, 5, "再见")],
                                    "en": [cue(1.1, 2.8, "Hello"), cue(4, 5, "Goodbye")]})
    assert [(c["start"], c["end"], c["text"]) for c in result] == [
        (1, 1.1, "你好"), (1.1, 2.8, "你好\nHello"), (2.8, 3, "你好"), (4, 5, "再见\nGoodbye")]
    adjacent = subocr.combine_tracks({"zh": [cue(0, 1, "甲"), cue(1, 2, "乙")],
                                     "en": [cue(0, 2, "A then B")]})
    assert [c["text"] for c in adjacent] == ["甲\nA then B", "乙\nA then B"]


def test_multitrack_export_keeps_language_and_independent_times(tmp_path):
    source = tmp_path / "source.json"
    tracks = {"zh": {"raw_cues": [cue(0, 1, "你好")], "cues": [cue(0, 1, "你好")], "repairs": []},
              "en": {"raw_cues": [cue(.1, 1.1, "Hello there")], "cues": [cue(.1, 1.1, "Hello there")], "repairs": []}}
    data = dict(primary_language="zh", tracks=tracks, start=0, end=2,
                stats={"max_frame_duration": .033}, **tracks["zh"])
    source.write_text(json.dumps(data))
    subocr.export(subocr.parser().parse_args(["export", str(source)]))
    assert "你好" in (tmp_path / "source.zh.srt").read_text()
    assert "Hello there" in (tmp_path / "source.en.srt").read_text()
    assert "00:00:00,100 --> 00:00:01,100" in (tmp_path / "source.en.srt").read_text()
    assert "你好\nHello there" in (tmp_path / "source.zh-en.srt").read_text()


def test_merge_repairs_cuts_at_actual_frame_times_and_preserves_blanks(tmp_path):
    inputs = []
    for n, (start, end, raw) in enumerate([
            (0, 1.999625, [cue(1, 1.999625, "你好")]),
            (2, 4, [cue(2.01, 3, "你好"), cue(3.5, 4, "再见")]),
            (4, 6, [cue(5, 6, "再见")])]):
        path = tmp_path / f"part{n}.json"
        data = dict(config={"fixture": True}, start=start, end=end, primary_language="zh",
                    raw_cues=raw, cues=raw, repairs=[],
                    stats=dict(first_frame_time=start+.01, max_frame_duration=.04))
        path.write_text(json.dumps(data)); inputs.append(str(path))
    output = tmp_path / "merged"
    subocr.merge(subocr.parser().parse_args(["merge", *inputs, "--output", str(output)]))
    data = json.loads(output.with_suffix('.json').read_text())
    assert [(c['start'], c['end'], c['text']) for c in data['cues']] == [
        (1, 3, '你好'), (3.5, 4.01, '再见'), (5, 6, '再见')]
    # Trim without decoding, retaining original-video times.
    subocr.export(subocr.parser().parse_args(["export", str(output.with_suffix('.json')),
                                             "--start", "2.5", "--end", "5.5"]))
    data = json.loads(output.with_suffix('.json').read_text())
    assert [(c['start'], c['end']) for c in data['cues']] == [(2.5,3), (3.5,4.01), (5,5.5)]


def test_translation_cannot_overwrite_existing_ocr_track(tmp_path):
    source = tmp_path / "movie.json"
    source.write_text(json.dumps(dict(primary_language="zh", tracks={"zh": {}, "en": {}})))
    args = subocr.parser().parse_args(["translate", str(source), "--target", "en"])
    with pytest.raises(ValueError, match="already exists"):
        subocr.translate(args)


@pytest.mark.parametrize("refine", ["linear", "bisect"])
def test_extraction_keeps_track_transitions_independent(tmp_path, monkeypatch, refine):
    import numpy as np
    states = [("甲", "A"), ("甲", "A"), ("甲", "B"), ("乙", "B"), ("", "B"), ("", "")]
    crop = np.zeros((64,64,3), dtype=np.uint8)
    monkeypatch.setattr(subocr, 'frames', lambda *a: iter(
        (i*.04,.04,i,{'zh':crop,'en':crop}) for i in range(len(states))))
    class FakeOCR:
        def __init__(self, work, identity, confidence, threads, max_boxes, language, *args):
            self.column = 0 if language == 'zh' else 1
            self.calls = self.hits = 0
            self.db = type('DB', (), {'close': lambda self: None})()
        def read(self, pts, image):
            self.calls += 1
            assert image().shape == crop.shape
            return subocr.Reading(states[pts][self.column], .99)
    monkeypatch.setattr(subocr, 'OCR', FakeOCR)
    video = tmp_path/'source.mkv';video.write_bytes(b'fixture')
    model = tmp_path/'en.onnx';model.write_bytes(b'fixture')
    prefix = tmp_path/'result'
    args = subocr.parser().parse_args(['extract',str(video),'--output',str(prefix),
        '--track','zh=0,0,1,0.5','--track','en=0,0.5,1,1','--english-model',str(model),
        '--work-dir',str(tmp_path),'--sample-fps','5','--refine',refine])
    subocr.extract(args)
    data=json.loads(prefix.with_suffix('.json').read_text())
    assert [(c['start'],c['end'],c['text']) for c in data['tracks']['zh']['cues']] == [(0,.12,'甲'),(.12,.16,'乙')]
    assert [(c['start'],c['end'],c['text']) for c in data['tracks']['en']['cues']] == [(0,.08,'A'),(.08,.2,'B')]


def test_reviewed_corrections_preserve_raw_ocr_and_survive_reexport(tmp_path):
    source=tmp_path/'scan.json'
    raw=[cue(1,2,'Come quick!y')]
    source.write_text(json.dumps(dict(primary_language='en', start=0, end=3,
        stats={'max_frame_duration':.04}, cues=raw, raw_cues=raw, repairs=[])))
    edits=tmp_path/'edits.json'
    edits.write_text(json.dumps([dict(language='en',start=1,original='Come quick!y',text='Come quickly')]))
    subocr.export(subocr.parser().parse_args(['export',str(source),'--corrections',str(edits)]))
    subocr.export(subocr.parser().parse_args(['export',str(source)]))
    data=json.loads(source.read_text())
    assert data['cues'][0]['text']=='Come quickly'
    assert data['raw_cues'][0]['text']=='Come quick!y'
    assert len(data['corrections'])==1
    assert 'reviewed_text_correction' in data['cues'][0]['flags']


def test_correction_rejects_mismatched_original_before_writing(tmp_path):
    source=tmp_path/'scan.json'; raw=[cue(1,2,'Hello')]
    source.write_text(json.dumps(dict(primary_language='en', start=0, end=3,
        stats={'max_frame_duration':.04}, cues=raw, raw_cues=raw, repairs=[])))
    edits=tmp_path/'edits.json'
    edits.write_text(json.dumps([dict(language='en',start=1,original='Wrong original',text='Goodbye')]))
    original=source.read_text()
    with pytest.raises(ValueError,match='exactly one'):
        subocr.export(subocr.parser().parse_args(['export',str(source),'--corrections',str(edits)]))
    assert source.read_text()==original
