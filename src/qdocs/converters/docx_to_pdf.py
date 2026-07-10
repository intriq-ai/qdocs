"""DOCX → PDF converter using docx2pdf."""

import logging
from pathlib import Path

from loguru import logger

from qdocs.exceptions import ConversionError

try:
    from docx2pdf import convert as _docx2pdf_convert

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def convert_docx_to_pdf(source: Path, target: Path) -> None:
    """Convert a DOCX file to PDF using docx2pdf.

    Raises:
        ConversionError: If docx2pdf is unavailable or conversion fails.
    """
    if not _AVAILABLE:
        msg = "docx2pdf not installed. Run: uv pip install docx2pdf"
        raise ConversionError(msg)
    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Suppress noisy logging from docx2pdf
        for name in ("docx2pdf", ""):
            lg = logging.getLogger(name)
            old_level, old_prop = lg.level, lg.propagate
            lg.setLevel(logging.CRITICAL)
            lg.propagate = False

        _docx2pdf_convert(str(source), str(target))

        for name in ("docx2pdf", ""):
            lg = logging.getLogger(name)
            lg.setLevel(old_level)
            lg.propagate = old_prop

        logger.debug(f"DOCX→PDF: {source.name} → {target.name}")
    except Exception as exc:
        msg = f"Failed to convert {source.name} to PDF: {exc}"
        raise ConversionError(msg) from exc
