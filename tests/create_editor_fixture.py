#!/usr/bin/env python3
"""Create synthetic bilingual input for the offline editor interaction test."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from subocr import save_json
from subocr_review import build_review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    tracks = {}
    for language, texts in [('zh', ['你好', '再见']), ('en', ['Hello there', 'Goodbye'])]:
        cues = [dict(id=i+1, start=float(1+2*i), end=float(2+2*i), text=text,
                     confidence=.85 if i else .99, observations=1,
                     flags=['low_ocr_confidence'] if i else [])
                for i, text in enumerate(texts)]
        tracks[language] = dict(cues=cues, raw_cues=deepcopy(cues), repairs=[])
    data = dict(schema_version=2, primary_language='zh', start=0, end=5,
                config={}, tracks=tracks, corrections=[])
    source = args.output.with_suffix('.json')
    save_json(source, data)
    build_review(argparse.Namespace(input=source, output=args.output))
    print(args.output)


if __name__ == '__main__':
    main()
