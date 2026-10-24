#!/usr/bin/env python3
"""Validate subtitle artifacts against saved OCR, including independent language tracks."""
import argparse
import json
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subocr import combine_tracks, stamp


def check_cues(cues, data, prefix):
    previous = data['start']
    for cue in cues:
        assert cue['start'] >= previous - 1e-6
        assert cue['start'] < cue['end'] <= data['end'] + 1e-6
        assert cue['text'].strip()
        previous = cue['end']
        if cue.get('image'):
            assert (prefix.parent / cue['image']).is_file(), cue['image']


def check_file(prefix, language, cues, extension, check_text=True):
    path = Path(f'{prefix}.{language}.{extension}')
    contents = path.read_text(encoding='utf-8')
    if extension == 'vtt':
        assert contents.startswith('WEBVTT\n')
    actual = [x for x in contents.splitlines() if ' --> ' in x]
    expected = [f"{stamp(c['start'], extension == 'vtt')} --> {stamp(c['end'], extension == 'vtt')}" for c in cues]
    assert actual == expected, f'{path}: timestamps differ from JSON'
    if check_text:
        blocks = re.split(r'\n\s*\n', contents.strip())
        blocks = [b for b in blocks if ' --> ' in b]
        assert ['\n'.join(b.splitlines()[2:]) for b in blocks] == [c['text'] for c in cues], path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('prefix', type=Path)
    p.add_argument('--target', default='en', help='Expected translated language for legacy single-track examples')
    a = p.parse_args()
    data = json.loads(Path(str(a.prefix)+'.json').read_text())
    tracks = data.get('tracks', {data.get('primary_language', 'zh'): data})
    counts = {}
    for language, track in tracks.items():
        cues = track['cues']
        check_cues(cues, data, a.prefix)
        for ext in ['srt', 'vtt']:
            check_file(a.prefix, language, cues, ext)
        counts[language] = dict(cues=len(cues), flagged=sum(bool(c.get('flags')) for c in cues),
                                repairs=len(track.get('repairs', [])))
    if len(tracks) > 1:
        combined = combine_tracks({k: v['cues'] for k, v in tracks.items()})
        check_cues(combined, data, a.prefix)
        for ext in ['srt', 'vtt']:
            check_file(a.prefix, '-'.join(tracks), combined, ext)
        counts['bilingual'] = len(combined)
    elif a.target not in tracks and Path(f'{a.prefix}.{a.target}.srt').is_file():
        source = next(iter(tracks))
        for lang, ext in [(a.target,'srt'), (a.target,'vtt'), (f'{source}-{a.target}','srt')]:
            check_file(a.prefix, lang, data['cues'], ext, check_text=False)
    assert Path(str(a.prefix)+'.review.html').is_file()
    print(json.dumps(dict(prefix=str(a.prefix), tracks=counts,
                          timestamps='match JSON', images='all present'), indent=2))


if __name__ == '__main__':
    main()
