"""Shared converter utilities — hashing, cache guards, image search, path helpers."""

import hashlib
from pathlib import Path

from loguru import logger


def get_file_hash(file_path: Path) -> str:
    """Return the SHA256 hex digest of a file's contents."""
    sha256 = hashlib.sha256()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def get_text_hash(text: str) -> str:
    """Return the SHA256 hex digest of a UTF-8 string (e.g. a Mermaid block)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def should_convert(source: Path, target: Path, force: bool = False) -> bool:
    """Return True if conversion should proceed.

    Checks target existence and source mtime.  For stronger cache guarantees
    use content-hash comparison via the DuckDB store instead.
    """
    if force or not target.exists():
        return True
    return source.stat().st_mtime > target.stat().st_mtime


def find_image(image_name: str, search_dir: Path) -> Path | None:
    """Locate an image file by name within search_dir (recursive).

    Tries relative-path resolution first (handles ../.. traversals),
    then exact match, then common extensions, then recursive glob.
    """
    extensions = [".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp"]

    # Relative path resolution — handles ../../../path/to/image.png
    relative_resolved = (search_dir / image_name).resolve()
    if relative_resolved.exists():
        return relative_resolved

    # Exact match if already has extension
    for ext in extensions:
        if image_name.lower().endswith(ext):
            exact = search_dir / image_name
            if exact.exists():
                return exact
            break

    # Try adding extensions
    for ext in extensions:
        candidate = search_dir / f"{image_name}{ext}"
        if candidate.exists():
            return candidate

    # Recursive stem match
    stem = Path(image_name).stem
    for pattern in ("**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.svg"):
        for img_path in search_dir.glob(pattern):
            if img_path.stem == stem:
                return img_path

    logger.debug(f"Image not found: {image_name} in {search_dir}")
    return None


def sanitize_filename(name: str) -> str:
    """Strip characters that are unsafe for filenames."""
    import re

    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
