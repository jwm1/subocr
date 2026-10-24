"""Infer recurrent horizontal subtitle bands from sampled video frames."""
import json
import math
from pathlib import Path
import re
import statistics

import subocr


def infer_regions(observations, samples):
    """Group changing text by language and baseline; static logos do not qualify."""
    clusters = []
    for item in sorted(observations, key=lambda x: x["cy"]):
        compatible = [c for c in clusters if c[0]["language"] == item["language"]
                      and abs(sum(x["cy"] for x in c)/len(c)-item["cy"]) < .018]
        if compatible:
            min(compatible, key=lambda c: abs(c[0]["cy"]-item["cy"])).append(item)
        else:
            clusters.append([item])
    required = max(3, math.ceil(samples * .10))
    candidates = []
    for group in clusters:
        frames = len({x["sample"] for x in group})
        unique = len({x["text"] for x in group})
        if frames < required or unique < 3:
            continue
        candidates.append(dict(language=group[0]["language"], support=frames,
                               distinct_texts=unique, group=group,
                               center=sum(x["cy"] for x in group)/len(group)))
    regions, evidence, geometry = {}, {}, {}
    for language in ("zh", "en"):
        choices = [c for c in candidates if c["language"] == language]
        if not choices:
            continue
        best = max(choices, key=lambda c: (c["support"], c["distinct_texts"], c["center"]))
        # Include a recurring second row of the same language, but not distant signs.
        chosen = [c for c in choices if abs(c["center"]-best["center"]) < .07]
        group = [x for c in chosen for x in c["group"]]
        top = max(0, min(x["bounds"][1] for x in group) - .008)
        bottom = min(1, max(x["bounds"][3] for x in group) + .008)
        regions[language] = [.025, round(top, 5), .975, round(bottom, 5)]
        geometry[language] = dict(top_center=min(c['center'] for c in chosen),
                                  bottom_center=max(c['center'] for c in chosen),
                                  height=statistics.median(x['bounds'][3]-x['bounds'][1] for x in group))
        evidence[language] = dict(sample_support=best["support"], total_samples=samples,
                                  distinct_texts=best["distinct_texts"], rows=len(chosen))
    # Separate language bands when padding touches; reject genuinely mixed layouts.
    ordered = sorted(regions, key=lambda k: regions[k][1])
    if len(ordered) == 2:
        upper, lower = [regions[k] for k in ordered]
        if upper[3] > lower[1]:
            # The detector includes glyph margins, then we add 0.008 padding
            # on each side. Do not mistake that padding for mixed languages.
            # Still reject genuinely overlapping/same-baseline script layouts.
            upper_geometry, lower_geometry = [geometry[k] for k in ordered]
            center_gap = lower_geometry['top_center'] - upper_geometry['bottom_center']
            if (upper[3] - lower[1] - .016 > .025
                    or center_gap < .5*min(upper_geometry['height'],lower_geometry['height'])):
                raise ValueError("Languages share a text band; use explicit --track regions after inspecting previews")
            split = round((upper[3]+lower[1])/2, 5)
            upper[3] = lower[1] = split
    if not regions:
        raise ValueError("No recurring, changing subtitle band found. Try more samples or use preview and explicit --roi/--track")
    return {k: regions[k] for k in ordered}, evidence


def detect(args):
    import av
    import cv2
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    if not 6 <= args.samples <= 240 or not 0 <= args.bottom < 1 or args.threads < 1:
        raise ValueError("samples must be 6–240, bottom in [0,1), and threads positive")
    with av.open(str(args.video)) as video:
        if not video.streams.video:
            raise ValueError("Input has no video stream")
        duration = video.duration / av.time_base if video.duration else None
    end = args.start + args.duration if args.duration is not None else duration
    if end is None or end <= args.start or (duration and args.start >= duration):
        raise ValueError("Cannot sample this video range; specify a valid --start/--duration")
    if duration:
        end = min(end, duration)
    # Avoid usual opening/closing credit blocks; report all actual sample times.
    targets = np.linspace(args.start+.05*(end-args.start), args.start+.90*(end-args.start), args.samples)
    engine = RapidOCR(intra_op_num_threads=args.threads, inter_op_num_threads=1,
                      det_limit_type="max", det_limit_side_len=1280, width_height_ratio=-1)
    observations, previews, sampled = [], [], []
    directory = Path(str(args.output)+".frames")
    directory.mkdir(parents=True, exist_ok=True)
    for index, target in enumerate(targets):
        iterator = subocr.frames(args.video, float(target), end, (0, 0, 1, 1))
        try:
            timestamp, _, _, frame = next(iterator)
        except StopIteration:
            continue
        finally:
            iterator.close()
        sampled.append(timestamp)
        h, w = frame.shape[:2]
        offset = round(h*args.bottom)
        result, _ = engine(frame[offset:], use_cls=False)
        for box, text, score in result or []:
            text = subocr.normalize(text)
            han = len(subocr.HAN.findall(text))
            latin = len(re.findall(r"[A-Za-z]", text))
            if score < .65 or (han < 1 and latin < 3):
                continue
            x1, x2 = min(p[0] for p in box)/w, max(p[0] for p in box)/w
            y1, y2 = (min(p[1] for p in box)+offset)/h, (max(p[1] for p in box)+offset)/h
            if y2-y1 > .10 or x2-x1 < (y2-y1)*.5 or abs((x1+x2)/2-.5) > .38:
                continue
            observations.append(dict(sample=index, time=timestamp, text=text, confidence=float(score),
                                     language="zh" if han else "en", cy=(y1+y2)/2,
                                     bounds=[x1,y1,x2,y2]))
        # Keep modest previews, not full video frames.
        small = cv2.resize(frame, (960, round(h*960/w)))
        preview = directory/f"{index+1:03}.jpg"
        if not cv2.imwrite(str(preview), small):
            raise OSError(f"Cannot save {preview}")
        previews.append(str(preview))
    report = dict(schema_version=1, video=str(args.video.resolve()), sample_times=sampled,
                  observations=observations, previews=previews,
                  method="Changing horizontal text recurring at a consistent vertical position; zh/en classification",
                  limitation="Sampled layout inference can confuse recurring signs/credits with captions, miss sparse dialogue or miss later layout changes. Inspect previews.")
    try:
        regions, evidence = infer_regions(observations, len(sampled))
    except ValueError as exc:
        report.update(status="needs_manual_roi", error=str(exc))
        subocr.save_json(str(args.output)+".json", report)
        raise
    report.update(status="detected", tracks=regions, evidence=evidence)
    thumbnails = []
    for name in previews:
        frame = cv2.imread(name)
        h, w = frame.shape[:2]
        for language, region in regions.items():
            x1,y1,x2,y2 = region
            cv2.rectangle(frame,(round(x1*w),round(y1*h)),(round(x2*w),round(y2*h)),(0,255,0),2)
            cv2.putText(frame,language,(round(x1*w),round(y1*h)-3),cv2.FONT_HERSHEY_SIMPLEX,.5,(0,255,0),1)
        cv2.imwrite(name,frame)
        thumbnails.append(cv2.resize(frame,(480,round(h/2))))
    # A single contact sheet of up to 12 evenly spaced previews.
    chosen = [thumbnails[i] for i in np.linspace(0,len(thumbnails)-1,min(12,len(thumbnails)),dtype=int)]
    while len(chosen)%3:
        chosen.append(np.zeros_like(chosen[0]))
    sheet = np.vstack([np.hstack(chosen[i:i+3]) for i in range(0,len(chosen),3)])
    cv2.imwrite(str(args.output)+".jpg",sheet)
    subocr.save_json(str(args.output)+".json", report)
    return dict(tracks=regions, evidence=evidence, report=str(args.output)+".json", preview=str(args.output)+".jpg")
