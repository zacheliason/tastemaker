from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path

from .storage import SupabaseStorage


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}


def import_directory(conn, root: str | Path, label: str = "like") -> int:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Reference directory does not exist: {root}")
    if label not in {"like", "dislike"}:
        raise ValueError("Reference label must be like or dislike")
    imported = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        relative = path.relative_to(root)
        category = {"pinterest_art": "art", "pinterest_clothes": "clothing", "pinterest_home_decor": "home_decor"}.get(relative.parts[0])
        if not category:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        conn.execute("""
            insert into taste_references
              (category, label, image_path, source_image_path, image_sha256, mime_type)
             values (%s, %s, %s, %s, %s, %s)
            on conflict (image_sha256) do update set
              label = excluded.label,
              image_path = excluded.image_path,
              source_image_path = excluded.source_image_path,
              mime_type = excluded.mime_type
        """, (category, label, str(path), str(path), digest, mimetypes.guess_type(path.name)[0]))
        imported += 1
    return imported


def upload_directory(conn, root: str | Path, bucket: str, storage: SupabaseStorage) -> int:
    """Upload a normalized reference tree and reconcile rows by manifest source path."""
    root = Path(root).expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Normalization manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    uploaded = 0
    for record in manifest.get("images", []):
        output = Path(record["output"]).expanduser().resolve()
        if not output.is_file():
            raise RuntimeError(f"Normalized image does not exist: {output}")
        relative = output.relative_to(root).as_posix()
        storage_path = f"references/{relative}"
        storage.upload(bucket, storage_path, output, "image/jpeg")
        source_path = str(Path(record["source"]).expanduser().resolve())
        with conn.cursor() as cur:
            cur.execute("""
                update taste_references
                   set image_path = %s, source_image_path = %s,
                       storage_bucket = %s, storage_path = %s,
                       mime_type = 'image/jpeg', image_width = %s, image_height = %s
                 where source_image_path = %s or image_path = %s
            """, (storage_path, source_path, bucket, storage_path,
                   record.get("width"), record.get("height"), source_path, source_path))
            if cur.rowcount:
                uploaded += 1
    return uploaded
