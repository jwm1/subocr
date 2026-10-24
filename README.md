# Subtitle OCR

`subocr` extracts burnt-in Chinese and English captions from video pixels. It writes SRT and WebVTT files, preserves original video timestamps, and saves OCR readings and images for review. Each language can have its own caption region and timing. Extraction runs locally on the CPU without an API key or network access.

Use [the output guide](outputs/README.md) for file formats, corrections and portability. The [validation guide](VALIDATION.md) explains how to check text and timing.

## Installation

Python 3.10 or newer is required; Python 3.12 is the tested runtime. Run these commands from the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
subocr --json doctor
```

Activating the environment puts `subocr` on the current shell's PATH. The project also provides `./subocr`, which uses `.venv` by default. Set `SUBOCR_PYTHON` to use another interpreter.

A wheel can be installed with `python -m pip install dist/subocr-1.2.1-py3-none-any.whl`. The wheel includes the English recognition model and editor template. Chinese OCR models come with the RapidOCR dependency. Model provenance is documented in [models/README.md](models/README.md).

`bash scripts/install_local.sh` installs into the existing project environment and links the command into `~/.local/bin`. It refuses to replace an existing command. Pass a different bin directory as its first argument if needed. Set `SUBOCR_OFFLINE_BUILD=1` to use already-installed build dependencies without downloading them.

## Start with a short extraction

The filenames below are placeholders. Replace `input.mp4` with your input path and quote paths containing spaces. `--output` takes a filename prefix without an extension.

```bash
subocr detect input.mp4 \
  --samples 32 --output outputs/layout

subocr extract input.mp4 --auto-roi \
  --start 00:05:00 --duration 00:01:00 \
  --output outputs/sample

subocr review outputs/sample.json

subocr --json validate outputs/sample.json
```

Inspect the layout contact sheet before relying on the extraction. `--auto-roi` detects the caption regions for the selected range. Omit `--duration` to scan from the start time to the end of the video. Times in the resulting subtitles still refer to the original video.

OCR and translation caches use `$SUBOCR_WORK_DIR`, or `~/.cache/subocr` when that variable is unset. Supply `--work-dir` to choose another writable directory. Final files go to the output prefix you specify.

## Caption regions and languages

Automatic detection looks for changing horizontal text at recurring positions. By default it samples 32 frames between 5% and 90% of the selected range and searches the lower 55% of the frame. Use `detect --bottom 0` to search the whole frame. Sparse dialogue, signs, credits and changing layouts can require manual regions.

Coordinates use `x1,y1,x2,y2`, expressed as fractions of the entire frame. A region should include every row of its language and exclude the other language. The default region is a narrow strip near the bottom; inspect it before using it on a new layout.

The following coordinates are illustrative. Adjust them after inspecting a preview:

```bash
subocr preview input.mp4 \
  --at 00:05:30 --roi 0.05,0.78,0.95,0.88 \
  --output outputs/crop.jpg

subocr extract input.mp4 \
  --track zh=0.05,0.78,0.95,0.88 \
  --track en=0.05,0.88,0.95,0.98 \
  --start 00:05:00 --duration 60 \
  --output outputs/bilingual
```

For a single language, use `--language zh` or `--language en` with `--roi`. Explicit `--track` order sets the line order in bilingual output. The supplied recognizers support Chinese and English.

Both languages retain their own cue boundaries. Bilingual output splits whenever either track changes; it does not assume matching cue counts or hold a missing line until the other language finishes.

## Review and corrections

Extraction creates a review page with cue images. The `review` command creates an offline editor with search, language and flag filters, text editing, deletion, and correction import/export. Open the generated `.editor.html` in a browser. You can select a local video for playback if the browser supports its codec.

The editor saves drafts in browser storage when available. Download the corrections to keep a portable copy, then apply them to a new output prefix:

```bash
subocr export outputs/sample.json \
  --corrections corrections.json \
  --output outputs/reviewed

subocr --json validate outputs/reviewed.json
```

Edits match a cue's language, start time and original text. Raw OCR remains in the saved record. Text edits preserve timing; deleting a cue removes its interval. The editor does not adjust cue boundaries. See [the output guide](outputs/README.md) for the correction schema.

## Existing subtitles and translation

Import an existing SRT or VTT track to retain its text and independent timing:

```bash
subocr import-track outputs/sample.json reference.en.vtt \
  --language en --output outputs/with-reference
```

The importer removes style tags, decodes text entities, rejects overlapping or malformed cues, and clips the imported track to the extraction range. It refuses to replace a language already present in the record.

Translation is a separate operation. Offline mapping mode requires a JSON object mapping each complete source cue to a nonempty translation:

```bash
subocr translate outputs/sample.json \
  --source zh --target en --mapping translations.json \
  --output outputs/translated
```

Without `--mapping`, translation uses the `trans` command from translate-shell and sends cue text to its online provider. `--glossary glossary.json` substitutes source terms before online translation. Translation preserves source cue boundaries, caches successful results and rejects missing output. Review names and sentences split across cues for contextual errors.

## Directory batches

`batch` records source fingerprints, options and progress in a manifest. Rerunning an unchanged batch reuses OCR caches and skips completed results after checking their hashes. Changed sources, settings or completed output files require a new output directory.

```bash
subocr --json batch media --auto-roi \
  --output outputs/batch --jobs 2 --threads 1 --dry-run

subocr batch media --auto-roi \
  --output outputs/batch --jobs 2 --threads 1

subocr --json status outputs/batch/batch.json
```

Use `--pattern` to select filenames and repeat `--exclude` to omit filename prefixes. A batch can import matching English `.en.vtt` sidecars with `--sidecar en`. Use that option with Chinese-only extraction; it cannot replace an English track discovered by OCR.

Keep the number of workers and threads within your CPU and storage capacity. Each video may need a different region, so automatic detection is preferable to sharing an unchecked crop across unrelated layouts.

## Exporting ranges and joining scans

`export` rebuilds subtitles from saved OCR without decoding the video again. A selected range keeps its original timestamps:

```bash
subocr export outputs/sample.json \
  --start 00:05:10 --end 00:05:50 \
  --output outputs/selection

subocr merge outputs/part-a.json outputs/part-b.json \
  --output outputs/combined
```

`merge` requires adjacent ranges from the same video with compatible OCR and sampling settings. It rejects gaps and overlaps, joins captions cut by a segment boundary, and reapplies temporal stabilization. The source repository also includes `scripts/parallel_extract.py` for splitting a video into adjacent segments and merging them after extraction.

## Timing and recognition settings

The default sampler targets 6 fps and selects the next available video frame. The saved JSON reports the maximum observed sample interval. Linear refinement OCRs every frame between sampled readings that differ. `--refine bisect` searches those intervals recursively and can run faster, but skips matching subintervals.

A short caption or blank gap can fall between identical sampled readings. Use `--every-frame` when those events matter. This removes the sampling blind spot, but recognition errors can still split a continuous caption, omit characters or move a detected boundary.

Temporal stabilization repairs brief one-character fluctuations bracketed by identical stable readings, up to 0.20 seconds. Repairs remain flagged and the raw readings are retained. Use `--no-stabilize` with extraction or export to keep raw intervals.

Other controls include:

| Option | Effect |
|---|---|
| `--confidence` | Minimum recognition confidence; default `0.70`. Lower-confidence text may be omitted. |
| `--max-text-boxes` | Reject dense layouts before recognition; default `4`, or `0` to disable. |
| `--max-center-offset` | Reject off-center text groups, as a fraction of crop width. Leave unset for left-aligned captions. |
| `--det-max-side` | Maximum detector image dimension; default `1280`. Recognition uses the original detected regions. |
| `--threads` | CPU threads per OCR process. |

Traditional characters, calligraphic fonts, low contrast, motion blur and background lettering can cause substantial errors. Enlarging an image may leave recognition unchanged. A high confidence score or a successful structural check does not establish text accuracy.

Frame presentation timestamps determine cue timing, including variable-frame-rate video. Cue ends are exclusive. Millisecond formatting does not imply sub-frame precision. Clip boundaries can truncate captions; those cues are flagged.

## Machine-readable commands

Put `--json` before the subcommand. Stdout contains one result envelope, while progress goes to stderr:

```json
{"ok": true, "command": "validate", "result": {}}
```

The `result` fields depend on the command. Failures return a nonzero exit status and an envelope containing `ok: false` and an `error` message. Argument errors may omit `command`. Help and version keep their normal text output.

```bash
subocr --json inspect outputs/sample.json \
  --language zh --limit 20 --offset 0

subocr --json inspect outputs/sample.json --raw
```

`inspect` provides a bounded cue view; `--raw` returns the complete saved record. `doctor` reports runtime and model availability. `batch --dry-run` returns the planned inputs and options without extracting them.

## Development checks

Install development dependencies with `python -m pip install ".[dev]"`. The regression suite covers timestamp handling, variable-rate decoding, caption transitions, temporal repairs, bilingual timelines, corrections, imports and resumable batches. See [VALIDATION.md](VALIDATION.md) for text and boundary review methods.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q

make wheel
```

The companion skill under `skills/subocr` describes the CLI workflow for coding assistants.

## License

Project code and documentation are licensed under GPL-3.0-only. See [LICENSE](LICENSE). The bundled OCR model retains its upstream Apache-2.0 notice; see [NOTICE](NOTICE) and [model provenance](models/README.md).
