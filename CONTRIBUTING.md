# Contributing

Use Python 3.12 for the tested development environment. Node.js is needed for the offline editor interaction test.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ".[dev]"
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python tests/create_editor_fixture.py .test-output/editor.html
node tests/editor_interactions.cjs .test-output/editor.html
python -m build
```

Use synthetic fixtures or material you have permission to share in tests. Keep source videos, extracted subtitle collections, caches and local paths out of commits. For recognition problems, describe the caption layout, language, sampling settings and visible error. A small shareable crop is more useful than a private media path.

Changes to timing, stabilization or track merging should include a regression test that distinguishes the old behavior from the intended result. Preserve raw OCR and correction provenance. Follow the checks in VALIDATION.md when evaluating extraction quality.

Project contributions use GPL-3.0-only. Preserve the license and attribution notices on bundled third-party material.
