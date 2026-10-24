# Subtitle output guide

`--output` selects a filename prefix. The examples here use the generic prefix `outputs/sample`. The actual suffixes depend on the extracted languages and the command used.

## Extraction files

| File or directory | Contents |
|---|---|
| `sample.zh.srt`, `sample.zh.vtt` | Chinese subtitles. |
| `sample.en.srt`, `sample.en.vtt` | English subtitles, when that track is present. |
| `sample.zh-en.srt`, `sample.zh-en.vtt` | Bilingual output when Chinese is the first track. Reversing track order uses `en-zh`. |
| `sample.json` | Extraction settings, source identity, final and raw cues, confidence, flags, repairs and run statistics. |
| `sample.zh.txt`, `sample.en.txt` | Caption text without timestamps, one cue per line. |
| `sample.review.html` | Review page with cue text, flags and evidence images. |
| `sample.zh.review.html`, `sample.en.review.html` | Language-specific review pages. |
| `sample.review/` | Saved caption crops referenced by the record and review pages. |
| `sample.editor.html` | Offline editor, created separately with `subocr review`. |

A monolingual extraction creates files only for its language. Bilingual subtitles split at changes in either track, so their cue count can differ from both monolingual counts. A blank or missing line in one track does not extend that line to match the other track.

## Timestamps

All subtitle timestamps refer to the original video, even when only a short range was extracted. They are not rebased to zero for a standalone clip. Cue ends are exclusive, and times come from decoded frame presentation timestamps.

SRT and VTT use millisecond formatting. This does not provide sub-frame precision or guarantee that an OCR change corresponds to a real caption change.

## Saved records and provenance

The extraction JSON stores separate language tracks. The top-level `cues` field aliases the primary track for compatibility. Raw OCR and temporal repair records allow an export to be rebuilt with or without stabilization.

Imported tracks record their source and retain their supplied timing within the extraction range. Corrections record the original text and replacement. Keep these records when you need to distinguish OCR, imported subtitles and later edits.

## Reviewing and rebuilding

Create an editor, apply downloaded corrections to a new prefix, and check the resulting files:

```bash
subocr review outputs/sample.json

subocr export outputs/sample.json \
  --corrections corrections.json \
  --output outputs/reviewed

subocr --json validate outputs/reviewed.json
```

Open the editor in a browser. Download its corrections before moving the project or clearing browser storage. Selecting a local video enables playback when its format is supported.

A correction file is a JSON list. For example:

```json
[
  {
    "language": "en",
    "start": 12.5,
    "original": "Helo there",
    "text": "Hello there"
  }
]
```

Each correction must match exactly one cue's language, start time within 1 ms, and original text. Use the editor to generate matching entries and deletion records. Export retains raw OCR and records applied changes. Subsequent exports reapply them; corrections outside a selected range are omitted.

Text corrections preserve cue timing. The editor does not provide boundary editing. See [the validation guide](../VALIDATION.md) for inspecting transitions and recognizing false internal boundaries.

## Translation outputs

`translate` writes target-language SRT and VTT, a source-target bilingual SRT, a target-language JSON record and a review page. It preserves the source track's cue boundaries exactly. It does not create a bilingual VTT or a new combined extraction record.

With a separate output prefix, translation does not copy the original source-language subtitle files. Retain them alongside the translated files when sharing both languages. The target-language JSON records translation provenance and has a different schema from extraction JSON.

## Layout and batch outputs

`detect` writes layout JSON, a contact sheet and a `.frames/` directory. These describe proposed caption regions and sampled evidence; they contain no extracted subtitle timeline.

`batch` writes a manifest and per-input logs and results. The manifest records source fingerprints, settings, progress and completed artifact hashes. Read it with `subocr --json status`. Resume an unchanged batch with the original command.

A packaged subtitle archive may contain only playback files and a short readme. Keep extraction JSON and evidence separately if they are not included. Archive integrity checks establish that its members can be read; they do not establish OCR accuracy.

## Moving and sharing results

SRT and VTT files can be used independently in a compatible player. The text files omit timing and are unsuitable as playback subtitles.

Move review pages, extraction JSON and their referenced image directories together, preserving relative paths. Exported records can refer to images beside an earlier extraction, so include those directories too. Source videos are not embedded in the editor; select the local video again after moving it.

OCR and translation caches are working data stored under `--work-dir`. They are separate from final outputs and are useful for resuming work. Retain final records and evidence even if you remove caches.

Confidence and flags help prioritize review. Structural validation checks file consistency; source-frame comparison establishes text and timing quality. Full methods are in [VALIDATION.md](../VALIDATION.md).
