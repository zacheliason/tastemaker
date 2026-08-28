from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
except ImportError:
    register_heif_opener = None


SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}


def normalize(source: Path, destination: Path, max_dimension: int = 1600, quality: int = 85) -> dict:
    if source.suffix.lower() in {".heic", ".heif"} and register_heif_opener:
        register_heif_opener()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        if getattr(image, "is_animated", False):
            image.seek(0)
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        image.save(destination, "JPEG", quality=quality, optimize=True, progressive=True)
    return {
        "source": str(source),
        "output": str(destination),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "width": image.width,
        "height": image.height,
        "format": "JPEG",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize reference images for storage and vision requests")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--max-dimension", type=int, default=1600)
    parser.add_argument("--quality", type=int, default=85)
    args = parser.parse_args()
    if not args.input_dir.is_dir():
        parser.error(f"input directory does not exist: {args.input_dir}")
    records = []
    failures = []
    for source in sorted(args.input_dir.rglob("*")):
        if not source.is_file() or source.suffix.lower() not in SUPPORTED:
            continue
        relative = source.relative_to(args.input_dir).with_suffix(".jpg")
        try:
            records.append(normalize(source, args.output_dir / relative, args.max_dimension, args.quality))
        except Exception as error:
            failures.append({"source": str(source), "error": str(error)})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "manifest.json").write_text(json.dumps({"images": records, "failures": failures}, indent=2) + "\n")
    print(f"normalized: {len(records)}")
    print(f"failures: {len(failures)}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
