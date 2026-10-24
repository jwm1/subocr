# Validating subtitle extraction

Validate file structure, text recognition and timing separately. A subtitle file can be internally consistent while containing misread text or false cue boundaries.

## Structural checks

Run the validator on an extraction record or an exported version of that record:

```bash
subocr --json validate outputs/sample.json
```

The validator checks positive cue durations, ordered non-overlapping intervals within each track, nonempty text, SRT/VTT agreement with the saved JSON, and the presence of referenced evidence images. For multiple tracks, it also checks the bilingual union output.

These checks establish that the files agree with the extraction record. They do not establish that the record matches the video. Standalone translation JSON has a different schema and is not an extraction record.

## Text review

Choose source frames independently of OCR output. Fixed offsets within each sample are useful because they include both visible captions and blank frames. Also inspect difficult text, names, short cues and low-confidence readings.

Transcribe the visible captions before comparing them with the extracted track active at each frame's presentation timestamp. Record blank-frame agreement separately from text agreement. Reviewing only recognized cues cannot reveal captions that OCR missed entirely.

For an exact-text comparison, state how whitespace, punctuation and character variants are treated. Preserve the distinction between simplified and traditional characters unless script conversion is an explicit part of the evaluation. Missing characters and equivalent script variants should remain distinguishable in the findings.

A fixed-frame spot check measures agreement at those checkpoints. It does not measure complete-caption recall or overall accuracy across the video.

## Reference subtitle comparison

Search the input directory and its subdirectories for candidate subtitle files. Inspect their dialogue content to establish the language; filenames and occasional Chinese credits can be misleading. Check whether the video contains an embedded subtitle stream as well.

Before computing text errors, establish that the reference describes the same edit of the video and that its timing is aligned. Differences in opening sequences, playback speed, cuts and caption segmentation can invalidate a direct cue-by-cue comparison. Match corresponding content before comparing text.

An English translation is not a Chinese transcription reference. If no suitable reference exists, report that limitation and use direct source-frame review. Do not report a reference-based character error rate from translated or unmatched subtitles.

## Boundary review

Inspect frames immediately before and at selected cue starts and ends. The source repository provides a helper for these contact sheets:

```bash
.venv/bin/python scripts/audit_boundaries.py \
  outputs/sample.json --language zh --count 12 \
  --output outputs/boundaries.jpg
```

The helper saves a contact sheet, individual source crops and a JSON index. Check whether each boundary follows an actual caption appearance, disappearance or text change. Use the full-size crops if the contact sheet is difficult to read.

Distinguish clip truncation from a detected caption transition. A caption may already be visible when extraction starts, or remain visible when it ends. Also check for false internal boundaries: recognition fluctuations can split one unchanged on-screen caption into several intervals.

For a short difficult section, compare sampled extraction with `--every-frame`. Agreement between the two methods supports that section's sampling behavior; it does not prove that either method recognized every character correctly.

## Bilingual checks

Review both languages independently. They may change at different times or have different numbers of cues. A bilingual file should reflect the union of their boundaries and remove a language when its own cue ends.

Inspect all rows of multiline captions and verify that each crop excludes the other language. Automatic layout detection can miss an infrequent second row or a later layout change. Preview frames from several parts of the video.

Imported tracks retain their supplied timing. Their confidence value is a serialization placeholder, not an OCR measurement. Translated tracks inherit the source cue boundaries and require a separate language-quality review.

## Interpreting flags and confidence

Flags identify low confidence, short intervals, temporal repairs, similar neighboring readings and possible clipping at the extraction range. A flagged cue may be correct. An unflagged cue may contain an error. Flag counts are not error rates.

Review pages show the first recognized frame of each cue. That image alone does not establish that its entire interval is correct. Use boundary evidence for timing and independently sampled frames to look for omissions.

Known failure modes include dropped characters, unstable numeric captions, traditional/simplified substitutions, spacing errors, background text, and false boundaries caused by changing OCR readings. Calligraphic fonts and low-contrast outlines can be particularly difficult. Check any proposed crop, threshold or enlargement change against held-out source frames before adopting it generally.

## Reproducibility

Keep the extraction JSON, raw readings, correction manifest and evidence images with the subtitle files. Record the tool version, crop settings, sampling and refinement modes, language models, and tested time ranges. Retain the original outputs when evaluating a change.

Separate cold-run timing from runs that reuse OCR or translation caches. Record interruptions and resumed work rather than treating the resulting runtime as a cold benchmark.

The automated regression suite covers variable-rate timestamps, blank gaps, adjacent caption changes, temporal repair constraints, subtitle serialization, bilingual boundaries, correction matching, track imports, layout separation and batch resume checks. Run it from the project directory:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

The offline editor also has JavaScript interaction tests. Those tests exercise editing logic; visual rendering and video playback depend on the browser and codec support.
