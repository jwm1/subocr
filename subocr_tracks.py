"""Import existing subtitle tracks without retiming or translating them."""
import copy
import html
import json
import os
import re
from pathlib import Path

import subocr


def read_subtitles(path):
    text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION", "WEBVTT")):
            continue
        timing = next((i for i, line in enumerate(lines) if " --> " in line), None)
        if timing is None:
            raise ValueError(f"Unrecognized subtitle block in {path}: {block[:80]}")
        match = re.fullmatch(r"([\d:.,]+)\s+-->\s+([\d:.,]+)(?:\s+.*)?", lines[timing])
        if not match:
            raise ValueError(f"Invalid subtitle timestamp: {lines[timing]}")
        start, end = [subocr.seconds(x.replace(",", ".")) for x in match.groups()]
        caption = html.unescape(re.sub(r"<[^>]*>", "", "\n".join(lines[timing+1:]))).strip()
        if not caption or end <= start or (cues and start < cues[-1]["end"] - 1e-6):
            raise ValueError(f"Empty, overlapping or invalid subtitle cue at {start}: {path}")
        cues.append(dict(id=len(cues)+1, start=start, end=end, text=caption,
                         confidence=1.0, observations=0, flags=[], provenance="imported_subtitle"))
    if not cues:
        raise ValueError(f"No subtitle cues in {path}")
    return cues


def import_track(args):
    data = json.loads(args.input.read_text(encoding="utf-8"))
    primary = data.get("primary_language", "zh")
    if "tracks" not in data:
        data["tracks"] = {primary: {k: copy.deepcopy(data.get(k, [])) for k in ("cues", "raw_cues", "repairs")}}
        data["primary_language"] = primary
    if args.language in data["tracks"]:
        raise ValueError(f"The {args.language} track already exists; choose another input")
    cues = read_subtitles(args.subtitles)
    # Import only the saved scan range; keep original video timestamps.
    cues = [dict(c, start=max(c["start"], data["start"]), end=min(c["end"], data["end"]))
            for c in cues if c["end"] > data["start"] and c["start"] < data["end"]]
    if not cues:
        raise ValueError("Imported track does not overlap the extraction range")
    for i, cue in enumerate(cues, 1):
        cue["id"] = i
    prefix = args.output or args.input.with_suffix("")
    for track in data["tracks"].values():
        for key in ("cues", "raw_cues"):
            for cue in track.get(key, []):
                if cue.get("image"):
                    cue["image"] = os.path.relpath(args.input.parent / cue["image"], prefix.parent)
    data["tracks"][args.language] = dict(cues=cues, raw_cues=copy.deepcopy(cues), repairs=[],
                                         source=dict(kind="imported_subtitle", path=str(args.subtitles.resolve())))
    data.update(data["tracks"][primary])
    subocr.update_stats(data)
    subocr.save_extraction(prefix, data)
    return {"output": str(prefix), "language": args.language, "cues": len(cues)}
