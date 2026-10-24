"""Bounded, resumable directory extraction with a durable status manifest."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import subocr


def fingerprint(path):
    stat = path.stat()
    return dict(path=str(path.resolve()), size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def output_name(path):
    match = re.match(r"^(\d{2})-", path.name)
    if match:
        return "ep" + match[1]
    return re.sub(r"[^\w.-]", "_", path.stem)[:60] + "-" + hashlib.sha256(path.name.encode()).hexdigest()[:8]


def run(args):
    if not 1 <= args.jobs <= 32:
        raise ValueError("jobs must be between 1 and 32")
    if not math.isfinite(args.sample_fps) or args.sample_fps <= 0 or args.threads < 1:
        raise ValueError("sample-fps and threads must be positive")
    if args.duration is not None and args.duration <= 0:
        raise ValueError("duration must be positive")
    if args.max_center_offset is not None and not 0 <= args.max_center_offset <= .5:
        raise ValueError("max-center-offset must be in [0, .5]")
    if args.auto_roi and args.sidecar:
        raise ValueError("Choose automatic burnt-in tracks or imported sidecars; do not combine --auto-roi and --sidecar")
    if args.sidecar == getattr(args, "language", "zh"):
        raise ValueError("The OCR language and imported sidecar language must differ")
    videos = sorted(p for p in args.directory.glob(args.pattern)
                    if p.is_file() and p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov", ".avi")
                    and not any(p.name.startswith(x) for x in args.exclude))
    if not videos:
        raise ValueError("No matching videos")
    names = [output_name(p) for p in videos]
    if len(set(names)) != len(names):
        raise ValueError("Output names collide; use separate batches")
    options = ["--sample-fps", str(args.sample_fps), "--refine", args.refine,
               "--threads", str(args.threads)]
    if not args.auto_roi:
        options += ["--roi", ",".join(map(str, args.roi))]
    if getattr(args, "language", "zh") != "zh":
        options += ["--language", args.language]
    if args.max_center_offset is not None:
        options += ["--max-center-offset", str(args.max_center_offset)]
    if args.auto_roi:
        options += ["--auto-roi"]
    if args.duration is not None:
        options += ["--duration", str(args.duration)]
    plan = []
    for video, name in zip(videos, names):
        sidecar = video.with_suffix(f".{args.sidecar}.vtt") if args.sidecar else None
        if sidecar and not sidecar.is_file():
            raise ValueError(f"Requested sidecar missing: {sidecar}")
        if sidecar:
            from subocr_tracks import read_subtitles
            read_subtitles(sidecar)  # Fail before starting expensive OCR on malformed inputs.
        request = dict(video=fingerprint(video), options=options,
                       sidecar=fingerprint(sidecar) if sidecar else None)
        plan.append(dict(name=name, video=str(video.resolve()), request=request,
                         sidecar=str(sidecar.resolve()) if sidecar else None,
                         prefix=str((args.output/name).resolve()), state="pending"))
    if args.dry_run:
        return dict(jobs=args.jobs, episodes=plan)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output/"batch.json"
    # Hold a kernel lock, so two invocations cannot overwrite the same manifest/cache.
    import fcntl
    lock = (args.output/".batch.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError(f"A batch is already running in {args.output}")
    try:
        return supervise(args, plan, manifest, options)
    finally:
        lock.close()


def artifact_hashes(prefix):
    from subocr_validate import validate
    from argparse import Namespace
    validate(Namespace(input=Path(str(prefix)+".json")))
    files = [Path(str(prefix)+suffix) for suffix in (".json", ".review.html")]
    data = json.loads(files[0].read_text())
    languages = list(data.get("tracks", {data.get("primary_language", "zh"): data}))
    if len(languages) > 1:
        languages.append("-".join(languages))
    files += [Path(f"{prefix}.{language}.{extension}") for language in languages for extension in ("srt", "vtt")]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def supervise(args, plan, manifest, options):
    old = json.loads(manifest.read_text()) if manifest.exists() else {"episodes": []}
    prior = {e["name"]: e for e in old["episodes"]}
    for episode in plan:
        saved = prior.get(episode["name"])
        if saved:
            if saved["request"] != episode["request"]:
                raise ValueError(f"Batch settings/source changed for {episode['name']}; choose a new output directory")
            if saved["state"] == "complete":
                try:
                    valid = artifact_hashes(episode["prefix"]) == saved.get("artifacts")
                except (OSError, ValueError):
                    valid = False
                if not valid:
                    raise ValueError(f"Completed files changed/missing for {episode['name']}; choose a new output directory to preserve edits")
                episode.update(saved)
        elif Path(episode["prefix"] + ".json").exists():
            raise ValueError(f"Output exists without a matching batch record: {episode['prefix']}")
    state = dict(schema_version=1, pid=os.getpid(), state="running", started=time.time(),
                 jobs=args.jobs, episodes=plan)
    def save():
        state["updated"] = time.time()
        subocr.save_json(manifest, state)
    save()
    pending = [e for e in plan if e["state"] != "complete"]
    running = {}
    last_report = 0
    try:
        while pending or running:
            while pending and len(running) < args.jobs:
                episode = pending.pop(0)
                prefix = episode["prefix"]
                # OCR gets a separate prefix when an imported track will be added.
                raw_prefix = prefix + ".ocr" if args.sidecar else prefix
                log_path = Path(prefix + ".log")
                log = log_path.open("a")
                command = [sys.executable, "-m", "subocr", "extract", episode["video"],
                           "--output", raw_prefix, "--work-dir", str(Path(args.work_dir)/episode["name"]), *options]
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                episode.update(state="running", pid=process.pid, log=str(log_path), started=time.time())
                running[episode["name"]] = (episode, process, log, raw_prefix)
                save()
            for name, (episode, process, log, raw_prefix) in list(running.items()):
                code = process.poll()
                if code is None:
                    continue
                log.close()
                del running[name]
                if code:
                    episode.update(state="failed", error=f"Extraction exited {code}; see {episode['log']}")
                else:
                    try:
                        if args.sidecar:
                            import argparse
                            from subocr_tracks import import_track
                            import_track(argparse.Namespace(input=Path(raw_prefix+".json"),
                                         subtitles=Path(episode["sidecar"]), language=args.sidecar,
                                         output=Path(episode["prefix"])))
                        episode.update(state="complete", artifacts=artifact_hashes(episode["prefix"]), finished=time.time())
                        print(f"Completed {name} ({sum(e['state']=='complete' for e in plan)}/{len(plan)})", file=sys.stderr, flush=True)
                    except (ValueError, OSError) as exc:
                        episode.update(state="failed", error=str(exc))
                save()
            if time.monotonic() - last_report >= 30:
                for episode, _, _, _ in running.values():
                    # Read only the log tail; large files do not grow supervisor memory.
                    with open(episode["log"], "rb") as log:
                        log.seek(max(0, os.fstat(log.fileno()).st_size-4096))
                        lines = [s for s in log.read().decode(errors="replace").splitlines() if s.startswith("video ")]
                    if lines:
                        episode["progress"] = lines[-1]
                        print(f"{episode['name']}: {lines[-1]}", file=sys.stderr, flush=True)
                save()
                last_report = time.monotonic()
            if running:
                time.sleep(1)
        state["state"] = "failed" if any(e["state"] == "failed" for e in plan) else "complete"
        save()
        if state["state"] == "failed":
            raise RuntimeError(f"Some episodes failed; see {manifest}. Rerun to resume.")
        return dict(manifest=str(manifest), completed=len(plan))
    finally:
        for episode, process, log, _ in running.values():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
            episode["state"] = "interrupted"
        if state["state"] == "running":
            state["state"] = "interrupted"
        save()


def status(args):
    data = json.loads(args.input.read_text())
    if data["state"] == "running":
        # A file lock works across PID namespaces; checking a foreign PID does not.
        import fcntl
        lock_path = args.input.parent/".batch.lock"
        if not lock_path.exists():
            data["state"] = "interrupted"
        else:
            with lock_path.open("r") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    data["state"] = "interrupted"
                except BlockingIOError:
                    pass
    return data
