#!/usr/bin/env python3
"""Validate completed batch episodes, build editors/index, and package subtitles.

With --watch this follows a running manifest and finishes automatically. It never
edits the extraction records or accepted subtitle files.
"""
import argparse
import html
import json
from pathlib import Path
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import subocr
from subocr_batch import status
from subocr_review import build_review
from subocr_validate import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--cutoffs", type=Path, help="Per-name JSON end times; write separate playback exports")
    args = parser.parse_args()
    cutoffs=json.loads(args.cutoffs.read_text()) if args.cutoffs else None
    folder = args.manifest.parent/"playback" if cutoffs else args.manifest.parent
    folder.mkdir(parents=True,exist_ok=True)
    checked = {}
    last_report = 0
    while True:
        batch = status(argparse.Namespace(input=args.manifest))
        for episode in batch["episodes"]:
            name = episode["name"]
            if episode["state"] != "complete" or name in checked:
                continue
            source = Path(episode["prefix"]+".json")
            if cutoffs:
                saved=json.loads(source.read_text())
                end=min(cutoffs[name]["end"],saved["end"])
                subocr.export(argparse.Namespace(input=source,output=folder/name,start=None,end=end,
                                                 corrections=None,no_stabilize=False))
                source=folder/(name+".json")
            result = validate(argparse.Namespace(input=source))
            build_review(argparse.Namespace(input=source, output=None))
            checked[name] = result
            print(f"Validated and built editor: {name}", flush=True)
        report = dict(state=batch["state"], updated=time.time(), expected=len(batch["episodes"]),
                      validated=len(checked), episodes=checked)
        if cutoffs:
            report["playback_cutoffs"]=cutoffs
        subocr.save_json(folder/"validation.json", report)
        rows = []
        for episode in batch["episodes"]:
            name=episode["name"]
            if name in checked:
                counts=checked[name]["tracks"]
                links=' · '.join(f'<a href="{name}.{language}.srt">{language} SRT</a> <a href="{name}.{language}.vtt">VTT</a>' for language in counts)
                rows.append(f'<tr><td>{name}</td><td>{links}<br><a href="{name}.editor.html">Edit / review</a> · <a href="{name}.review.html">All cue images</a></td><td>'+html.escape(', '.join(f'{lang}: {count["cues"]} cues' for lang,count in counts.items()))+'</td></tr>')
            else:
                rows.append(f'<tr><td>{name}</td><td>{html.escape(episode["state"])}</td><td>{html.escape(episode.get("progress", ""))}</td></tr>')
        content='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subtitle collection</title><style>body{font:16px/1.6 system-ui;max-width:1200px;margin:35px auto;padding:0 20px;color:#163235}table{border-collapse:collapse;width:100%}td{border-bottom:1px solid #ccd9d7;padding:14px;vertical-align:top}a{color:#126d65}</style>
<h1>Subtitles</h1><p>Each track retains its own timing. Bilingual files split at changes in either language. Track provenance is recorded in the extraction JSON. Full-video scans can include title, credit or song text.</p>'''
        if cutoffs:
            content+='<p>Playback editions use the supplied per-input cutoffs, capped at the saved extraction end. Original timestamps are retained; the complete scans are in the parent folder. The ending choices and their evidence are recorded in validation.json.</p>'
        content+=f'<p>{len(checked)} of {len(batch["episodes"])} episodes validated. Batch: {html.escape(batch["state"])}. Reload this page for updates.</p>'
        if batch["state"] == "complete":
            content+='<p><a href="subtitles.zip">Download all subtitle files</a></p>'
        content+='<table><thead><tr><th>Episode</th><th>Files</th><th>Cues / progress</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'
        subocr.atomic_text(folder/"index.html",content)
        if batch["state"] == "complete":
            archive=folder/"subtitles.zip"
            temporary=archive.with_suffix(".zip.tmp")
            with zipfile.ZipFile(temporary,"w",zipfile.ZIP_DEFLATED) as out:
                for episode in batch["episodes"]:
                    prefix=folder/episode["name"] if cutoffs else Path(episode["prefix"])
                    for language in checked[episode["name"]]["tracks"]:
                        for extension in ("srt","vtt"):
                            file=Path(f'{prefix}.{language}.{extension}')
                            out.write(file,file.name)
                out.writestr("README.txt", "Subtitle collection.\nTrack provenance is recorded in the extraction JSON. Original timestamps are preserved independently. Bilingual files use the union of track boundaries.\nSettings: see batch.json requests. Full-video scans may contain sparse title/credit text. Text outside the extraction regions is not included. Imported tracks may include song translations. OCR flags and images are available in the project review pages. This is structural validation, not complete proofreading.\n" + ("These playback editions use supplied per-input cutoffs, capped at the saved extraction end. Cutoffs are recorded in validation.json; complete scans are retained separately.\n" if cutoffs else ""))
                out.writestr("validation.json",json.dumps(report,ensure_ascii=False,indent=2))
            with zipfile.ZipFile(temporary) as check:
                bad=check.testzip()
                if bad:
                    raise ValueError(f"Archive CRC failed for {bad}")
            temporary.replace(archive)
            print(f"Complete: {archive}",flush=True)
            return
        if not args.watch:
            return
        if batch["state"] in ("failed","interrupted"):
            raise RuntimeError(f"Batch is {batch['state']}; resume it, then rerun this finishing command")
        if time.monotonic()-last_report >= 60:
            print(f"Waiting: {len(checked)}/{len(batch['episodes'])} validated",flush=True)
            last_report=time.monotonic()
        time.sleep(15)


if __name__ == "__main__":
    main()
