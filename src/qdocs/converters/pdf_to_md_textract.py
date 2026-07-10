"""PDF → Markdown converter using AWS Textract + AI post-processing.

Architecture
------------
1. **MFA preflight** — verifies a valid qmfa session is in place before making
   any AWS API calls.  Raises ``ConversionError`` with an actionable message if
   no valid session exists.

2. **Page rendering** — each PDF page is rasterised to a PNG image at 300 DPI
   using PyMuPDF (already a transitive dep via pdf2docx).  This works for both
   text-native and scanned PDFs.

3. **Textract analysis** — per-page ``analyze_document`` call with
   ``FeatureTypes=["TABLES"]`` via ``qagents.providers.aws.textract.TextractProvider``.
   Returns typed ``TextractBlock`` objects.

4. **Markdown assembly** — blocks are sorted by bounding-box Y position
   (reading order).  LINE blocks emit as plain text; TABLE blocks are formatted
   as GFM pipe tables.  LINE blocks that fall inside a TABLE bounding box are
   suppressed to avoid duplication.

5. **AI formatting** (optional, default on) — the assembled Markdown is split
   into chunks and each chunk is cleaned by an LLM (AWS Bedrock via qagents
   settings) to:
   - Remove OCR artifacts (standalone page numbers, watermark fragments).
   - Add proper Markdown heading hierarchy (## / ###).
   - Rejoin sentences fragmented across OCR lines.
   - Fix table cells incorrectly split by Textract.
   - PRESERVE ALL substantive content — no summarisation or data loss.

Usage
-----
    from qdocs.converters.pdf_to_md_textract import (
        convert_pdf_to_md_textract,
        check_mfa_session,
    )

    ok, msg = check_mfa_session()
    if ok:
        convert_pdf_to_md_textract(Path("report.pdf"), Path("report.md"))
        # Disable AI formatting (raw Textract output only):
        convert_pdf_to_md_textract(Path("report.pdf"), Path("report.md"), ai_format=False)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from qdocs.exceptions import ConversionError

if TYPE_CHECKING:
    from qdocs.cache import ContentCache

# ---------------------------------------------------------------------------
# Optional imports — guarded so the module loads even if deps are absent.
# ---------------------------------------------------------------------------
try:
    import fitz as _fitz

    _FITZ_AVAILABLE = True
except ImportError:
    _FITZ_AVAILABLE = False

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def check_mfa_session() -> tuple[bool, str]:
    """Return ``(valid, message)`` for the current AWS MFA session.

    Checks ``MFAService.get_status()`` — reads from env vars first, then the
    local ``~/.intriq/auth_tokens/mfa_session.json`` cache.

    Returns:
        (True, "session valid …")  — ready to call Textract
        (False, "reason …")       — no valid session; caller should
                                    fall back or prompt for ``eval $(qmfa)``
    """
    try:
        from qcli.mfa.service import MFAService

        status = MFAService().get_status()
        if status["valid"]:
            exp = status.get("expiration", "")
            key = status.get("key_suffix", "")
            return (
                True,
                f"valid MFA session  key={key}  expires={exp}  source={status['source']}",
            )
        src = status.get("source", "none")
        exp = status.get("expiration") or "never"
        return (
            False,
            f"MFA session expired or absent (source={src}, expiration={exp}).  "
            f"Run: eval $(qmfa)",
        )
    except Exception as exc:
        return False, f"Could not check MFA session: {exc}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_page_png(page: object, dpi: int = 300) -> bytes:
    """Render a PyMuPDF page to PNG bytes at the given DPI."""
    zoom = dpi / 72.0
    mat = _fitz.Matrix(zoom, zoom)  # type: ignore[attr-defined]
    pix = page.get_pixmap(matrix=mat)  # type: ignore[attr-defined]
    return pix.tobytes("png")


def _run_async(coro: object) -> object:
    """Run an async coroutine safely even inside an existing event loop.

    Uses a fresh event loop in a worker thread to avoid
    'cannot run nested event loop' errors from clypi's async runner.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()  # ty: ignore[invalid-argument-type]


def _table_to_md(table_block: object, by_id: dict) -> str:
    """Format a Textract TABLE block as a GFM pipe table.

    Args:
        table_block: A ``TextractBlock`` with ``block_type == TABLE``.
        by_id:       All blocks in this page keyed by ID.

    Returns:
        Multi-line string: header row, separator row, data rows.
        Empty string if the table has no parseable cells.
    """
    from qagents.models.ocr import BlockType, TextractBlock

    cells: list[TextractBlock] = [
        by_id[cid]
        for cid in table_block.child_ids  # type: ignore[attr-defined]
        if cid in by_id and by_id[cid].block_type == BlockType.CELL
    ]
    if not cells:
        return ""

    max_row = max((c.row_index or 0) for c in cells)
    max_col = max((c.column_index or 0) for c in cells)
    if max_row == 0 or max_col == 0:
        return ""

    grid: list[list[str]] = [[""] * max_col for _ in range(max_row)]
    for cell in cells:
        r = (cell.row_index or 1) - 1
        c = (cell.column_index or 1) - 1
        if 0 <= r < max_row and 0 <= c < max_col:
            words = [
                by_id[wid].text
                for wid in cell.child_ids
                if wid in by_id
                and by_id[wid].block_type == BlockType.WORD
                and by_id[wid].text
            ]
            grid[r][c] = " ".join(words)

    lines: list[str] = []
    if not grid:
        return ""

    # Header row
    header = grid[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * max_col) + " |")
    lines.extend("| " + " | ".join(row) + " |" for row in grid[1:])

    return "\n".join(lines)


def _bbox_y0(block: object) -> float:
    """Return the top Y coordinate of a block's bounding box (0.0 if absent)."""
    bbox = getattr(block, "bbox", None)
    return bbox.y0 if bbox else 0.0


def _bbox_overlaps_table(line_bbox: object, table_bboxes: list) -> bool:
    """Return True if *line_bbox* vertically overlaps any TABLE bbox.

    Checks that the LINE's centre Y falls within the TABLE's Y range.
    A small tolerance of 0.5% of normalised height is applied.
    """
    if line_bbox is None:
        return False
    cy = (line_bbox.y0 + line_bbox.y1) / 2.0
    for tbbox in table_bboxes:
        if tbbox is None:
            continue
        if tbbox.y0 - 0.005 <= cy <= tbbox.y1 + 0.005:
            return True
    return False


def _blocks_to_markdown(blocks: list) -> str:
    """Convert a page's flat Textract block list into a Markdown string.

    Algorithm:
    1. Sort all blocks by top-Y position (reading order).
    2. Collect TABLE bboxes for LINE suppression.
    3. Emit LINE text for lines not inside any TABLE.
    4. Emit TABLE markdown at its Y position.
    5. Deduplicate consecutive blank lines.

    Args:
        blocks: List of ``TextractBlock`` objects for a single page.

    Returns:
        Markdown string for the page content.
    """
    from qagents.models.ocr import BlockType

    by_id: dict = {b.id: b for b in blocks}

    # Collect TABLE bboxes so we can suppress overlapping LINE blocks
    table_bboxes = [b.bbox for b in blocks if b.block_type == BlockType.TABLE]

    # Sort all blocks by top-Y, then left-X (reading order)
    sorted_blocks = sorted(
        blocks,
        key=lambda b: (
            _bbox_y0(b),
            getattr(getattr(b, "bbox", None), "x0", 0.0),
        ),
    )

    md_parts: list[str] = []
    emitted_table_ids: set[str] = set()

    for block in sorted_blocks:
        if block.block_type == BlockType.LINE:
            if _bbox_overlaps_table(block.bbox, table_bboxes):
                continue  # will be rendered via TABLE block
            text = block.text.strip()
            if text:
                md_parts.append(text)

        elif block.block_type == BlockType.TABLE:
            if block.id in emitted_table_ids:
                continue
            emitted_table_ids.add(block.id)
            tmd = _table_to_md(block, by_id)
            if tmd:
                md_parts.append("")  # blank line before table
                md_parts.append(tmd)
                md_parts.append("")  # blank line after table

    # Collapse multiple consecutive blank lines to a single blank
    result: list[str] = []
    prev_blank = False
    for line in md_parts:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        result.append(line)
        prev_blank = is_blank

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Figure extraction
# ---------------------------------------------------------------------------

# Minimum gap height (normalised page fraction) to qualify as a figure.
_MIN_FIG_HEIGHT: float = 0.07
# Fraction of near-white pixels above which a crop is considered blank space.
_BLANK_RATIO: float = 0.96
# Top/bottom margins to exclude from gap search (headers, page numbers, footers).
_HEADER_ZONE: float = 0.06
_FOOTER_ZONE: float = 0.06


def _extract_page_figures(
    page: object,
    blocks: list,
    figures_dir: Path,
    page_num: int,
    fig_offset: int = 0,
    dpi: int = 300,
) -> list[tuple[float, str, dict]]:
    """Detect and save figure regions from a single page.

    Detection strategy (in priority order):
    1. **Explicit LAYOUT_FIGURE blocks** — when Textract was called with the
       LAYOUT feature type, it returns ``LAYOUT_FIGURE`` blocks with precise
       bounding boxes.  These are used directly, giving the most accurate results.
    2. **Gap heuristic** — fallback when LAYOUT is absent.  Identifies contiguous
       vertical areas of the page with no LINE or TABLE blocks above a minimum
       height threshold.  Less accurate but works with ``FeatureTypes=["TABLES"]``.

    Each candidate is cropped from the page via PyMuPDF, checked for blank
    content, and saved as a PNG alongside the output Markdown.

    Args:
        page:        PyMuPDF Page object.
        blocks:      Textract blocks for this page (normalised coords).
        figures_dir: Directory to write figure PNG files.
        page_num:    0-indexed page number (used in output filenames).
        fig_offset:  Global figure count before this page (for sequential numbering).
        dpi:         Render DPI for figure crops (should match main render DPI).

    Returns:
        List of ``(y_centre_normalised, md_ref, meta)`` tuples sorted by Y position.
        ``md_ref`` is a Markdown image reference.  ``meta`` is a dict with figure
        metadata suitable for writing to ``figures/index.yaml``.
    """
    import io

    import numpy as np
    from PIL import Image
    from qagents.models.ocr import BlockType

    # ── 0. Prefer explicit LAYOUT_FIGURE blocks if present ───────────────
    layout_figures = [b for b in blocks if b.block_type == BlockType.LAYOUT_FIGURE]
    if layout_figures:
        logger.debug(
            "[fig-extract] p{}: {} LAYOUT_FIGURE block(s) — using explicit detection",
            page_num + 1,
            len(layout_figures),
        )
        gap_candidates = [(b.bbox.y0, b.bbox.y1) for b in layout_figures if b.bbox]
        use_gap_heuristic = False
    else:
        use_gap_heuristic = True

    if use_gap_heuristic:
        # ── 1. Collect vertical coverage from content blocks ─────────────────
        intervals: list[tuple[float, float]] = [
            (block.bbox.y0, block.bbox.y1)
            for block in blocks
            if block.block_type in (BlockType.LINE, BlockType.TABLE) and block.bbox
        ]

        # ── 2. Merge overlapping/adjacent coverage intervals ─────────────────
        merged: list[tuple[float, float]] = []
        if intervals:
            intervals.sort()
            cy0, cy1 = intervals[0]
            for y0, y1 in intervals[1:]:
                if y0 <= cy1 + 0.005:  # merge if gap ≤ 0.5 % of page height
                    cy1 = max(cy1, y1)
                else:
                    merged.append((cy0, cy1))
                    cy0, cy1 = y0, y1
            merged.append((cy0, cy1))

        # ── 3. Enumerate gaps (content zone only, excluding header/footer) ────
        zone_top = _HEADER_ZONE
        zone_bottom = 1.0 - _FOOTER_ZONE
        gap_candidates = []
        if not merged:
            # Whole content zone is uncovered — treat as one candidate
            gap_candidates.append((zone_top, zone_bottom))
        else:
            # Gap before first block
            if merged[0][0] - zone_top > _MIN_FIG_HEIGHT:
                gap_candidates.append((zone_top, merged[0][0]))
            # Gaps between consecutive blocks
            for i in range(len(merged) - 1):
                g0, g1 = merged[i][1], merged[i + 1][0]
                if g1 - g0 > _MIN_FIG_HEIGHT:
                    gap_candidates.append((g0, g1))
            # Gap after last block
            if zone_bottom - merged[-1][1] > _MIN_FIG_HEIGHT:
                gap_candidates.append((merged[-1][1], zone_bottom))

    if not gap_candidates:
        return []

    # ── 4. Crop, check blank, save ────────────────────────────────────────
    pw: float = page.rect.width  # type: ignore[attr-defined]  — PDF points
    ph: float = page.rect.height  # type: ignore[attr-defined]
    zoom = dpi / 72.0
    mat = _fitz.Matrix(zoom, zoom)  # type: ignore[attr-defined]

    figures_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[float, str]] = []

    for gap_y0, gap_y1 in gap_candidates:
        # Normalised → PDF point coordinates (small horizontal margin)
        rect = _fitz.Rect(  # type: ignore[attr-defined]
            0.02 * pw,
            gap_y0 * ph,
            0.98 * pw,
            gap_y1 * ph,
        )
        pix = page.get_pixmap(matrix=mat, clip=rect)  # type: ignore[attr-defined]
        png_bytes = pix.tobytes("png")

        # Blank detection: skip if ≥ _BLANK_RATIO of pixels are near-white
        img_arr = np.array(
            Image.open(io.BytesIO(png_bytes)).convert("L"), dtype=np.uint8
        )
        total = img_arr.size
        if total > 0 and np.sum(img_arr >= 245) / total >= _BLANK_RATIO:
            logger.debug(
                "[fig-extract] p{} gap [{:.3f}-{:.3f}] skipped (blank)",
                page_num + 1,
                gap_y0,
                gap_y1,
            )
            continue

        fig_num = fig_offset + len(results) + 1
        filename = f"page{page_num + 1}_fig{fig_num}.png"
        (figures_dir / filename).write_bytes(png_bytes)
        y_centre = (gap_y0 + gap_y1) / 2.0
        detection = "layout" if not use_gap_heuristic else "gap"
        description = (
            f"Figure {fig_num} \u2014 page {page_num + 1}, "
            f"detected via {'LAYOUT_FIGURE block' if not use_gap_heuristic else 'gap heuristic'}"
        )
        meta = {
            "filename": filename,
            "page": page_num + 1,
            "fig_num": fig_num,
            "detection": detection,
            "description": description,
            "bbox_y0": round(gap_y0, 4),
            "bbox_y1": round(gap_y1, 4),
            "size_bytes": len(png_bytes),
            "width_px": pix.width,  # type: ignore[attr-defined]
            "height_px": pix.height,  # type: ignore[attr-defined]
        }
        md_ref = f"\n\n![Figure {fig_num}](figures/{filename})\n\n"
        results.append((y_centre, md_ref, meta))  # ty: ignore[invalid-argument-type]
        logger.debug(
            "[fig-extract] p{} gap [{:.3f}\u2013{:.3f}] \u2192 {}",
            page_num + 1,
            gap_y0,
            gap_y1,
            filename,
        )

    return results  # ty: ignore[invalid-return-type]


def _write_figures_index(
    figures_dir: Path,
    records: list[dict],
    source_name: str,
) -> None:
    """Write ``figures/index.yaml`` with compact per-figure metadata.

    Args:
        figures_dir:  Path to the figures directory (index written here).
        records:      List of metadata dicts from ``_extract_page_figures``.
        source_name:  Original PDF filename, embedded as a comment header.
    """
    from datetime import UTC, datetime

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 120
    yaml.indent(mapping=2, sequence=4, offset=2)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        "source": source_name,
        "generated": now,
        "count": len(records),
        "figures": [
            {
                "path": r["filename"],
                "description": r["description"],
                "page": r["page"],
                "detection": r["detection"],
                "bbox": [r["bbox_y0"], r["bbox_y1"]],
                "size_bytes": r["size_bytes"],
                "dimensions": f"{r['width_px']}x{r['height_px']}",
            }
            for r in sorted(records, key=lambda x: (x["page"], x["fig_num"]))
        ],
    }

    index_path = figures_dir / "index.yaml"
    with index_path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    logger.debug("[fig-extract] wrote figures/index.yaml  ({} figures)", len(records))


# ---------------------------------------------------------------------------
# AI formatting
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = """\
You are a document formatting specialist. Your task is to clean raw OCR-extracted \
text from a professional PDF report and produce clean, readable Markdown.

STRICT RULES — follow precisely, in order of priority:

1. PRESERVE ALL CONTENT. Never remove, summarise, paraphrase, truncate or alter \
any substantive text, numbers, names, dates, control IDs, criteria references, \
audit results, legal language, or data of any kind.

2. Remove ONLY these non-content OCR artifacts:
   a. Standalone page-number lines: a bare integer (e.g. "6", "38", "1") appearing \
ALONE on its own line with nothing else — this is a PDF page-stamp, not content. \
ALWAYS remove it. It will typically appear at the very start of the input.
   b. Logo / watermark fragments: isolated short ALL-CAPS words that are clearly \
not prose and not part of a heading (e.g. "ATOM", "TM", "aicpa.org/soc4so" on \
their own line without surrounding context).
   c. Repeated section-header echoes: if the exact same heading text appears twice \
in immediate succession, keep only the first occurrence.

3. Add Markdown heading markers:
   - Use ## for major section headings (e.g. "INDEPENDENT SERVICE AUDITOR'S REPORT", \
"MANAGEMENT'S ASSERTION", "SECURITY PRINCIPLE AND CRITERIA TABLE", \
"Table of Contents", "COMPLEMENTARY USER ENTITY CONTROLS").
   - Use ### for subsection headings (e.g. "Scope", "Opinion", "Restricted Use", \
"CONTROL ENVIRONMENT", "RISK ASSESSMENT", "INCIDENT MANAGEMENT").
   - Detect headings from context: short lines in ALL-CAPS or Title Case that \
introduce a new section, not mid-sentence.
   - Do NOT add # (H1) — the document title is handled outside this chunk.

4. Fix fragmented lines:
   - If a heading or sentence is clearly split mid-word or mid-phrase across \
adjacent lines (e.g. a heading split as "SECURITY PRINCIPLE AND CRITERIA TABLE II \
Transformation\\nDiagnostics AI Limited"), join those lines into a single continuous \
line before adding heading markers.
   - Do NOT join lines that are clearly separate paragraphs, list items, table rows, \
or complete sentences ending with punctuation.

5. Fix broken table cells:
   - If a table cell's text is incorrectly split across multiple adjacent pipe-table \
cells (e.g. "| The entity demonstrates a | commitment to |"), merge the content \
into the correct single cell.
   - Preserve all table rows and columns; do not collapse rows.

6. Return ONLY the reformatted Markdown. No explanations, preamble, or trailing commentary.\
"""

# Characters-per-chunk target. At ~4 chars/token, 8 000 chars ≈ 2 000 tokens —
# comfortably fits within an 8 192-token max-output budget.
_CHUNK_CHARS = 8_000


async def _ai_format_chunk(
    raw: str,
    *,
    bedrock_client: object,
    model_id: str,
    cache: ContentCache | None = None,
) -> str:
    """Send one Markdown chunk to Bedrock for cleaning and return the result.

    Cache-checks by content-addressed URN before calling Bedrock; stores the
    result on success.  Falls back to raw input on any API error.
    """
    if not raw.strip():
        return raw

    _urn: str | None = None
    if cache is not None:
        from qdocs.cache import ContentCache

        _urn = ContentCache.urn_for("ai_format", f"{model_id}|{raw}".encode())
        _hit = cache.get_text(_urn)
        if _hit is not None:
            logger.debug("[ai-format] cache hit  urn={}\u2026", _urn[:32])
            return _hit

    kwargs: dict = {
        "modelId": model_id,
        "system": [{"text": _AI_SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": raw}]}],
        "inferenceConfig": {"maxTokens": 8192, "temperature": 0.0},
    }
    try:
        response = await asyncio.to_thread(
            bedrock_client.converse,
            **kwargs,  # type: ignore[attr-defined]
        )
        content = response["output"]["message"]["content"]
        result = "".join(c.get("text", "") for c in content).strip()
        if cache is not None and _urn is not None:
            cache.put_text(_urn, result)
        return result
    except Exception as exc:
        logger.warning("[ai-format] Bedrock call failed, keeping raw output: {}", exc)
        return raw


async def _ai_format_markdown(
    raw_md: str,
    *,
    region: str,
    model_id: str,
    cache: ContentCache | None = None,
    on_ai_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Split *raw_md* into chunks, clean each via Bedrock, reassemble.

    The title line (H1) and ``## Page N`` headers are preserved as-is and not
    sent to the LLM to avoid accidental removal.  The content of each page is
    cleaned independently.

    Args:
        raw_md:          Raw Markdown produced by the Textract block assembler.
        region:          AWS region for Bedrock.
        model_id:        Bedrock model ID.
        on_ai_progress:  Optional ``(completed, total)`` callback fired after
                         each page section is formatted.

    Returns:
        Cleaned Markdown string.
    """
    import boto3  # core dep

    client = boto3.client("bedrock-runtime", region_name=region)

    # Split on page-section markers to preserve structure
    # First element is the document title (# ...), rest are page sections
    import re

    sections = re.split(r"(\n## Page \d+\n)", raw_md)
    # sections alternates: [title_block, "## Page N\n", content, "## Page N+1\n", content, ...]

    total_sections = max((len(sections) - 1) // 2, 1)
    cleaned_parts: list[str] = []

    # First block is the title — keep as-is
    if sections:
        cleaned_parts.append(sections[0])

    # Pair up the page-header and page-content tokens
    i = 1
    section_idx = 0
    while i < len(sections):
        header = sections[i] if i < len(sections) else ""
        content = sections[i + 1] if i + 1 < len(sections) else ""
        i += 2
        section_idx += 1

        cleaned_parts.append(header)  # keep ## Page N as-is

        if not content.strip():
            cleaned_parts.append(content)
        elif len(content) <= _CHUNK_CHARS:
            # Sub-chunk large page content to stay within output token limit
            cleaned = await _ai_format_chunk(
                content, bedrock_client=client, model_id=model_id, cache=cache
            )
            cleaned_parts.append(cleaned)
        else:
            # Split oversized pages on blank lines, recombine into max-size chunks
            paragraphs = content.split("\n\n")
            current_chunk: list[str] = []
            current_len = 0
            page_parts: list[str] = []

            for para in paragraphs:
                if current_len + len(para) > _CHUNK_CHARS and current_chunk:
                    chunk_text = "\n\n".join(current_chunk)
                    page_parts.append(
                        await _ai_format_chunk(
                            chunk_text,
                            bedrock_client=client,
                            model_id=model_id,
                            cache=cache,
                        )
                    )
                    current_chunk = [para]
                    current_len = len(para)
                else:
                    current_chunk.append(para)
                    current_len += len(para)

            if current_chunk:
                chunk_text = "\n\n".join(current_chunk)
                page_parts.append(
                    await _ai_format_chunk(
                        chunk_text,
                        bedrock_client=client,
                        model_id=model_id,
                        cache=cache,
                    )
                )

            cleaned_parts.append("\n\n".join(page_parts))

        if on_ai_progress is not None:
            on_ai_progress(section_idx, total_sections)

    return "".join(cleaned_parts)


# ---------------------------------------------------------------------------
# Public converter
# ---------------------------------------------------------------------------


def _parse_page_spec(spec: str, total_pages: int) -> set[int]:
    """Parse a page specification string into a set of 1-based page numbers.

    Supports comma-separated values and ranges, e.g. ``'1-3,7,10-12'``.
    Values outside ``[1, total_pages]`` are silently clamped/ignored.

    Args:
        spec:        Page spec string from the user (e.g. ``'1-3,7'``).
        total_pages: Total number of pages in the document.

    Returns:
        Set of 1-based page numbers to process.
    """
    result: set[int] = set()
    for _part in spec.split(","):
        part = _part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            try:
                lo_n, hi_n = int(lo_s.strip()), int(hi_s.strip())
            except ValueError:
                continue
            for n in range(max(1, lo_n), min(total_pages, hi_n) + 1):
                result.add(n)
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total_pages:
                result.add(n)
    return result


def convert_pdf_to_md_textract(
    source: Path,
    target: Path,
    *,
    region: str = "eu-west-1",
    dpi: int = 300,
    ai_format: bool = True,
    ai_model_id: str | None = None,
    extract_figures: bool = False,
    figures_dir: Path | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    cache: ContentCache | None = None,
    pages: str | None = None,
) -> None:
    """Convert a PDF to Markdown via AWS Textract + AI post-processing.

    Each page is rasterised to a PNG at *dpi* DPI, sent to Textract
    ``analyze_document`` (TABLES feature), assembled into raw Markdown, then
    optionally cleaned by an LLM (``ai_format=True``, default) to fix heading
    hierarchy, remove OCR artifacts, and rejoin fragmented lines — without any
    data loss.

    Requires a valid AWS MFA session — run ``eval $(qmfa)`` beforehand.

    Args:
        source:      Path to the source PDF file.
        target:      Path for the output Markdown file.
        region:      AWS region for both Textract and Bedrock (default: eu-west-1).
        dpi:         Page rendering resolution (default: 300).
        ai_format:   Apply AI post-processing to clean up OCR artifacts and add
                     heading markers (default: True).  Set False for raw Textract
                     output.
        ai_model_id:      Bedrock model ID for AI formatting.  Defaults to
                          ``AgentSettings().bedrock_model_id`` from env/config.
        extract_figures:  Detect and save figure regions as PNG files in a
                          ``figures/`` sub-directory next to *target*, and insert
                          Markdown image references at their reading-order
                          positions (default: False).  When enabled, calls
                          Textract with ``LAYOUT`` in addition to ``TABLES`` so
                          that explicit ``LAYOUT_FIGURE`` blocks are used for
                          figure bounding boxes instead of the gap heuristic.
                          Falls back to gap heuristic when Textract returns no
                          ``LAYOUT_FIGURE`` blocks (e.g. older API response).

    Raises:
        ConversionError: If PyMuPDF is missing, source does not exist,
                         no valid MFA session is found, or Textract fails.
    """
    if not _FITZ_AVAILABLE:
        msg = "PyMuPDF not installed.  Run: uv pip install pymupdf"
        raise ConversionError(msg)

    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)

    # ── MFA preflight ────────────────────────────────────────────────────
    valid, mfa_msg = check_mfa_session()
    if not valid:
        msg = (
            f"Textract requires a valid AWS MFA session.\n"
            f"  {mfa_msg}\n"
            f"  Run: eval $(qmfa)"
        )
        raise ConversionError(msg)
    logger.debug("[textract] {}", mfa_msg)

    # ── Resolve AI model ─────────────────────────────────────────────────
    _model_id = ai_model_id
    if ai_format and _model_id is None:
        try:
            from qagents.settings import AgentSettings

            _model_id = AgentSettings().bedrock_model_id
        except Exception:
            _model_id = "eu.anthropic.claude-sonnet-4-6"
    logger.debug(
        "[textract] ai_format={} model={}",
        ai_format,
        _model_id if ai_format else "n/a",
    )

    # ── Import provider ─────────────────────────────────────────────────
    try:
        from qagents.providers.aws.textract import TextractProvider
    except ImportError as exc:
        msg = f"qagents not installed or missing TextractProvider: {exc}"
        raise ConversionError(msg) from exc

    provider = TextractProvider(region=region)

    # ── Open PDF ─────────────────────────────────────────────────────────
    try:
        doc = _fitz.open(str(source))  # type: ignore[attr-defined]
    except Exception as exc:
        msg = f"Failed to open PDF {source.name}: {exc}"
        raise ConversionError(msg) from exc

    doc_page_count = len(doc)
    _page_filter: set[int] = _parse_page_spec(pages, doc_page_count) if pages else set()
    pages_to_process = (
        sorted(_page_filter) if _page_filter else list(range(1, doc_page_count + 1))
    )
    page_count = len(pages_to_process)
    logger.debug(
        "[textract] {} — {}{} page(s)  region={}  ai_format={}",
        source.name,
        page_count,
        f"/{doc_page_count} filtered" if _page_filter else "",
        region,
        ai_format,
    )

    # ── Extract pages via Textract ────────────────────────────────────────
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        md_lines: list[str] = [f"# {source.stem}\n"]
        _figures_dir: Path | None = (
            (figures_dir or target.parent / "figures") if extract_figures else None
        )
        fig_count = 0
        _all_fig_records: list[dict] = []

        for loop_idx, page_1 in enumerate(pages_to_process, 1):
            page = doc[page_1 - 1]
            logger.debug("[textract] rendering page {}/{}", page_1, page_count)

            png_bytes = _render_page_png(page, dpi=dpi)

            # Pass LAYOUT feature type when extracting figures for explicit
            # LAYOUT_FIGURE block detection (falls back to gap heuristic if absent).
            _feature_types = (
                ["TABLES", "LAYOUT"] if extract_figures else ["TABLES", "FORMS"]
            )

            # Textract: check content-addressed cache first
            _page_urn: str | None = None
            if cache is not None:
                from qagents.models.ocr import TextractBlock

                from qdocs.cache import ContentCache

                _page_urn = ContentCache.urn_for("textract", png_bytes)
                _cached_blocks = cache.get_json(_page_urn)
                if _cached_blocks is not None:
                    blocks = [TextractBlock.model_validate(d) for d in _cached_blocks]
                    logger.debug("[textract] p{} cache hit", page_1)
                else:
                    blocks = None
            else:
                blocks = None

            if blocks is None:
                try:
                    blocks = _run_async(
                        provider.analyze_document_blocks(
                            png_bytes, feature_types=_feature_types
                        )
                    )
                    if _page_urn is not None:
                        cache.put_json(  # type: ignore[union-attr]
                            _page_urn, [b.model_dump(mode="json") for b in blocks]
                        )
                except Exception as exc:
                    logger.warning(
                        "[textract] p{} analyze failed: {} — falling back to extract_text",
                        page_1,
                        exc,
                    )
                    try:
                        text = _run_async(provider.extract_text(png_bytes, mode="text"))
                        blocks_result: list = []
                        from qagents.models.ocr import BlockType, TextractBlock

                        for line_text in text.splitlines():
                            lt = line_text.strip()
                            if lt:
                                blocks_result.append(
                                    TextractBlock(
                                        id=f"_fallback_{page_1}_{len(blocks_result)}",
                                        block_type=BlockType.LINE,
                                        text=lt,
                                    )
                                )
                        blocks = blocks_result
                    except Exception as exc2:
                        logger.warning(
                            "[textract] p{} extract_text also failed: {}",
                            page_1,
                            exc2,
                        )
                        blocks = []

            if doc_page_count > 1:
                md_lines.append(f"\n## Page {page_1}\n")

            page_md = _blocks_to_markdown(blocks)  # ty: ignore[invalid-argument-type]
            if page_md.strip():
                md_lines.append(page_md)
            else:
                md_lines.append("_[No extractable content on this page]_")

            if extract_figures and _figures_dir is not None:
                fig_markers = _extract_page_figures(
                    page,
                    blocks,  # ty: ignore[invalid-argument-type]
                    _figures_dir,
                    page_1 - 1,
                    fig_offset=fig_count,
                    dpi=dpi,
                )
                for _, md_ref, fig_meta in fig_markers:
                    md_lines.append(md_ref)
                    _all_fig_records.append(fig_meta)
                fig_count += len(fig_markers)

            if on_progress is not None:
                on_progress("page", loop_idx, page_count)

        doc.close()
        raw_md = "\n".join(md_lines)

        # ── AI post-processing ────────────────────────────────────────────
        if ai_format and _model_id:
            logger.debug(
                "[textract] AI formatting {} via {}  model={}",
                source.name,
                region,
                _model_id,
            )
            if on_progress is not None:
                on_progress("ai_start", 0, page_count)

            def _ai_progress_cb(completed: int, total: int) -> None:
                if on_progress is not None:
                    on_progress("ai_chunk", completed, total)

            try:
                final_md = _run_async(
                    _ai_format_markdown(
                        raw_md,
                        region=region,
                        model_id=_model_id,
                        cache=cache,
                        on_ai_progress=_ai_progress_cb,
                    )
                )
                logger.debug("[textract] AI formatting complete for {}", source.name)
            except Exception as exc:
                logger.warning(
                    "[textract] AI formatting failed, writing raw output: {}", exc
                )
                final_md = raw_md
            finally:
                if on_progress is not None:
                    on_progress("ai_done", 0, page_count)
        else:
            final_md = raw_md

        target.write_text(final_md, encoding="utf-8")  # ty: ignore[invalid-argument-type]

        # ── Write figures/index.yaml ──────────────────────────────────────
        if extract_figures and _figures_dir is not None:
            try:
                _figures_dir.mkdir(parents=True, exist_ok=True)
                _write_figures_index(_figures_dir, _all_fig_records, source.name)
            except Exception as idx_exc:
                logger.warning(
                    "[textract] figures/index.yaml write failed: {}", idx_exc
                )

        if on_progress is not None:
            on_progress("done", page_count, fig_count)
        logger.debug(
            "[textract] {} \u2192 {}  ({} pages, ai_format={})",
            source.name,
            target.name,
            page_count,
            ai_format,
        )

    except ConversionError:
        raise
    except Exception as exc:
        msg = f"Textract conversion failed for {source.name}: {exc}"
        raise ConversionError(msg) from exc
