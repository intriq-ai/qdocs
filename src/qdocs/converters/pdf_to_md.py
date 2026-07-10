"""PDF → Markdown converter.

Provider selection
------------------
``convert_pdf_to_md`` accepts a *provider* keyword argument:

``"auto"`` (default)
    Checks for a valid AWS MFA session.  If one exists, uses Textract for
    superior OCR quality (handles scanned PDFs, tables, complex layouts).
    Falls back to pdfplumber automatically if no session is found.

``"textract"``
    Forces AWS Textract.  Raises ``ConversionError`` if no valid MFA session
    is present.  Run ``eval $(qmfa)`` to acquire a session first.

``"pdfplumber"``
    Forces pdfplumber (local, no AWS, text-layer PDFs only).

The Textract path uses ``qagents.providers.aws.textract.TextractProvider`` which
renders each page to a PNG via PyMuPDF and calls ``analyze_document`` with the
TABLES feature, producing accurate line-and-table Markdown.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from loguru import logger

from qdocs.exceptions import ConversionError

if TYPE_CHECKING:
    from qdocs.cache import ContentCache

try:
    import pdfplumber

    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False


def convert_pdf_to_md(
    source: Path,
    target: Path,
    *,
    provider: Literal["auto", "textract", "pdfplumber"] = "auto",
    textract_region: str = "eu-west-1",
    ai_format: bool = True,
    ai_model_id: str | None = None,
    extract_figures: bool = False,
    figures_dir: Path | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    cache: ContentCache | None = None,
    pages: str | None = None,
) -> None:
    """Convert a PDF to Markdown.

    Args:
        source:           Path to the source PDF.
        target:           Path for the output Markdown file.
        provider:         Conversion backend — ``"auto"`` (default), ``"textract"``,
                          or ``"pdfplumber"``.  See module docstring for details.
        textract_region:  AWS region for Textract (only used when provider is
                          ``"textract"`` or ``"auto"`` with a valid MFA session).
        ai_format:        Apply AI post-processing after Textract extraction to
                          clean up OCR artifacts and add heading markers (default:
                          True).  Ignored when provider is ``"pdfplumber"``.
        ai_model_id:      Bedrock model ID for AI formatting.  ``None`` uses the
                          default from ``AgentSettings``.
        extract_figures:  Detect and save figure regions as PNG files in a
                          ``figures/`` sub-directory next to *target*, and insert
                          Markdown image references in the output.  Only applies
                          when the Textract provider is used (ignored for
                          pdfplumber).
        cache:            Optional content-addressed cache for Textract and AI
                          format responses.  Pass ``ContentCache()`` to enable.

    Raises:
        ConversionError: If the chosen provider is unavailable or conversion fails.
    """
    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)

    # ── Provider resolution ──────────────────────────────────────────────
    if provider == "textract":
        _convert_via_textract(
            source,
            target,
            region=textract_region,
            ai_format=ai_format,
            ai_model_id=ai_model_id,
            extract_figures=extract_figures,
            figures_dir=figures_dir,
            on_progress=on_progress,
            cache=cache,
            pages=pages,
        )
        return

    if provider == "auto":
        from qdocs.converters.pdf_to_md_textract import check_mfa_session

        valid, mfa_msg = check_mfa_session()
        if valid:
            logger.debug("[pdf→md] auto: Textract selected ({})", mfa_msg)
            try:
                _convert_via_textract(
                    source,
                    target,
                    region=textract_region,
                    ai_format=ai_format,
                    ai_model_id=ai_model_id,
                    extract_figures=extract_figures,
                    figures_dir=figures_dir,
                    on_progress=on_progress,
                    cache=cache,
                    pages=pages,
                )
                return
            except ConversionError as exc:
                logger.warning(
                    "[pdf→md] Textract failed — falling back to pdfplumber: {}", exc
                )
        else:
            logger.debug("[pdf→md] auto: pdfplumber selected ({})", mfa_msg)

    # ── pdfplumber path ──────────────────────────────────────────────────
    _convert_via_pdfplumber(source, target, pages=pages)


def _convert_via_textract(
    source: Path,
    target: Path,
    *,
    region: str,
    ai_format: bool = True,
    ai_model_id: str | None = None,
    extract_figures: bool = False,
    figures_dir: Path | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    cache: ContentCache | None = None,
    pages: str | None = None,
) -> None:
    """Delegate to the Textract converter."""
    from qdocs.converters.pdf_to_md_textract import convert_pdf_to_md_textract

    convert_pdf_to_md_textract(
        source,
        target,
        region=region,
        ai_format=ai_format,
        ai_model_id=ai_model_id,
        extract_figures=extract_figures,
        figures_dir=figures_dir,
        on_progress=on_progress,
        cache=cache,
        pages=pages,
    )


def _convert_via_pdfplumber(
    source: Path, target: Path, *, pages: str | None = None
) -> None:
    """Convert a PDF to Markdown via pdfplumber (text-layer extraction)."""
    if not _PDF_AVAILABLE:
        msg = "pdfplumber not installed. Run: uv pip install pdfplumber"
        raise ConversionError(msg)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [f"# {source.stem}\n"]

        with pdfplumber.open(source) as pdf:
            total_pages = len(pdf.pages)
            if pages:
                from qdocs.converters.pdf_to_md_textract import _parse_page_spec

                _page_filter: set[int] = _parse_page_spec(pages, total_pages)
            else:
                _page_filter = set()
            for page_num, page in enumerate(pdf.pages, 1):
                if _page_filter and page_num not in _page_filter:
                    continue
                text = page.extract_text()
                if text:
                    if total_pages > 1:
                        lines.append(f"\n## Page {page_num}\n")
                    lines.append(f"{text}\n")
                for table in page.extract_tables() or []:
                    if not table:
                        continue
                    header = [str(c or "") for c in table[0]]
                    lines.append(f"| {' | '.join(header)} |\n")
                    lines.append(f"| {' | '.join(['---'] * len(header))} |\n")
                    lines.extend(
                        f"| {' | '.join(str(c or '') for c in row)} |\n"
                        for row in table[1:]
                    )

        target.write_text("\n".join(lines), encoding="utf-8")
        logger.debug(f"PDF→MD: {source.name} → {target.name}")
    except Exception as exc:
        msg = f"Failed to convert {source.name} to MD: {exc}"
        raise ConversionError(msg) from exc
