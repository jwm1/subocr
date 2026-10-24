---
name: subocr
description: Extract burnt-in Chinese and English video subtitles with the local subocr CLI, infer caption regions, batch videos, and apply reviewed OCR corrections. Use for pixel-based subtitle extraction rather than speech transcription.
---

Run `command -v subocr` and `subocr --json doctor` first. OCR runs offline without an API key. The project launcher is a fallback when the installed command is not on PATH.

For a new video, run `detect` and inspect its contact sheet before choosing regions. `extract --auto-roi` combines these steps when sampled inference is appropriate. Detection supports Chinese and English horizontal bands; sparse dialogue, credits and changing layouts may require explicit `--track` regions. Explicit coordinates refer to the entire video frame.

Extraction uses original frame presentation timestamps. The default 6 fps sampling plus linear transition refinement can miss short cues or blank gaps between identical sample endpoints; use `--every-frame` for exhaustive OCR. Do not treat confidence or passing structural tests as proofread accuracy.

Use `review` to make an offline editor. Download its corrections and apply them with `export --corrections` to a new output prefix. Edits match original text, language and time; raw OCR is retained. Preserve accepted outputs unless the user asks to revise them. Use `inspect --raw` to read the complete saved record when the bounded cue view is insufficient.

Directory extraction uses `batch`; inspect `--dry-run` first, then run the same command without that flag. `status batch.json` reports progress. A rerun skips completed results only when source/configuration and artifact hashes agree; changed outputs require a new directory. `--sidecar en` imports matching English VTT files without translating or retiming them. Online `translate` sends cue text to translate-shell's provider and is separate from offline OCR.

```bash
subocr --json detect movie.mp4 \
  --output output/layout

subocr extract movie.mp4 --auto-roi \
  --output output/movie

subocr review output/movie.json \
  --output output/movie.editor.html
```

See the project README for installation, JSON result shapes, batch settings and timing limitations. This repository ships the skill under `skills/subocr`; copy that folder to your Codex skills directory to enable discovery outside this project.
