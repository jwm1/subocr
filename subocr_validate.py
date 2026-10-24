"""Verify serialized tracks and their evidence against the extraction record."""
import json
from pathlib import Path
import re

import subocr


def validate(args):
    path = args.input
    prefix = path.with_suffix("") if path.suffix == ".json" else path
    data = json.loads(Path(str(prefix)+".json").read_text(encoding="utf-8"))
    tracks = data.get("tracks", {data.get("primary_language", "zh"): data})
    all_tracks = {language: track["cues"] for language, track in tracks.items()}
    if len(tracks) > 1:
        all_tracks["-".join(tracks)] = subocr.combine_tracks(all_tracks)
    counts = {}
    for language, cues in all_tracks.items():
        previous = data["start"]
        for cue in cues:
            if not previous-1e-6 <= cue["start"] < cue["end"] <= data["end"]+1e-6 or not cue["text"].strip():
                raise ValueError(f"Invalid interval/text in {language} cue {cue.get('id')}")
            previous = cue["end"]
            for name in cue.get("images", [cue.get("image")]):
                if name and not (prefix.parent/name).is_file():
                    raise ValueError(f"Missing evidence image: {name}")
        for extension in ("srt", "vtt"):
            subtitle = Path(f"{prefix}.{language}.{extension}")
            actual = subtitle.read_text(encoding="utf-8")
            expected = ["WEBVTT", ""] if extension == "vtt" else []
            for i, cue in enumerate(cues, 1):
                expected += [str(i), f"{subocr.stamp(cue['start'],extension=='vtt')} --> {subocr.stamp(cue['end'],extension=='vtt')}", cue["text"].strip(), ""]
            if actual != "\n".join(expected)+"\n":
                raise ValueError(f"Subtitle contents differ from JSON: {subtitle}")
        counts[language] = dict(cues=len(cues), flagged=sum(bool(c.get("flags")) for c in cues))
    return dict(input=str(path), valid=True, tracks=counts, timestamps="match JSON", images="all present")
