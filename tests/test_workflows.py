import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

import subocr
from subocr_detect import infer_regions
from subocr_tracks import read_subtitles, import_track
from subocr_review import build_review
from subocr_batch import run as run_batch


def document(tmp_path):
    cues = [dict(id=1, start=1., end=2., text="你好", confidence=.99, observations=1, flags=[]),
            dict(id=2, start=3., end=4., text="再见", confidence=.99, observations=1, flags=[])]
    track = dict(cues=cues, raw_cues=copy.deepcopy(cues), repairs=[])
    data = dict(schema_version=2, start=0, end=5, primary_language="zh", config={},
                stats=dict(max_frame_duration=.04), tracks={"zh": track}, **track)
    path = tmp_path/"input.json"
    subocr.save_json(path, data)
    return path, data


def test_detect_ignores_static_logos_and_separates_languages():
    observations = []
    for i in range(12):
        for lang, text, center in [("zh",f"中文{i}",.85),("en",f"English {i}",.93),("en","STATION",.6)]:
            observations.append(dict(language=lang, text=text, cy=center, sample=i,
                                     bounds=[.2,center-.025,.8,center+.025]))
    regions, evidence = infer_regions(observations, 12)
    assert list(regions) == ["zh", "en"]
    assert regions["zh"][3] < regions["en"][1]
    assert regions["en"][1] > .8
    assert evidence["zh"]["sample_support"] == 12


def test_detect_refuses_unsubstantiated_layout():
    with pytest.raises(ValueError, match="No recurring"):
        infer_regions([dict(language="zh", text="台标", cy=.8, sample=i, bounds=[.1,.78,.3,.82]) for i in range(20)], 20)


def test_read_srt_vtt_and_reject_overlaps(tmp_path):
    path = tmp_path/"example.vtt"
    path.write_text("WEBVTT\nKind: captions\n\nNOTE note\n\ncue-id\n00:01.000 --> 00:02.000 align:center\n<i>Hello &amp; goodbye</i>\n\n")
    assert read_subtitles(path)[0]["text"] == "Hello & goodbye"
    path.write_text("1\n00:00:01,000 --> 00:00:03,000\nOne\n\n2\n00:00:02,000 --> 00:00:04,000\nTwo\n")
    with pytest.raises(ValueError, match="overlapping"):
        read_subtitles(path)


def test_import_preserves_independent_timing_and_records_source(tmp_path):
    source, data = document(tmp_path)
    path = tmp_path/"en.vtt"
    path.write_text("WEBVTT\n\n00:00:01.500 --> 00:00:03.500\nHello\n")
    prefix = tmp_path/"combined"
    import_track(argparse.Namespace(input=source, subtitles=path, language="en", output=prefix))
    result = json.loads(prefix.with_suffix(".json").read_text())
    assert result["tracks"]["zh"]["cues"] == data["tracks"]["zh"]["cues"]
    en = result["tracks"]["en"]
    assert en["cues"][0]["start"] == 1.5
    assert en["source"]["kind"] == "imported_subtitle"
    text = Path(str(prefix)+".zh-en.srt").read_text()
    assert "00:00:02,000 --> 00:00:03,000\nHello" in text


def test_delete_corrections_are_audited_and_reapply_without_losing_raw(tmp_path):
    source, data = document(tmp_path)
    edits = tmp_path/"edits.json"
    subocr.save_json(edits,[dict(language="zh",start=1,original="你好",delete=True),
                            dict(language="zh",start=3,original="再见",text="再會")])
    prefix=tmp_path/"reviewed"
    subocr.export(subocr.parser().parse_args(["export",str(source),"--corrections",str(edits),"--output",str(prefix)]))
    result = json.loads(prefix.with_suffix(".json").read_text())
    assert [c["text"] for c in result["cues"]] == ["再會"]
    assert len(result["raw_cues"]) == 2 and len(result["corrections"]) == 2
    assert result["cues"][0]["id"] == 1
    subocr.export(subocr.parser().parse_args(["export",str(prefix.with_suffix(".json"))]))
    assert json.loads(prefix.with_suffix(".json").read_text())["cues"] == result["cues"]
    page = tmp_path/"review.html"
    build_review(argparse.Namespace(input=prefix.with_suffix(".json"),output=page))
    payload=json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',page.read_text(),re.S)[1])
    assert [c["original"] for c in payload["records"]] == ["你好","再见"]
    assert payload["corrections"] == result["corrections"]


def test_editor_escapes_script_content(tmp_path):
    source, data=document(tmp_path)
    data["raw_cues"][0]["text"]='</script><script>throw Error("x")</script>'
    subocr.save_json(source,data)
    page=tmp_path/"editor.html"
    build_review(argparse.Namespace(input=source,output=page))
    assert '</script><script>throw' not in page.read_text()
    assert '\\u003c/script\\u003e' in page.read_text()


def test_batch_dry_run_is_read_only_and_auto_does_not_send_explicit_roi(tmp_path):
    video=tmp_path/"02-video.mp4";video.touch()
    output=tmp_path/"out"
    args=subocr.parser().parse_args(["batch",str(tmp_path),"--output",str(output),"--auto-roi","--dry-run"])
    plan=run_batch(args)
    assert not output.exists()
    assert "--auto-roi" in plan["episodes"][0]["request"]["options"]
    assert "--roi" not in plan["episodes"][0]["request"]["options"]


def test_json_cli_errors_are_machine_readable(tmp_path):
    for argv in (["--json","inspect",str(tmp_path/"missing.json")],["--json","nonsense"]):
        p=subprocess.run([sys.executable,"-m","subocr",*argv],capture_output=True,text=True)
        assert p.returncode != 0
        assert json.loads(p.stdout)["ok"] is False


def test_batch_resume_skips_finished_work_and_protects_edited_outputs(tmp_path, monkeypatch):
    import subocr_batch
    media=tmp_path/"media";media.mkdir();(media/"02-example.mp4").touch()
    output=tmp_path/"batch"
    _,data=document(tmp_path)
    calls=[]
    class Process:
        pid=12345
        def __init__(self,command,**kwargs):
            calls.append(command)
            prefix=Path(command[command.index("--output")+1])
            subocr.save_extraction(prefix,copy.deepcopy(data))
        def poll(self):return 0
    monkeypatch.setattr(subocr_batch.subprocess,"Popen",Process)
    args=subocr.parser().parse_args(["batch",str(media),"--output",str(output),"--work-dir",str(tmp_path/"cache")])
    assert run_batch(args)["completed"] == 1
    assert run_batch(args)["completed"] == 1
    assert len(calls) == 1
    (output/"ep02.zh.srt").write_text("manually edited")
    with pytest.raises(ValueError,match="changed/missing"):
        run_batch(args)
    assert len(calls) == 1


def test_validate_rejects_changed_subtitle_text(tmp_path):
    from subocr_validate import validate
    _,data=document(tmp_path)
    prefix=tmp_path/"validated"
    subocr.save_extraction(prefix,data)
    assert validate(argparse.Namespace(input=prefix))["valid"]
    subtitle=Path(str(prefix)+".zh.vtt")
    subtitle.write_text(subtitle.read_text().replace("你好","别人"))
    with pytest.raises(ValueError,match="differ from JSON"):
        validate(argparse.Namespace(input=prefix))


def test_detect_refuses_overlapping_language_regions():
    observations=[dict(language=lang,text=f'{lang}{i}',cy=.9,sample=i,bounds=[.2,.87,.8,.93])
                  for i in range(10) for lang in ('zh','en')]
    with pytest.raises(ValueError,match="share a text band"):
        infer_regions(observations,10)


def test_detect_separates_adjacent_lines_with_padded_detector_boxes():
    observations=[]
    for i in range(10):
        observations.extend([
            dict(language='en',text=f'English {i}',cy=.867,sample=i,bounds=[.2,.834,.8,.900]),
        ])
        observations.append(dict(language='zh',text=f'中文{i}',cy=.925,sample=i,bounds=[.2,.888,.8,.958]))
    regions,_=infer_regions(observations,10)
    assert list(regions)==['en','zh']
    assert regions['en'][3]==regions['zh'][1]
    assert .89<regions['en'][3]<.90
