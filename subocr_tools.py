"""Small offline discovery/read commands."""
import importlib
import hashlib
import json

import subocr


def doctor(args):
    dependencies = {}
    for name in ("av", "cv2", "numpy", "onnxruntime", "rapidocr_onnxruntime"):
        try:
            module = importlib.import_module(name)
            dependencies[name] = dict(available=True, version=getattr(module, "__version__", "installed"))
        except ImportError as exc:
            dependencies[name] = dict(available=False, error=str(exc))
    model = subocr.EN_MODEL
    checksum = hashlib.sha256(model.read_bytes()).hexdigest() if model.is_file() else None
    model_valid = checksum == "e8770c967605983d1570cdf5352041dfb68fa0c21664f49f47b155abd3e0e318"
    healthy = all(d["available"] for d in dependencies.values()) and model_valid
    return dict(version=subocr.VERSION, healthy=healthy, mode="offline OCR", auth="not required",
                dependencies=dependencies, english_model=dict(path=str(model), available=model.is_file(), sha256=checksum, checksum_valid=model_valid),
                work_dir=subocr.DEFAULT_WORK)


def inspect_document(args):
    data = json.loads(args.input.read_text(encoding="utf-8"))
    if args.raw:
        return data
    if args.offset < 0 or not 1 <= args.limit <= 10000:
        raise ValueError("offset must be nonnegative; limit must be 1–10000")
    language = args.language or data.get("primary_language", "zh")
    tracks = data.get("tracks", {data.get("primary_language", "zh"): data})
    if language not in tracks:
        raise ValueError(f"No {language} track")
    cues = tracks[language]["cues"]
    return dict(language=language, total=len(cues), offset=args.offset,
                next_offset=args.offset+args.limit if args.offset+args.limit < len(cues) else None,
                cues=cues[args.offset:args.offset+args.limit])
