#!/usr/bin/env python3
"""Save before/at frames for evenly spaced OCR cues, for human timing review."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subocr import frames, save_json


def main():
    from PIL import Image, ImageDraw
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--language", choices=("zh", "en"), help="Track to audit in a bilingual scan")
    args = p.parse_args()
    data = json.loads(args.input.read_text())
    language = args.language or data.get("primary_language", "zh")
    cues = data["tracks"][language]["cues"] if "tracks" in data else data["cues"]
    roi = data["config"].get("tracks", {}).get(language, data["config"]["roi"])
    if not cues or args.count < 1:
        p.error("Need at least one cue and a positive count")
    indices = sorted(set(round(i * (len(cues)-1) / max(args.count-1, 1)) for i in range(args.count)))
    canvas = Image.new("RGB", (1600, len(indices)*155), "white")
    draw = ImageDraw.Draw(canvas)
    audit = []
    for row, idx in enumerate(indices):
        cue = cues[idx]
        draw.text((5, row*155), f'Cue {cue["id"]} | {cue["start"]:.3f} - {cue["end"]:.3f}', fill="black")
        evidence = dict(id=cue["id"], text=cue["text"], start=cue["start"], end=cue["end"], frames=[])
        for side, boundary in enumerate([cue["start"], cue["end"]]):
            samples = list(frames(data["config"]["video"], max(0, boundary-.15), boundary+.1, roi))
            before = [x for x in samples if x[0] < boundary-1e-7]
            after = [x for x in samples if x[0] >= boundary-1e-7]
            for offset, sample in enumerate([before[-1] if before else None, after[0] if after else None]):
                if sample is None:
                    continue
                t, _, _, crop = sample
                x, y = (side*2+offset)*400, row*155+25
                label = ("start" if side == 0 else "end") + (" before" if offset == 0 else " at")
                # Center display is sufficient for these single-line captions;
                # preserve the full ROI in the evidence PNGs.
                name = f'{args.output.stem}.cue{cue["id"]}.{side}.{offset}.png'
                args.output.parent.mkdir(parents=True, exist_ok=True)
                im = Image.fromarray(crop[:, :, ::-1])
                im.save(args.output.parent / name)
                im.thumbnail((395, 95))
                canvas.paste(im, (x, y+20))
                draw.text((x+4, y), f'{label} {t:.3f}s', fill="black")
                evidence["frames"].append(dict(label=label, time=t, image=name))
        audit.append(evidence)
    canvas.save(args.output)
    save_json(args.output.with_suffix(".json"), audit)
    print(args.output)


if __name__ == "__main__":
    main()
