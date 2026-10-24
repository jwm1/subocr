#!/usr/bin/env python3
"""Scan a long video in adjacent segments, then merge the subtitle tracks.

Run with the project's Python environment. Extra options are passed to `extract`.
"""
import argparse
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from subocr import seconds


def main():
    import av
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('video', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--parts-dir', type=Path)
    p.add_argument('--work-dir', type=Path, required=True)
    p.add_argument('--jobs', type=int, default=3)
    p.add_argument('--segment-seconds', type=seconds, default=1200)
    p.add_argument('--start', type=seconds, default=0)
    p.add_argument('--duration', type=seconds)
    args, extra = p.parse_known_args()
    if args.jobs < 1 or args.segment_seconds <= 0:
        p.error('jobs and segment-seconds must be positive')
    with av.open(str(args.video)) as source:
        length = source.duration / av.time_base if source.duration is not None else None
    if args.duration is None and length is None:
        p.error('Video duration is unknown; supply --duration')
    end = args.start + args.duration if args.duration is not None else length
    if length is not None:
        end = min(end, length)
    if end <= args.start:
        p.error('The requested range contains no video')
    parts = args.parts_dir or args.output.with_name(args.output.name + '.parts')
    parts.mkdir(parents=True, exist_ok=True)
    count = math.ceil((end-args.start)/args.segment_seconds)
    pending = list(range(count)); running = {}; completed = []
    last_report = 0
    try:
        while pending or running:
            while pending and len(running) < args.jobs:
                index = pending.pop(0)
                start = args.start + index*args.segment_seconds
                stop = min(start + args.segment_seconds, end)
                name = f'part{index+1:02}'
                log_path = parts / f'{name}.log'
                log = log_path.open('w')
                command = [sys.executable, str(ROOT/'subocr.py'), 'extract', str(args.video),
                           '--start', str(start), '--duration', str(stop-start),
                           '--output', str(parts/name), '--work-dir', str(args.work_dir/name), *extra]
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                running[index] = (process, log, log_path)
            for index, (process, log, log_path) in list(running.items()):
                code = process.poll()
                if code is not None:
                    log.close();del running[index]
                    if code:
                        raise RuntimeError(f'{log_path.stem} failed with exit code {code}; see {log_path}. Rerun to reuse cached OCR.')
                    completed.append(parts/f'part{index+1:02}.json')
                    print(f'Completed {index+1}/{count}: {completed[-1]}', flush=True)
            if time.monotonic() - last_report >= 15:
                for _, _, log_path in running.values():
                    lines = [line for line in log_path.read_text(errors='replace').splitlines() if line.startswith('video ')]
                    if lines:
                        print(f'{log_path.stem}: {lines[-1]}', flush=True)
                last_report = time.monotonic()
            if running:
                time.sleep(1)
        subprocess.run([sys.executable, str(ROOT/'subocr.py'), 'merge',
                        *map(str, sorted(completed)), '--output', str(args.output),
                        *(['--no-stabilize'] if '--no-stabilize' in extra else [])], check=True)
    finally:
        for process, log, _ in running.values():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
            log.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        raise SystemExit(1)
