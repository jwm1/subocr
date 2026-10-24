#!/usr/bin/env python3
"""Extract Chinese/English burnt-in captions using OCR and presentation timestamps."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata

VERSION = "1.2.1"
OCR_CACHE_VERSION = "1.1.0"  # UI/packaging changes do not invalidate existing OCR.
DEFAULT_WORK = os.environ.get("SUBOCR_WORK_DIR", str(Path.home() / ".cache" / "subocr"))
EN_MODEL = Path(__file__).resolve().parent / "subocr_assets" / "en_PP-OCRv4_rec.onnx"
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]")


def seconds(value):
    try:
        parts = [float(x) for x in str(value).split(":")]
        if not 1 <= len(parts) <= 3 or any(x < 0 for x in parts):
            raise ValueError()
        result = sum(x * 60**i for i, x in enumerate(reversed(parts)))
        if not math.isfinite(result):
            raise ValueError()
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("Use nonnegative seconds or HH:MM:SS.sss")


def roi_value(value):
    try:
        x1, y1, x2, y2 = [float(x) for x in value.split(",")]
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError()
        return x1, y1, x2, y2
    except ValueError:
        raise argparse.ArgumentTypeError("ROI must be x1,y1,x2,y2 fractions between 0 and 1")


def normalize(text):
    text = unicodedata.normalize("NFKC", text).strip()
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)


def track_value(value):
    try:
        language, region = value.split("=", 1)
        if language not in ("zh", "en"):
            raise ValueError()
        return language, roi_value(region)
    except ValueError:
        raise argparse.ArgumentTypeError("Use zh=x1,y1,x2,y2 or en=x1,y1,x2,y2")


def combine_tracks(tracks):
    """Union of independent intervals; never extend text across another track's gap."""
    events = {}
    for language, cues in tracks.items():
        for cue in cues:
            events.setdefault(cue["start"], []).append((language, cue))
            events.setdefault(cue["end"], []).append((language, None))
    times = sorted(events)
    active, result = {}, []
    for start, end in zip(times, times[1:]):
        # Ends precede starts when adjacent cues share a timestamp.
        for language, cue in sorted(events[start], key=lambda e: e[1] is not None):
            active[language] = cue
        selected = [active[k] for k in tracks if active.get(k)]
        if not selected:
            continue
        text = "\n".join(c["text"] for c in selected)
        if result and result[-1]["text"] == text and result[-1]["end"] == start:
            result[-1]["end"] = end
            result[-1]["confidence"] = min(result[-1]["confidence"], *(c["confidence"] for c in selected))
            result[-1]["flags"] = sorted(set(result[-1]["flags"]) | {f for c in selected for f in c.get("flags", [])})
        else:
            result.append(dict(start=start, end=end, text=text,
                               confidence=min(c["confidence"] for c in selected),
                               image=selected[0].get("image", ""),
                               images=[c["image"] for c in selected if c.get("image")],
                               flags=sorted({f for c in selected for f in c.get("flags", [])})))
    for i, cue in enumerate(result, 1):
        cue["id"] = i
    return result


def stamp(t, vtt=False):
    ms = max(0, round(t * 1000))
    secs, ms = divmod(ms, 1000)
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours:02}:{mins:02}:{secs:02}{'.' if vtt else ','}{ms:03}"


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def save_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_subtitles(cues, path, texts=None):
    path = Path(path)
    vtt = path.suffix == ".vtt"
    lines = ["WEBVTT", ""] if vtt else []
    if texts is None:
        texts = [c["text"] for c in cues]
    if len(texts) != len(cues):
        raise ValueError("Translation count must equal cue count")
    previous_end = 0.0
    for i, (cue, text) in enumerate(zip(cues, texts), 1):
        start, end = cue["start"], cue["end"]
        if start < previous_end - 1e-6 or end <= start:
            raise ValueError(f"Invalid/overlapping cue {i}")
        previous_end = end
        if not text.strip():
            raise ValueError(f"Empty text at cue {i}")
        lines.extend([str(i), f"{stamp(start, vtt)} --> {stamp(end, vtt)}",
                      text.strip(), ""])
    atomic_text(path, "\n".join(lines) + "\n")


@dataclass
class Reading:
    text: str = ""
    confidence: float = 0.0
    boxes: list | None = None
    detected_boxes: int = 0
    layout_rejected: bool = False


class Timeline:
    """End-exclusive intervals; a blank frame always ends the current cue."""
    def __init__(self):
        self.cues = []
        self.active = None
        self.last_time = -1.0

    def push(self, t, reading):
        if t < self.last_time:
            raise ValueError("Video timestamps are not monotonic")
        self.last_time = t
        if self.active and self.active["text"] == reading.text:
            self.active["confidence"] = min(self.active["confidence"], reading.confidence)
            self.active["observations"] += 1
            return
        self.finish(t)
        if reading.text:
            self.active = dict(start=t, text=reading.text, confidence=reading.confidence,
                               observations=1)

    def finish(self, t):
        if self.active:
            if t > self.active["start"]:
                self.cues.append({**self.active, "end": t})
            self.active = None


def one_edit_apart(a, b):
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), len(a))
    return a[i:] == b[i+1:]


def stabilize(cues, max_glitch=.20):
    """Repair brief one-edit glitches bracketed by the identical stable caption.

    Never cross a blank gap. Preserve all raw readings and report every repair.
    Adjacent different captions without a matching return remain distinct.
    """
    result, repairs = [], []
    i = 0
    while i < len(cues):
        anchor = dict(cues[i])
        j = i + 1
        while j < len(cues):
            if abs(cues[j-1]["end"] - cues[j]["start"]) > 1e-6:
                break
            if cues[j]["text"] == anchor["text"]:
                if j > i+1 and cues[j]["start"] - cues[i+1]["start"] <= max_glitch + 1e-6:
                    if max(anchor["end"]-anchor["start"], cues[j]["end"]-cues[j]["start"]) >= .20:
                        repairs.append(dict(start=cues[i+1]["start"], end=cues[j]["start"],
                                            text=anchor["text"], readings=[dict(c) for c in cues[i+1:j]]))
                        anchor["end"] = cues[j]["end"]
                        anchor["observations"] = anchor.get("observations", 1) + sum(c.get("observations", 1) for c in cues[i+1:j+1])
                        anchor["confidence"] = min(anchor["confidence"], cues[j]["confidence"])
                        anchor["temporal_repair"] = True
                        i = j
                        j = i + 1
                        continue
                break
            if not one_edit_apart(anchor["text"], cues[j]["text"]):
                break
            if cues[j]["end"] - cues[i+1]["start"] > max_glitch + 1e-6:
                break
            j += 1
        result.append(anchor)
        i += 1
    return result, repairs


def annotate_cues(cues, start, end, frame_duration):
    for i, cue in enumerate(cues, 1):
        cue["id"] = i
        cue["flags"] = []
        if cue["confidence"] < .90:
            cue["flags"].append("low_ocr_confidence")
        if cue["end"] - cue["start"] < .20:
            cue["flags"].append("short_cue_check_for_ocr_flicker")
        if cue.get("temporal_repair"):
            cue["flags"].append("temporal_consensus_repair_check_raw_readings")
        if cue["start"] <= start + frame_duration:
            cue["flags"].append("possibly_clipped_at_start")
        if i == len(cues) and abs(cue["end"] - end) < .001:
            cue["flags"].append("possibly_clipped_at_end")
    for left, right in zip(cues, cues[1:]):
        if (abs(left["end"] - right["start"]) < 1e-6
                and min(left["confidence"], right["confidence"]) < .97
                and one_edit_apart(left["text"], right["text"])):
            for cue in (left, right):
                if "similar_neighbor_check" not in cue["flags"]:
                    cue["flags"].append("similar_neighbor_check")


class OCR:
    def __init__(self, work_dir, identity, confidence=0.70, threads=2, max_boxes=4,
                 language="zh", english_model=None, det_max_side=1280):
        from rapidocr_onnxruntime import RapidOCR
        kwargs = {"rec_model_path": str(english_model or EN_MODEL)} if language == "en" else {}
        self.engine = RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=1,
                               det_limit_type="max", det_limit_side_len=det_max_side,
                               width_height_ratio=-1, **kwargs)
        self.language = language
        self.confidence = confidence
        self.identity = identity
        self.max_boxes = max_boxes
        self.db = sqlite3.connect(Path(work_dir) / "ocr.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS frames (key TEXT PRIMARY KEY, result TEXT)")
        self.calls = self.hits = 0
        self.max_center_offset = None

    def filter_layout(self, reading, crop):
        """Optional centered-caption prior; cached OCR remains unfiltered/reusable."""
        tolerance = getattr(self, "max_center_offset", None)
        if tolerance is None or not reading.text or not reading.boxes:
            return reading
        crop = crop() if callable(crop) else crop
        xs = [p[0] for box in reading.boxes for p in box]
        offset = abs((min(xs) + max(xs)) / 2 - crop.shape[1] / 2) / crop.shape[1]
        if offset > tolerance:
            return Reading(detected_boxes=reading.detected_boxes, layout_rejected=True)
        return reading

    def read(self, pts, crop):
        key = f"{self.identity}:{pts}"
        row = self.db.execute("SELECT result FROM frames WHERE key=?", (key,)).fetchone()
        if row:
            cached = Reading(**json.loads(row[0]))
            count = cached.detected_boxes or len(cached.boxes or [])
            if self.max_boxes and count > self.max_boxes:
                self.hits += 1
                return Reading(boxes=cached.boxes, detected_boxes=count, layout_rejected=True)
            if not cached.layout_rejected:
                self.hits += 1
                return self.filter_layout(cached, crop)
        crop = crop() if callable(crop) else crop
        detected, _ = self.engine(crop, use_cls=False, use_rec=False)
        count = len(detected or [])
        if self.max_boxes and count > self.max_boxes:
            reading = Reading(boxes=detected, detected_boxes=count, layout_rejected=True)
            self.db.execute("INSERT OR REPLACE INTO frames VALUES (?,?)", (key, json.dumps(asdict(reading))))
            self.db.commit()
            self.calls += 1
            return reading
        # Reuse detection boxes rather than running the detector twice.
        if detected:
            import numpy as np
            crops = self.engine.get_crop_img_list(crop, np.asarray(detected, dtype=np.float32))
            recognized, _ = self.engine.text_rec(crops)
            result = [(box, text, score) for box, (text, score) in zip(detected, recognized)]
        else:
            result = []
        kept = []
        for box, text, score in result or []:
            text = normalize(text)
            is_caption = HAN.search(text) if self.language == "zh" else re.search(r"[A-Za-z0-9]", text)
            if score >= self.confidence and is_caption:
                # A horizontal 一 is sometimes excluded from the detected box.
                # Centered captions shift right by half a glyph when their
                # leading 一 is missed. Require that geometry as well as OCR
                # agreement; unrestricted expansion hallucinates background text.
                height = max(p[1] for p in box) - min(p[1] for p in box)
                center_shift = (min(p[0] for p in box) + max(p[0] for p in box) - crop.shape[1]) / 2
                x1 = max(0, int(min(p[0] for p in box) - height))
                x2 = min(crop.shape[1], int(max(p[0] for p in box) + height))
                y1 = max(0, int(min(p[1] for p in box) - 3))
                y2 = min(crop.shape[0], int(max(p[1] for p in box) + 3))
                expanded = None
                if self.language == "zh" and height * .20 < center_shift < height * .80:
                    expanded, _ = self.engine(crop[y1:y2, x1:x2], use_det=False, use_cls=False)
                if expanded:
                    candidate, candidate_score = expanded[0]
                    candidate = normalize(candidate)
                    if candidate_score >= .90 and candidate == "一" + text:
                        text, score = candidate, candidate_score
                        box = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                kept.append((box, text, float(score)))
        # Avoid double-counting a detached glyph covered by an expanded line.
        kept = [item for item in kept if not any(
            item is not other and len(item[1]) < len(other[1]) and item[1] in other[1]
            and min(p[0] for p in other[0]) <= min(p[0] for p in item[0])
            and max(p[0] for p in other[0]) >= max(p[0] for p in item[0])
            and min(p[1] for p in other[0]) <= min(p[1] for p in item[0])
            and max(p[1] for p in other[0]) >= max(p[1] for p in item[0])
            for other in kept)]
        # Arrange rows top-to-bottom and boxes left-to-right within each row.
        rows = []
        for item in sorted(kept, key=lambda b: min(p[1] for p in b[0])):
            box = item[0]
            y = sum(p[1] for p in box) / 4
            height = max(p[1] for p in box) - min(p[1] for p in box)
            if rows and abs(y - rows[-1][0]) < height / 2:
                rows[-1][1].append(item)
            else:
                rows.append((y, [item]))
        separator = " " if self.language == "en" else ""
        text = "\n".join(separator.join(b[1] for b in sorted(items, key=lambda b: b[0][0][0]))
                         for _, items in rows)
        reading = Reading(text, min((x[2] for x in kept), default=0), [x[0] for x in kept], count)
        self.db.execute("INSERT OR REPLACE INTO frames VALUES (?,?)",
                        (key, json.dumps(asdict(reading), ensure_ascii=False)))
        self.db.commit()
        self.calls += 1
        return self.filter_layout(reading, crop)


def frames(video, start, end, roi, lazy=False):
    """Yield actual presentation times, never frame_index / nominal_fps."""
    import av
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        stream.codec_context.thread_count = 2
        origin = (container.start_time or 0) / av.time_base
        if start:
            container.seek(int((start + origin) / stream.time_base), stream=stream,
                           backward=True, any_frame=False)
        fallback_duration = 1 / float(stream.average_rate or 30)
        for frame in container.decode(stream):
            if frame.pts is None:
                raise RuntimeError("Video frame lacks a presentation timestamp")
            t = float(frame.pts * frame.time_base) - origin
            if t < start - 1e-7:
                continue
            if end is not None and t >= end - 1e-7:
                break
            h, w = frame.height, frame.width
            regions = roi if isinstance(roi, dict) else {"single": roi}
            bounds = {k: (round(r[0]*w), round(r[1]*h), round(r[2]*w), round(r[3]*h))
                      for k, r in regions.items()}
            if any(r[2] <= r[0] or r[3] <= r[1] for r in bounds.values()):
                raise ValueError("ROI has no pixels at this video resolution")
            def get_crop(source_frame=frame, coords=bounds):
                image = source_frame.to_ndarray(format="bgr24")
                crops = {k: image[top:bottom, left:right].copy()
                         for k, (left, top, right, bottom) in coords.items()}
                return crops if isinstance(roi, dict) else crops["single"]
            duration = float(frame.duration * frame.time_base) if frame.duration else fallback_duration
            # Keep native frames only for the small pending window. Most frames
            # never need the relatively expensive full-resolution RGB conversion.
            yield t, duration, frame.pts, get_crop if lazy else get_crop()


def prepare_work(path):
    path = Path(path).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(f"Cannot write work directory {path}. Use --work-dir with a writable path. {exc}") from exc
    return path


def extract(args):
    import cv2
    if not args.video.is_file():
        raise ValueError(f"Video does not exist: {args.video}")
    if not math.isfinite(args.sample_fps) or args.sample_fps <= 0 or not 0 <= args.confidence <= 1 or args.threads < 1:
        raise ValueError("sample-fps and threads must be positive; confidence must be in [0,1]")
    if args.duration is not None and args.duration <= 0:
        raise ValueError("duration must be positive")
    if args.max_text_boxes < 0:
        raise ValueError("max-text-boxes must be nonnegative")
    if args.max_center_offset is not None and not 0 <= args.max_center_offset <= .5:
        raise ValueError("max-center-offset must be between 0 and 0.5")
    layout = None
    if getattr(args, "auto_roi", False):
        from subocr_detect import detect
        layout = detect(argparse.Namespace(video=args.video, output=Path(str(args.output)+".layout"),
                        start=args.start, duration=args.duration, samples=32, bottom=.45, threads=args.threads))
    regions = layout["tracks"] if layout else (dict(args.track) if args.track else {args.language: args.roi})
    if args.track and len(regions) != len(args.track):
        raise ValueError("Only one region per language is supported; include all its lines in that region")
    if args.det_max_side < 32:
        raise ValueError("det-max-side must be at least 32")
    model_hash = None
    if "en" in regions:
        if not args.english_model.is_file():
            raise ValueError(f"English model missing: {args.english_model}. See models/README.md")
        model_hash = hashlib.sha256(args.english_model.read_bytes()).hexdigest()
    work = prepare_work(args.work_dir)
    prefix = args.output
    prefix.parent.mkdir(parents=True, exist_ok=True)
    image_dir = prefix.with_name(prefix.name + ".review")
    image_dir.mkdir(exist_ok=True)
    stat = args.video.stat()
    config = dict(version=OCR_CACHE_VERSION, video=str(args.video.resolve()), size=stat.st_size,
                  mtime_ns=stat.st_mtime_ns, roi=next(iter(regions.values())), tracks=regions,
                  confidence=args.confidence, model="RapidOCR-1.4.4-PP-OCRv4",
                  english_model_sha256=model_hash, det_max_side=args.det_max_side, threads=args.threads)
    ocrs, timelines = {}, {}
    try:
        for language, roi in regions.items():
            track_config = {**config, "tracks": None, "roi": roi, "language": language,
                            "english_model_sha256": model_hash if language == "en" else None}
            identity = hashlib.sha256(json.dumps(track_config, sort_keys=True).encode()).hexdigest()
            ocrs[language] = OCR(work, identity, args.confidence, args.threads, args.max_text_boxes,
                                 language, args.english_model, args.det_max_side)
            ocrs[language].max_center_offset = args.max_center_offset
            timelines[language] = Timeline()
        end = args.start + args.duration if args.duration is not None else None
        started = time.monotonic()
        last_log = started
        pending = []
        previous = {k: None for k in regions}
        next_sample = args.start
        frame_count = 0
        first_frame_time = None
        sampled_times = []
        last_end = args.start
        frame_durations = []

        def accept(language, t, reading, crop):
            timeline = timelines[language]
            different = timeline.active is None or timeline.active["text"] != reading.text
            timeline.push(t, reading)
            if reading.text and different:
                filename = f"{language}-{t:.6f}.jpg"
                cv2.imwrite(str(image_dir / filename), crop())
                timeline.active["image"] = f"{image_dir.name}/{filename}"

        def consume_pending():
            if not pending:
                return
            t, duration, pts, crops = pending[-1]
            for language, ocr in ocrs.items():
                crop = lambda: crops(language)
                reading = ocr.read(pts, crop)
                if previous[language] is None or previous[language].text != reading.text:
                    if args.refine == "bisect" and previous[language] is not None:
                        def refine(left, left_reading, right, right_reading):
                            if left_reading.text == right_reading.text:
                                return
                            if right - left == 1:
                                ft, _, _, fc = pending[right]
                                accept(language, ft, right_reading, lambda: fc(language))
                                return
                            middle = (left + right) // 2
                            ft, _, fp, fc = pending[middle]
                            mid_reading = ocr.read(fp, lambda: fc(language))
                            refine(left, left_reading, middle, mid_reading)
                            refine(middle, mid_reading, right, right_reading)
                        refine(-1, previous[language], len(pending)-1, reading)
                    else:
                        for ft, _, fp, fc in pending[:-1]:
                            get_crop = lambda: fc(language)
                            accept(language, ft, ocr.read(fp, get_crop), get_crop)
                accept(language, t, reading, crop)
                previous[language] = reading
            pending.clear()

        for t, duration, pts, source in frames(args.video, args.start, end, regions, True):
            # Decode/convert once even when both language recognizers need this frame.
            def crops(language, source=source, memo={}):
                if "images" not in memo:
                    memo["images"] = source() if callable(source) else source
                images = memo["images"]
                return images[language] if isinstance(images, dict) else images
            frame_count += 1
            if first_frame_time is None:
                first_frame_time = t
            if len(frame_durations) < 10000:
                frame_durations.append(duration)
            last_end = t + duration
            pending.append((t, duration, pts, crops))
            if args.every_frame or t + 1e-7 >= next_sample:
                consume_pending()
                sampled_times.append(t)
                next_sample = t + 1 / args.sample_fps
            now = time.monotonic()
            if now - last_log >= 15:
                counts = ", ".join(f"{k}:{len(v.cues)}" for k, v in timelines.items())
                print(f"video {t:.1f}s | cues {counts} | "
                      f"{sum(o.calls for o in ocrs.values())} OCR calls | "
                      f"{sum(o.hits for o in ocrs.values())} cache hits | elapsed {now-started:.1f}s",
                      file=sys.stderr, flush=True)
                last_log = now
        consume_pending()
        if not frame_count:
            raise ValueError("No video frames in the requested time range")
        effective_end = min(last_end, end) if end is not None else last_end
        tracks = {}
        for language, timeline in timelines.items():
            timeline.finish(effective_end)
            raw = timeline.cues
            cues, repairs = ([dict(c) for c in raw], []) if args.no_stabilize else stabilize(raw)
            annotate_cues(cues, args.start, effective_end, max(frame_durations))
            tracks[language] = dict(cues=cues, raw_cues=raw, repairs=repairs)
    finally:
        for ocr in ocrs.values():
            ocr.db.close()
    primary = next(iter(tracks))
    data = dict(schema_version=2, config=config, start=args.start, end=effective_end,
                stabilized=not args.no_stabilize,
                primary_language=primary, tracks=tracks,
                sample_fps=args.sample_fps, every_frame=args.every_frame, refine=args.refine,
                max_text_boxes=args.max_text_boxes,
                max_center_offset=args.max_center_offset,
                timing="Source presentation timestamps, end-exclusive. Transition refinement: " + args.refine,
                limitation=None if args.every_frame else "A caption or gap shorter than the measured sampling interval (nominal interval plus frame rounding) may be missed when inspected endpoints have identical text. Bisect refinement also skips matching subintervals within detected transitions. Use --every-frame for exhaustive OCR.",
                stats=dict(frames=frame_count, first_frame_time=first_frame_time,
                           ocr_calls=sum(o.calls for o in ocrs.values()),
                           cache_hits=sum(o.hits for o in ocrs.values()),
                           elapsed_seconds=time.monotonic()-started,
                           max_frame_duration=max(frame_durations),
                           max_sample_interval=max((b-a for a,b in zip(sampled_times,sampled_times[1:])), default=0)),
                **tracks[primary])
    update_stats(data)
    if layout:
        data["layout_detection"] = layout
    save_extraction(prefix, data)
    print(json.dumps(data["stats"], indent=2))
    return dict(output=str(prefix), stats=data["stats"])


def update_stats(data):
    data["stats"].update(cues=len(data["cues"]), raw_cues=len(data["raw_cues"]),
                         temporal_repairs=len(data["repairs"]))
    if "tracks" in data:
        data["stats"]["tracks"] = {
            k: dict(cues=len(v["cues"]), raw_cues=len(v["raw_cues"]), temporal_repairs=len(v["repairs"]),
                    flagged_cues=sum(bool(c.get("flags")) for c in v["cues"]))
            for k, v in data["tracks"].items()}


def save_extraction(prefix, data):
    save_json(str(prefix) + ".json", data)
    tracks = data.get("tracks", {data.get("primary_language", "zh"): data})
    for language, track in tracks.items():
        cues = track["cues"]
        for extension in ("srt", "vtt"):
            write_subtitles(cues, str(prefix) + f".{language}.{extension}")
        atomic_text(str(prefix) + f".{language}.txt", "\n".join(c["text"].replace("\n", " ") for c in cues) + "\n")
        if len(tracks) > 1:
            review(str(prefix) + f".{language}", {**data, **track})
    if len(tracks) > 1:
        combined = combine_tracks({k: v["cues"] for k, v in tracks.items()})
        for extension in ("srt", "vtt"):
            write_subtitles(combined, str(prefix) + "." + "-".join(tracks) + f".{extension}")
        review(prefix, {**data, "cues": combined, "repairs": []})
    else:
        review(prefix, data)


def export(args):
    data = json.loads(args.input.read_text(encoding="utf-8"))
    tracks = data.get("tracks", {data.get("primary_language", "zh"): data})
    prefix = args.output or args.input.with_suffix("")
    start = args.start if args.start is not None else data["start"]
    end = args.end if args.end is not None else data["end"]
    if not data["start"] <= start < end <= data["end"]:
        raise ValueError("Export range must lie within the saved scan")
    if (start, end) != (data["start"], data["end"]):
        data["source_scan"] = dict(start=data["start"], end=data["end"], stats=dict(data["stats"]))
        data.update(start=start, end=end)
    no_stabilize = args.no_stabilize or data.get("stabilized") is False
    data["stabilized"] = not no_stabilize
    for track in tracks.values():
        raw = [dict(c, start=max(c["start"], start), end=min(c["end"], end))
               for c in track.get("raw_cues", track["cues"]) if c["end"] > start and c["start"] < end]
        if prefix.parent.resolve() != args.input.parent.resolve():
            for cue in raw:
                if cue.get("image"):
                    cue["image"] = os.path.relpath(args.input.parent / cue["image"], prefix.parent)
        cues, repairs = ([dict(c) for c in raw], []) if no_stabilize else stabilize(raw)
        annotate_cues(cues, data["start"], data["end"], data["stats"]["max_frame_duration"])
        track.update(cues=cues, raw_cues=raw, repairs=repairs)
    edits = json.loads(args.corrections.read_text(encoding="utf-8")) if args.corrections else data.get("corrections", [])
    if not isinstance(edits, list):
        raise ValueError("Corrections must be a JSON list of language, start, original and text objects")
    applied = []
    for edit in edits:
        if not isinstance(edit, dict) or not all(k in edit for k in ("language", "start", "original")):
            raise ValueError("Each correction requires language, start, original and text or delete:true")
        if (not isinstance(edit["start"], (int, float)) or not math.isfinite(edit["start"])
                or not isinstance(edit["original"], str)
                or (edit.get("delete") is not True and (not isinstance(edit.get("text"), str) or not edit["text"].strip()))
                or not isinstance(edit["language"], str)
                or edit["language"] not in tracks):
            raise ValueError(f"Invalid correction: {edit}")
        if not start <= edit["start"] < end:
            continue
        matches = [c for c in tracks[edit["language"]]["cues"]
                   if abs(c["start"] - edit["start"]) < .001 and c["text"] == edit["original"]]
        if len(matches) != 1:
            raise ValueError(f"Correction must match exactly one cue's timestamp and original text: {edit}")
        if edit.get("delete") is True:
            tracks[edit["language"]]["cues"].remove(matches[0])
        else:
            matches[0]["text"] = edit["text"].strip()
            matches[0]["flags"].append("reviewed_text_correction")
        applied.append(edit)
    for track in tracks.values():
        for i, cue in enumerate(track["cues"], 1):
            cue["id"] = i
    if applied:
        data["corrections"] = applied
    else:
        data.pop("corrections", None)
    if "tracks" in data:
        data.update(tracks[data["primary_language"]])
    update_stats(data)
    save_extraction(prefix, data)
    print(f"Exported {sum(len(t['cues']) for t in tracks.values())} cues across {len(tracks)} track(s).")
    return dict(output=str(prefix), stats=data["stats"], corrections=len(applied))


def merge(args):
    """Join adjacent scans of the same video/configuration, repairing cut cues."""
    documents = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in args.inputs]
    documents.sort(key=lambda item: item[1]["start"])
    first = documents[0][1]
    languages = list(first.get("tracks", {first.get("primary_language", "zh"): first}))
    tracks = {k: dict(raw_cues=[], cues=[], repairs=[]) for k in languages}
    boundaries = []
    previous_end = None
    for path, data in documents:
        if data["config"] != first["config"]:
            raise ValueError("Merge requires the same video fingerprint, OCR configuration and track regions")
        if any(data.get(key) != first.get(key) for key in ("sample_fps", "every_frame", "refine", "max_text_boxes", "max_center_offset")):
            raise ValueError("Merge requires matching sampling and refinement settings")
        # Some containers round frame.duration separately from frame PTS (e.g.
        # a 24 fps frame reports 41.625 ms). Allow <=1 ms of that rounding,
        # then restore the real next-frame PTS below. Real gaps/overlaps fail.
        if previous_end is not None and not -1e-6 <= data["start"] - previous_end <= .001:
            raise ValueError("Scans must be adjacent, without overlaps or gaps beyond 1 ms duration rounding")
        current = data.get("tracks", {data.get("primary_language", "zh"): data})
        boundary = data["stats"].get("first_frame_time")
        if boundary is None:
            # Compatibility for older scans; one seek resolves a cut between PTS values.
            boundary = next(frames(data["config"]["video"], data["start"], data["end"],
                                   data["config"]["roi"], True))[0]
        for language in languages:
            raw = tracks[language]["raw_cues"]
            incoming = [dict(c) for c in current[language].get("raw_cues", current[language]["cues"])]
            for cue in incoming:
                if cue.get("image"):
                    cue["image"] = os.path.relpath(path.parent / cue["image"], args.output.parent)
            if raw and previous_end is not None and abs(raw[-1]["end"] - previous_end) < 1e-6:
                raw[-1]["end"] = boundary
                if incoming and abs(incoming[0]["start"] - boundary) < 1e-6 and raw[-1]["text"] == incoming[0]["text"]:
                    right = incoming.pop(0)
                    raw[-1]["end"] = right["end"]
                    raw[-1]["confidence"] = min(raw[-1]["confidence"], right["confidence"])
                    raw[-1]["observations"] += right.get("observations", 1)
            raw.extend(incoming)
        if previous_end is not None:
            boundaries.append(dict(requested=data["start"], first_frame=boundary))
        previous_end = data["end"]
    data = {**first, "tracks": tracks, "end": documents[-1][1]["end"],
            "merged_from": [str(p.resolve()) for p, _ in documents], "merge_boundaries": boundaries}
    data["stats"] = {key: sum(d["stats"].get(key, 0) for _, d in documents)
                     for key in ("frames", "ocr_calls", "cache_hits", "elapsed_seconds")}
    for key in ("max_frame_duration", "max_sample_interval"):
        data["stats"][key] = max(d["stats"].get(key, 0) for _, d in documents)
    data["stats"]["elapsed_seconds_note"] = "Sum of segment processing times; concurrent segments overlap in wall time."
    for track in tracks.values():
        raw = track["raw_cues"]
        cues, repairs = ([dict(c) for c in raw], []) if args.no_stabilize else stabilize(raw)
        annotate_cues(cues, data["start"], data["end"], data["stats"]["max_frame_duration"])
        track.update(cues=cues, repairs=repairs)
    data["primary_language"] = languages[0]
    data.update(tracks[languages[0]])
    update_stats(data)
    save_extraction(args.output, data)
    print(json.dumps(data["stats"], indent=2))
    return dict(output=str(args.output), stats=data["stats"])


def review(prefix, data, translations=None, image_base=None):
    rows = []
    flag_labels = {
        "low_ocr_confidence": "Low OCR confidence",
        "short_cue_check_for_ocr_flicker": "Very short cue: check for OCR flicker",
        "temporal_consensus_repair_check_raw_readings": "Brief OCR glitch repaired; check original readings",
        "possibly_clipped_at_start": "Caption may start before the selected clip",
        "possibly_clipped_at_end": "Caption may continue after the selected clip",
        "similar_neighbor_check": "Similar adjacent captions: check character differences",
        "reviewed_text_correction": "Reviewed text correction; original OCR retained in JSON",
    }
    for i, c in enumerate(data["cues"]):
        translated = html.escape(translations[i]) if translations else ""
        flags = "; ".join(flag_labels.get(f, f) for f in c.get("flags", []))
        repair_readings = [r for repair in data.get("repairs", [])
                           if c["start"] <= repair["start"] < c["end"] for r in repair["readings"]]
        details = ""
        if repair_readings:
            original = "\n".join(f'{stamp(r["start"])} - {stamp(r["end"])}  {r["text"]}' for r in repair_readings)
            details = '<details><summary>Original OCR readings</summary><pre>' + html.escape(original) + '</pre></details>'
        images = []
        for image_path in c.get("images", [c.get("image", "")]):
            if image_path and image_base is not None:
                image_path = os.path.relpath(Path(image_base) / image_path, Path(prefix).parent)
            if image_path:
                images.append(f'<img loading="lazy" src="{html.escape(image_path, quote=True)}">')
        rows.append(f'<tr data-flagged="{1 if flags else 0}"><td>{i+1}<br>{stamp(c["start"])}<br>{stamp(c["end"])}</td>'
                    f'<td>{"<br>".join(images)}</td>'
                    f'<td><pre>{html.escape(c["text"])}</pre><pre>{translated}</pre>'
                    f'<small>OCR {c["confidence"]:.3f} {html.escape(flags)}</small>{details}</td></tr>')
    content = ('<!doctype html><meta charset="utf-8"><title>Subtitle OCR review</title>'
               '<style>body{font:16px system-ui;margin:24px;background:#f7f7f7}table{width:100%;border-collapse:collapse}'
               'td{padding:10px;border-bottom:1px solid #ccc;vertical-align:top}img{width:100%;max-width:900px}'
               'pre{white-space:pre-wrap;font:20px system-ui}small{color:#555}</style>'
               '<h1>Subtitle OCR review</h1><p>Images show the first recognized frame of each cue. '
               'OCR confidence is not a guarantee of correctness. Check names and short cues. '
               'Times refer to the original video.</p>'
               f'<p>{len(data.get("repairs", []))} temporal repairs; original readings are retained in the JSON file.</p>'
               f'<p>{html.escape(data.get("limitation") or "Every video frame was sent to OCR.")}</p>'
               '<label><input type="checkbox" onchange="document.querySelectorAll(\'tr[data-flagged]\').forEach(r => r.hidden = this.checked &amp;&amp; r.dataset.flagged === \'0\')"> Show flagged cues only</label>'
               '<table>' + ''.join(rows) + '</table>')
    atomic_text(str(prefix) + ".review.html", content)


def translate(args):
    if not math.isfinite(getattr(args, "delay", 0)) or getattr(args, "delay", 0) < 0:
        raise ValueError("Translation delay must be finite and nonnegative")
    data = json.loads(args.input.read_text(encoding="utf-8"))
    source_language = getattr(args, "source", None) or data.get("primary_language", "zh")
    if "tracks" in data:
        if source_language not in data["tracks"]:
            raise ValueError(f"No {source_language} track in the input")
        if args.target in data["tracks"] and (args.output is None or args.output.resolve() == args.input.with_suffix("").resolve()):
            raise ValueError("Target already exists as an OCR track. Choose a different --output prefix for translation")
        data = {**data, **data["tracks"][source_language]}
    elif source_language != data.get("primary_language", "zh"):
        raise ValueError("Source language does not match the saved OCR track")
    if source_language == args.target:
        raise ValueError("Source and target languages must differ")
    cues = data["cues"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", args.target):
        raise ValueError("Target must be a language code such as en, fr, or zh-TW")
    work = prepare_work(args.work_dir)
    glossary = {}
    if getattr(args, "glossary", None):
        glossary = json.loads(args.glossary.read_text(encoding="utf-8"))
        if not isinstance(glossary, dict) or any(not isinstance(k, str) or not k
                                                or not isinstance(v, str) or not v
                                                for k, v in glossary.items()):
            raise ValueError("Glossary must map nonempty source terms to nonempty target terms")
    glossary_id = hashlib.sha256(json.dumps(glossary, sort_keys=True).encode()).hexdigest()[:12]
    provider_source = "zh-CN" if source_language == "zh" else source_language
    cache_file = work / f"translations.{provider_source}.{args.target}.{glossary_id}.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    if args.mapping:
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                                                for k, v in mapping.items()):
            raise ValueError("Mapping must be a JSON object of Chinese text to nonempty translated text")
        # Mapping mode is deliberately offline: missing entries are an error.
        missing = [c["text"] for c in cues if c["text"] not in mapping]
        if missing:
            raise ValueError(f"Mapping is missing {len(missing)} cue(s), beginning with {missing[0]!r}")
        texts = [mapping[c["text"]] for c in cues]
        provider = "supplied translation mapping"
    else:
        if not shutil.which("trans"):
            raise RuntimeError("Install translate-shell (trans), or supply --mapping translations.json")
        texts = []
        for i, c in enumerate(cues, 1):
            source = c["text"]
            if source not in cache:
                query = source.replace("\n", " ")
                if glossary:
                    pattern = "|".join(re.escape(term) for term in sorted(glossary, key=len, reverse=True))
                    query = re.sub(pattern, lambda match: glossary[match[0]], query)
                error = None
                for attempt in range(3):
                    try:
                        p = subprocess.run(["trans", "-brief", "-no-ansi", "-no-auto", "-s", provider_source,
                                            "-t", args.target, "--", query],
                                           capture_output=True, text=True, timeout=60)
                        result = p.stdout.strip()
                        if p.returncode or not result or result == source or result.startswith("[ERROR]"):
                            raise RuntimeError(p.stderr.strip() or "Empty/unchanged translation")
                        cache[source] = result
                        save_json(cache_file, cache)
                        error = None
                        break
                    except (RuntimeError, subprocess.TimeoutExpired) as exc:
                        error = exc
                        if attempt < 2:
                            time.sleep(2**attempt)
                if error:
                    raise RuntimeError(f"Translation failed at cue {i}; completed translations cached. {error}")
                time.sleep(args.delay)
            texts.append(cache[source])
            if i % 20 == 0:
                print(f"Translated {i}/{len(cues)}", file=sys.stderr, flush=True)
        provider = "translate-shell default engine (online machine translation)"
    prefix = args.output or args.input.with_suffix("")
    write_subtitles(cues, str(prefix) + f".{args.target}.srt", texts)
    write_subtitles(cues, str(prefix) + f".{args.target}.vtt", texts)
    write_subtitles(cues, str(prefix) + f".{source_language}-{args.target}.srt",
                    [c["text"] + "\n" + t for c, t in zip(cues, texts)])
    save_json(str(prefix) + f".{args.target}.json", dict(provider=provider, target=args.target,
              source=str(args.input), source_language=source_language, glossary=glossary,
              mapping=str(args.mapping) if args.mapping else None,
              cues=[dict(id=c["id"], start=c["start"], end=c["end"],
                                               text=t) for c, t in zip(cues, texts)]))
    review(prefix, data, texts, image_base=args.input.parent)
    print(f"Wrote {len(texts)} translated cues; original timestamps preserved.")
    return dict(output=str(prefix), cues=len(texts), source=source_language, target=args.target)


def preview(args):
    import cv2
    import av
    # A full frame with the selected ROI is easier to inspect than a crop alone.
    with av.open(str(args.video)) as c:
        s = c.streams.video[0]
        origin = (c.start_time or 0) / av.time_base
        c.seek(int((args.at + origin) / s.time_base), stream=s, backward=True)
        for f in c.decode(s):
            if f.time - origin + 1e-7 >= args.at:
                image = f.to_ndarray(format="bgr24")
                h, w = image.shape[:2]
                x1, y1, x2, y2 = args.roi
                cv2.rectangle(image, (round(x1*w), round(y1*h)), (round(x2*w), round(y2*h)), (0,255,0), 3)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                if not cv2.imwrite(str(args.output), image):
                    raise RuntimeError("Could not save preview")
                print(args.output)
                return dict(output=str(args.output))
    raise ValueError("No frame at the requested time")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--json", action="store_true", help="Emit one JSON result; progress goes to stderr")
    commands = p.add_subparsers(dest="command", required=True)
    from subocr_tools import doctor, inspect_document
    d = commands.add_parser("doctor", help="Check the offline OCR runtime and bundled model")
    d.set_defaults(func=doctor)
    i = commands.add_parser("inspect", help="Read saved cues or the complete raw JSON document")
    i.add_argument("input", type=Path)
    i.add_argument("--language", choices=("zh", "en"))
    i.add_argument("--offset", type=int, default=0)
    i.add_argument("--limit", type=int, default=50)
    i.add_argument("--raw", action="store_true", help="Return the entire saved JSON, including provenance/raw OCR")
    i.set_defaults(func=inspect_document)
    from subocr_validate import validate
    val = commands.add_parser("validate", help="Check SRT/VTT contents, intervals and evidence images against saved JSON")
    val.add_argument("input", type=Path, help="Saved JSON or its output prefix")
    val.set_defaults(func=validate)
    from subocr_detect import detect
    d = commands.add_parser("detect", help="Locate recurring Chinese/English caption regions and save previews")
    d.add_argument("video", type=Path)
    d.add_argument("--output", type=Path, required=True, help="Prefix for layout JSON and preview contact sheet")
    d.add_argument("--samples", type=int, default=32)
    d.add_argument("--start", type=seconds, default=0)
    d.add_argument("--duration", type=seconds)
    d.add_argument("--bottom", type=float, default=.45, help="Search from this fraction of frame height to the bottom; 0 searches all")
    d.add_argument("--threads", type=int, default=1)
    d.set_defaults(func=detect)
    from subocr_review import build_review
    r = commands.add_parser("review", help="Create an offline cue editor with images, search and downloadable corrections")
    r.add_argument("input", type=Path)
    r.add_argument("--output", type=Path, help="HTML path; default INPUT.editor.html")
    r.set_defaults(func=build_review)
    import subocr_batch
    b = commands.add_parser("batch", help="Resume bounded directory extraction with a durable manifest")
    b.add_argument("directory", type=Path)
    b.add_argument("--output", type=Path, required=True)
    b.add_argument("--work-dir", default=DEFAULT_WORK)
    b.add_argument("--pattern", default="*")
    b.add_argument("--exclude", action="append", default=[], help="Exclude filenames starting with this prefix")
    b.add_argument("--jobs", type=int, default=3)
    b.add_argument("--threads", type=int, default=1)
    b.add_argument("--sample-fps", type=float, default=6)
    b.add_argument("--refine", choices=("linear", "bisect"), default="linear")
    b.add_argument("--roi", type=roi_value, default=(.08, .875, .92, .95))
    b.add_argument("--language", choices=("zh", "en"), default="zh")
    b.add_argument("--max-center-offset", type=float)
    b.add_argument("--auto-roi", action="store_true")
    b.add_argument("--duration", type=seconds)
    b.add_argument("--sidecar", choices=("en",), help="Import matching .en.vtt sidecars and produce bilingual files")
    b.add_argument("--dry-run", action="store_true", help="Show the plan without writing or extracting")
    b.set_defaults(func=subocr_batch.run)
    s = commands.add_parser("status", help="Read a batch manifest and its latest progress")
    s.add_argument("input", type=Path)
    s.set_defaults(func=subocr_batch.status)
    from subocr_tracks import import_track
    it = commands.add_parser("import-track", help="Add an existing SRT/VTT track, keeping original timestamps")
    it.add_argument("input", type=Path)
    it.add_argument("subtitles", type=Path)
    it.add_argument("--language", choices=("zh", "en"), required=True)
    it.add_argument("--output", type=Path)
    it.set_defaults(func=import_track)
    e = commands.add_parser("extract", help="OCR captions into monolingual and optional bilingual files")
    e.add_argument("video", type=Path)
    e.add_argument("--output", type=Path, required=True, help="Output prefix, without extension")
    e.add_argument("--start", type=seconds, default=0)
    e.add_argument("--duration", type=seconds, help="Omit for the rest of the video")
    e.add_argument("--sample-fps", type=float, default=6)
    e.add_argument("--refine", choices=("linear", "bisect"), default="linear", help="linear: OCR every transition frame; bisect: faster recursive boundary search, skipping matching subintervals")
    e.add_argument("--every-frame", action="store_true", help="OCR every frame, including very short cues/gaps")
    e.add_argument("--no-stabilize", action="store_true", help="Keep brief OCR glitches without temporal consensus repairs")
    e.add_argument("--confidence", type=float, default=0.70)
    e.add_argument("--max-text-boxes", type=int, default=4, help="Ignore dense layouts such as credits; 0 disables this filter")
    e.add_argument("--max-center-offset", type=float, help="Optional centered-caption filter: maximum horizontal offset as a fraction of crop width (e.g. 0.15)")
    e.add_argument("--threads", type=int, default=2)
    e.add_argument("--language", choices=("zh", "en"), default="zh", help="Language for --roi; default zh")
    region_options = e.add_mutually_exclusive_group()
    region_options.add_argument("--track", type=track_value, action="append", help="LANG=x1,y1,x2,y2; repeat for bilingual extraction")
    region_options.add_argument("--auto-roi", action="store_true", help="Infer Chinese/English regions from 32 sampled frames")
    e.add_argument("--english-model", type=Path, default=EN_MODEL, help="English recognition ONNX model")
    e.add_argument("--det-max-side", type=int, default=1280, help="Maximum detection image dimension; recognition uses original pixels")
    region_options.add_argument("--roi", type=roi_value, default=(0.08, 0.875, 0.92, 0.95))
    e.add_argument("--work-dir", default=DEFAULT_WORK)
    e.set_defaults(func=extract)
    x = commands.add_parser("export", help="Rebuild all tracks from saved raw OCR, without decoding again")
    x.add_argument("input", type=Path)
    x.add_argument("--output", type=Path)
    x.add_argument("--start", type=seconds, help="Keep only this original-video range")
    x.add_argument("--end", type=seconds, help="End of the kept original-video range, exclusive")
    x.add_argument("--corrections", type=Path, help="Reviewed JSON edits keyed by language, start and original text; raw OCR is preserved")
    x.add_argument("--no-stabilize", action="store_true")
    x.set_defaults(func=export)
    m = commands.add_parser("merge", help="Join adjacent scans of the same video and OCR configuration")
    m.add_argument("inputs", type=Path, nargs="+")
    m.add_argument("--output", type=Path, required=True)
    m.add_argument("--no-stabilize", action="store_true")
    m.set_defaults(func=merge)
    t = commands.add_parser("translate", help="Translate OCR JSON while preserving timing")
    t.add_argument("input", type=Path)
    t.add_argument("--target", default="en")
    t.add_argument("--source", choices=("zh", "en"), help="Track to translate; defaults to the primary OCR track")
    t.add_argument("--output", type=Path)
    t.add_argument("--mapping", type=Path, help="Offline JSON object mapping original text to translations")
    t.add_argument("--glossary", type=Path, help="JSON mapping of source names/terms to target spellings for online translation")
    t.add_argument("--work-dir", default=DEFAULT_WORK)
    t.add_argument("--delay", type=float, default=0.25)
    t.set_defaults(func=translate)
    v = commands.add_parser("preview", help="Save a frame with the selected caption area outlined")
    v.add_argument("video", type=Path)
    v.add_argument("--at", type=seconds, default=30)
    v.add_argument("--roi", type=roi_value, default=(0.08, 0.875, 0.92, 0.95))
    v.add_argument("--output", type=Path, required=True)
    v.set_defaults(func=preview)
    return p


def main():
    try:
        args = parser().parse_args()
    except SystemExit as exc:
        if exc.code and "--json" in sys.argv:
            print(json.dumps(dict(ok=False, error="Invalid command arguments; see stderr for usage")))
        return exc.code
    try:
        if args.json:
            from contextlib import redirect_stdout
            with redirect_stdout(sys.stderr):
                result = args.func(args)
            print(json.dumps(dict(ok=True, command=args.command, result=result), ensure_ascii=False))
        else:
            result = args.func(args)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, indent=2))
    except (RuntimeError, ValueError, OSError) as exc:
        if args.json:
            print(json.dumps(dict(ok=False, command=args.command, error=str(exc)), ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
