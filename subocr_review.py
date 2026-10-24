"""Generate a self-contained browser editor; no server or uploads required."""
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shlex
from urllib.parse import quote

import subocr


def build_review(args):
    data = json.loads(args.input.read_text(encoding="utf-8"))
    output = args.output or args.input.with_suffix(".editor.html")
    tracks = data.get("tracks", {data.get("primary_language", "zh"): data})
    records = []
    for language, track in tracks.items():
        raw = track.get("raw_cues", track["cues"])
        baseline = [dict(c) for c in raw] if data.get("stabilized") is False else subocr.stabilize(raw)[0]
        current = {round(c["start"], 6): c for c in track["cues"]}
        for cue in baseline:
            item = dict(language=language, start=cue["start"], end=cue["end"], original=cue["text"],
                        confidence=cue["confidence"], flags=current.get(round(cue["start"], 6), cue).get("flags", []),
                        source=track.get("source", {}).get("kind", "OCR"))
            item["image"] = quote(os.path.relpath(args.input.parent / cue["image"], output.parent), safe="/.") if cue.get("image") else ""
            records.append(item)
    records.sort(key=lambda c: (c["start"], list(tracks).index(c["language"])))
    identity = hashlib.sha256(args.input.resolve().as_posix().encode()+args.input.read_bytes()).hexdigest()[:20]
    command = "subocr export " + shlex.quote(str(args.input.resolve())) + " --corrections corrections.json --output " + shlex.quote(str(args.input.with_suffix("").resolve())+"-reviewed")
    payload = dict(title=args.input.stem, records=records, corrections=data.get("corrections", []),
                   identity=identity, languages=list(tracks), command=command,
                   source_video=data.get("config", {}).get("video", ""))
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = files("subocr_assets").joinpath("review.html").read_text(encoding="utf-8")
    subocr.atomic_text(output, template.replace("__SUBOCR_DATA__", encoded))
    return dict(output=str(output), cues=len(records), mode="offline browser editor")
