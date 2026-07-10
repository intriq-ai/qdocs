"""Markdown → XLSX converter using openpyxl.

Parses a Markdown file and writes a multi-sheet Excel workbook.

Sheet detection
---------------
Each H2 heading (``## Sheet Name``) starts a new worksheet.  Tables in
that section land on that sheet.  If there are no H2 headings the entire
document writes to a single sheet named after the file stem.

Cover sheet
-----------
When ``cover`` is enabled (``CoverPageConfig.enabled = True``) the
first sheet is a branded cover sheet containing:
    • Company logo (PNG, top-left, 2x0.75 in)
  • Document title (H1 heading from the Markdown, large bold cell)
  • Metadata row: Author | Version | Date | Classification
  • An optional chapter label
  • Revision indicator in the footer row
  • Classification colour bar (thin row, classification colour fill)

Formatting
----------
All configuration is driven by ``XlsxFormatConfig`` (passed at call
time or defaulted).  The caller can override locale, currency symbol,
date format, number format, and column-width strategy.

Supported Markdown constructs
------------------------------
* H1 (``# Title``) — document title (used on cover if enabled)
* H2 (``## Sheet``) — new worksheet
* H3 (``### Section``) — bold section header row, full-width, dark fill
* Tables (GFM pipe tables) — rendered as styled data tables
* Paragraphs of plain text — written as single merged cells (note rows)
* Bullet lists (``- item``) — each item as an indented cell row
* Bold paragraph (``**text**``) — rendered as a sub-header row (medium fill)
* Horizontal rules (``---``) — blank separator row

Limitations
-----------
* Nested tables are not supported (GFM restriction anyway).
* Code fences are written as plain-text cells.
* Mermaid blocks are omitted (not renderable in xlsx).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from qdocs.models.profile import CoverPageConfig

try:
    import openpyxl
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule, FormulaRule
    from openpyxl.styles import (
        Alignment,
        Border,
        Font,
        PatternFill,
        Side,
    )
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

from qdocs.exceptions import ConversionError

# ---------------------------------------------------------------------------
# Format configuration
# ---------------------------------------------------------------------------

# Colour palette — can be overridden via XlsxFormatConfig
_PALETTE = {
    "cover_title_bg":    "1F4E79",   # dark navy
    "cover_meta_bg":     "2E75B6",   # mid blue
    "cover_bar_bg":      "BDD7EE",   # light blue bar
    "section_h2_bg":     "1F4E79",   # dark navy (sheet title row)
    "section_h3_bg":     "2E75B6",   # mid blue (section header)
    "col_header_bg":     "D6E4F0",   # pale blue
    "alt_row_bg":        "F2F7FB",   # very pale blue alt row
    "total_row_bg":      "BDD7EE",   # light blue total row
    "subheader_bg":      "E9EFF7",   # very light (bold paragraph)
    "note_text":         "595959",   # grey italic
}

# Classification colours (matching DocumentClassification.color_hex)
_CLASSIF_COLORS = {
    "PUBLIC":       "006400",
    "INTERNAL":     "1a1a8c",
    "CONFIDENTIAL": "cc0000",
    "RESTRICTED":   "8B4500",
    "TOP SECRET":   "4B0000",  # pragma: allowlist secret
}


class XlsxFormatConfig:
    """Runtime formatting options for the XLSX converter.

    All options have sensible defaults matching Intriq's financial document
    style.  Pass overrides when calling ``convert_md_to_xlsx``.
    """

    def __init__(
        self,
        *,
        # Locale / currency
        currency_symbol: str = "$",
        currency_format: str = "#,##0.00",
        number_format: str = "#,##0",
        date_format: str = "YYYY-MM-DD",
        locale: str = "en_US",
        # Column widths
        auto_width: bool = True,
        min_col_width: float = 8.0,
        max_col_width: float = 60.0,
        default_col_width: float = 18.0,
        # Row heights
        data_row_height: float = 16.0,
        header_row_height: float = 26.0,
        section_row_height: float = 20.0,
        # Freeze header row on data sheets
        freeze_header: bool = True,
        # Wrap text in data cells by default
        wrap_text: bool = True,
        # Auto-detect and apply currency format to numeric cells
        auto_currency: bool = True,
        # Palette overrides (key → hex string, no #)
        palette: dict[str, str] | None = None,
        # Tab colour for worksheets (hex, no #) — None = auto
        tab_color: str | None = None,
        # Advanced worksheet features
        merged_ranges: list[str] | None = None,
        named_ranges: dict[str, str] | None = None,
        conditional_rules: list[dict[str, Any]] | None = None,
        data_validations: list[dict[str, Any]] | None = None,
        cell_comments: dict[str, str] | None = None,
    ) -> None:
        self.currency_symbol = currency_symbol
        self.currency_format = currency_format
        self.number_format = number_format
        self.date_format = date_format
        self.locale = locale
        self.auto_width = auto_width
        self.min_col_width = min_col_width
        self.max_col_width = max_col_width
        self.default_col_width = default_col_width
        self.data_row_height = data_row_height
        self.header_row_height = header_row_height
        self.section_row_height = section_row_height
        self.freeze_header = freeze_header
        self.wrap_text = wrap_text
        self.auto_currency = auto_currency
        self.palette = {**_PALETTE, **(palette or {})}
        self.tab_color = tab_color
        self.merged_ranges = merged_ranges or []
        self.named_ranges = named_ranges or {}
        self.conditional_rules = conditional_rules or []
        self.data_validations = data_validations or []
        self.cell_comments = cell_comments or {}


# ---------------------------------------------------------------------------
# Internal style helpers
# ---------------------------------------------------------------------------

def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color.upper())


def _font(
    bold: bool = False,
    italic: bool = False,
    size: float = 10,
    color: str = "000000",
    name: str = "Calibri",
) -> Font:
    return Font(bold=bold, italic=italic, size=size, color=color, name=name)


def _border_thin() -> Border:
    t = Side(style="thin")
    return Border(left=t, right=t, top=t, bottom=t)


def _align(
    wrap: bool = True,
    horiz: str = "left",
    vert: str = "top",
) -> Alignment:
    return Alignment(wrap_text=wrap, horizontal=horiz, vertical=vert)


def _apply_cell(
    ws: Any,
    row: int,
    col: int,
    value: Any,
    *,
    font: Any = None,
    fill: Any = None,
    border: Any = None,
    alignment: Any = None,
    number_format: str | None = None,
    row_height: float | None = None,
) -> Any:
    cell = ws.cell(row=row, column=col, value=value)
    if font:
        cell.font = font
    if fill:
        cell.fill = fill
    if border:
        cell.border = border
    if alignment:
        cell.alignment = alignment
    if number_format:
        cell.number_format = number_format
    if row_height:
        ws.row_dimensions[row].height = row_height
    return cell


# ---------------------------------------------------------------------------
# Markdown parsing helpers
# ---------------------------------------------------------------------------

_RE_H1 = re.compile(r"^#\s+(.+)$")
_RE_H2 = re.compile(r"^##\s+(.+)$")
_RE_H3 = re.compile(r"^###\s+(.+)$")
_RE_H4 = re.compile(r"^####\s+(.+)$")
_RE_BOLD_PARA = re.compile(r"^\*\*(.+)\*\*\s*$")
_RE_TABLE_ROW = re.compile(r"^\|(.+)\|$")
_RE_TABLE_SEP = re.compile(r"^\|[\s\-:|]+\|$")
_RE_HR = re.compile(r"^---+$")
_RE_BULLET = re.compile(r"^[-*+]\s+(.+)$")
_RE_MERMAID_FENCE = re.compile(r"^```mermaid", re.IGNORECASE)
_RE_CODE_FENCE = re.compile(r"^```")
_RE_ITALIC = re.compile(r"\*(.+?)\*")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")


def _strip_inline(text: str) -> str:
    """Strip common inline Markdown decorations to plain text."""
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC.sub(r"\1", text)
    text = _RE_LINK.sub(r"\1", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    return text.strip()


def _parse_table_row(line: str) -> list[str]:
    """Parse a GFM pipe-table row into a list of cell strings."""
    # Strip leading/trailing pipes and split
    inner = line.strip().strip("|")
    return [_strip_inline(c.strip()) for c in inner.split("|")]


def _is_separator_row(line: str) -> bool:
    """Return True if line is a GFM table separator (| --- | --- |)."""
    if not line.strip().startswith("|"):
        return False
    cells = _parse_table_row(line)
    return all(re.match(r"^[-:]+$", c.replace(" ", "")) for c in cells if c)


def _looks_like_number(value: str) -> tuple[bool, float | None]:
    """Try to parse a cell value as a number.  Returns (is_num, float_value)."""
    cleaned = value.replace(",", "").replace("$", "").replace("(", "-").replace(")", "").strip()
    if cleaned in ("", "N/A", "n/a", "-", "—"):
        return False, None
    try:
        return True, float(cleaned)
    except ValueError:
        return False, None


def _looks_like_currency(value: str, symbol: str = "$") -> bool:
    """Return True if cell value looks like a currency amount."""
    v = value.strip()
    return (
        v.startswith((symbol, "-" + symbol)) or (v.startswith("(") and symbol in v)
    )


# ---------------------------------------------------------------------------
# Sheet content block types
# ---------------------------------------------------------------------------

class _Block:
    """Base class for parsed Markdown blocks destined for a worksheet."""

class _TableBlock(_Block):
    def __init__(self, headers: list[str], rows: list[list[str]]) -> None:
        self.headers = headers
        self.rows = rows

class _SectionBlock(_Block):
    def __init__(self, text: str, level: int = 3) -> None:
        self.text = text
        self.level = level  # 3 = H3, 4 = H4

class _ParagraphBlock(_Block):
    def __init__(self, text: str, bold: bool = False) -> None:
        self.text = text
        self.bold = bold

class _BulletBlock(_Block):
    def __init__(self, items: list[str]) -> None:
        self.items = items

class _SeparatorBlock(_Block):
    pass

class _CodeBlock(_Block):
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines


# ---------------------------------------------------------------------------
# Markdown → sheet content parser
# ---------------------------------------------------------------------------

def _parse_md(content: str) -> tuple[str, list[tuple[str, list[_Block]]]]:
    """Parse Markdown into (title, [(sheet_name, [blocks])]).

    Returns the document title (H1) and a list of (sheet_name, blocks) pairs.
    Sheet breaks occur at H2 headings.  The first sheet uses the stem or H1
    if no H2 is present.
    """
    lines = content.splitlines()

    title = ""
    sheets: list[tuple[str, list[_Block]]] = []
    current_sheet_name = ""
    current_blocks: list[_Block] = []

    # State
    in_mermaid = False
    in_code = False
    code_lines: list[str] = []
    in_table = False
    table_headers: list[str] = []
    table_rows: list[list[str]] = []
    bullet_items: list[str] = []

    def _flush_table() -> None:
        nonlocal in_table, table_headers, table_rows
        if in_table and table_headers:
            current_blocks.append(_TableBlock(table_headers[:], table_rows[:]))
        in_table = False
        table_headers = []
        table_rows = []

    def _flush_bullets() -> None:
        nonlocal bullet_items
        if bullet_items:
            current_blocks.append(_BulletBlock(bullet_items[:]))
        bullet_items = []

    def _flush_sheet() -> None:
        nonlocal current_blocks, current_sheet_name
        _flush_table()
        _flush_bullets()
        if current_blocks or current_sheet_name:
            sheets.append((current_sheet_name or "Sheet1", current_blocks))
        current_blocks = []

    for line in lines:
        stripped = line.strip()

        # Mermaid fence — skip block entirely
        if _RE_MERMAID_FENCE.match(stripped):
            in_mermaid = True
            continue
        if in_mermaid:
            if stripped == "```":
                in_mermaid = False
            continue

        # Generic code fence
        if not in_code and _RE_CODE_FENCE.match(stripped) and stripped != "```":
            in_code = True
            code_lines = []
            continue
        if in_code:
            if stripped == "```":
                in_code = False
                _flush_table()
                _flush_bullets()
                current_blocks.append(_CodeBlock(code_lines[:]))
                code_lines = []
            else:
                code_lines.append(stripped)
            continue

        # H1 — document title
        m = _RE_H1.match(stripped)
        if m:
            title = _strip_inline(m.group(1))
            continue

        # H2 — new sheet
        m = _RE_H2.match(stripped)
        if m:
            _flush_sheet()
            current_sheet_name = _strip_inline(m.group(1))[:31]  # xlsx limit
            continue

        # H3 — section header
        m = _RE_H3.match(stripped)
        if m:
            _flush_table()
            _flush_bullets()
            current_blocks.append(_SectionBlock(_strip_inline(m.group(1)), level=3))
            continue

        # H4 — sub-section header
        m = _RE_H4.match(stripped)
        if m:
            _flush_table()
            _flush_bullets()
            current_blocks.append(_SectionBlock(_strip_inline(m.group(1)), level=4))
            continue

        # Horizontal rule
        if _RE_HR.match(stripped) and len(stripped) >= 3:
            _flush_table()
            _flush_bullets()
            current_blocks.append(_SeparatorBlock())
            continue

        # Table row
        if _RE_TABLE_ROW.match(stripped):
            _flush_bullets()
            if _is_separator_row(stripped):
                # Separator row marks end of header; don't do anything
                continue
            cells = _parse_table_row(stripped)
            if not in_table:
                in_table = True
                table_headers = cells
                table_rows = []
            else:
                table_rows.append(cells)
            continue
        # Non-table line — flush pending table
        _flush_table()

        # Bullet list item
        m = _RE_BULLET.match(stripped)
        if m:
            bullet_items.append(_strip_inline(m.group(1)))
            continue
        _flush_bullets()

        # Bold paragraph (subheader)
        m = _RE_BOLD_PARA.match(stripped)
        if m:
            current_blocks.append(_ParagraphBlock(_strip_inline(m.group(1)), bold=True))
            continue

        # Regular paragraph (non-empty)
        if stripped:
            current_blocks.append(_ParagraphBlock(_strip_inline(stripped), bold=False))

    _flush_sheet()

    # If no H2 found, use a single default sheet
    if not sheets:
        current_blocks_final: list[_Block] = []
        for b in current_blocks:
            current_blocks_final.append(b)
        sheets = [("Data", current_blocks_final)]

    return title, sheets


# ---------------------------------------------------------------------------
# Cover sheet builder
# ---------------------------------------------------------------------------

def _build_cover_sheet(
    wb: Any,
    title: str,
    cover: CoverPageConfig,
    revision: int,
    fmt: XlsxFormatConfig,
) -> None:
    """Insert a branded cover sheet as the first sheet in the workbook."""
    ws = wb.create_sheet(title="Cover", index=0)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 22
    ws.column_dimensions["E"].width = 22
    ws.column_dimensions["F"].width = 4

    pal = fmt.palette
    row = 1

    # Top margin
    for _ in range(2):
        ws.row_dimensions[row].height = 6
        row += 1

    # Classification colour bar
    classif = cover.classification
    classif_str = classif.value if classif else "CONFIDENTIAL"
    classif_color = _CLASSIF_COLORS.get(classif_str, "cc0000")
    for c in range(1, 8):
        cell = ws.cell(row=row, column=c)
        cell.fill = _fill(classif_color)
    ws.row_dimensions[row].height = 6
    row += 1

    # Spacer
    ws.row_dimensions[row].height = 12
    row += 1

    # Logo row — insert image if available
    logo_path = cover.profile.logo_path if cover.profile else None
    if logo_path and logo_path.exists():
        try:
            img = XlImage(str(logo_path))
            img.width = 144   # ~2 inches at 72 dpi
            img.height = 54
            ws.add_image(img, f"B{row}")
        except Exception as exc:
            logger.debug(f"[xlsx cover] logo embed failed: {exc}")
    ws.row_dimensions[row].height = 54
    row += 1

    # Spacer
    ws.row_dimensions[row].height = 18
    row += 1

    # Company name
    profile = cover.profile
    company = (profile.company_name if profile else None) or "Intriq AI"
    cell = ws.cell(row=row, column=2, value=company)
    cell.font = _font(bold=True, size=13, color=pal["cover_title_bg"])
    cell.alignment = _align(wrap=False)
    ws.row_dimensions[row].height = 20
    row += 1

    # Spacer
    ws.row_dimensions[row].height = 8
    row += 1

    # Document title (large)
    cell = ws.cell(row=row, column=2, value=title or "Untitled")
    cell.font = _font(bold=True, size=22, color="000000")
    cell.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 44
    # Merge B:E for title
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    row += 1

    # Chapter label (optional)
    if cover.chapter:
        cell = ws.cell(row=row, column=2, value=cover.chapter)
        cell.font = _font(bold=False, italic=True, size=12, color="404040")
        cell.alignment = _align(wrap=False)
        ws.row_dimensions[row].height = 18
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        row += 1

    # Spacer
    ws.row_dimensions[row].height = 16
    row += 1

    # Divider row (thin colour bar)
    for c in range(2, 6):
        ws.cell(row=row, column=c).fill = _fill(pal["cover_bar_bg"])
    ws.row_dimensions[row].height = 4
    row += 1

    # Spacer
    ws.row_dimensions[row].height = 10
    row += 1

    # Metadata table
    meta_labels = ["Author", "Version", "Date", "Classification"]
    author = cover.author or (profile.default_author if profile else None) or ""
    version_str = cover.version_str or "" if cover.show_version else ""
    date_str = date.today().isoformat() if cover.show_date else ""
    meta_values = [author, version_str, date_str, classif_str]

    for label, value in zip(meta_labels, meta_values, strict=False):
        # Label cell
        lc = ws.cell(row=row, column=2, value=label)
        lc.font = _font(bold=True, size=9, color="FFFFFF")
        lc.fill = _fill(pal["cover_meta_bg"])
        lc.alignment = _align(wrap=False, vert="center")
        lc.border = _border_thin()
        ws.row_dimensions[row].height = 18
        # Value cell (merged C:E)
        vc = ws.cell(row=row, column=3, value=value)
        vc.font = _font(size=9)
        vc.alignment = _align(wrap=False, vert="center")
        vc.border = _border_thin()
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
        row += 1

    # Revision row (only if show_revision)
    if cover.show_revision and revision:
        lc = ws.cell(row=row, column=2, value="Revision")
        lc.font = _font(bold=True, size=9, color="FFFFFF")
        lc.fill = _fill(pal["cover_meta_bg"])
        lc.alignment = _align(wrap=False, vert="center")
        lc.border = _border_thin()
        ws.row_dimensions[row].height = 18
        vc = ws.cell(row=row, column=3, value=f"r{revision}")
        vc.font = _font(size=9)
        vc.alignment = _align(wrap=False, vert="center")
        vc.border = _border_thin()
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
        row += 1

    # Spacer
    ws.row_dimensions[row].height = 16
    row += 1

    # Copyright / address footer
    copyright_text = (profile.copyright if profile else None) or ""
    if copyright_text:
        cell = ws.cell(row=row, column=2, value=copyright_text)
        cell.font = _font(italic=True, size=8, color="808080")
        cell.alignment = _align(wrap=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        ws.row_dimensions[row].height = 14
        row += 1

    # Bottom classification bar
    row += 2
    for c in range(1, 8):
        ws.cell(row=row, column=c).fill = _fill(classif_color)
    ws.row_dimensions[row].height = 6


# ---------------------------------------------------------------------------
# Data sheet renderer
# ---------------------------------------------------------------------------

def _render_blocks(
    ws: Any,
    blocks: list[_Block],
    fmt: XlsxFormatConfig,
    *,
    sheet_title: str = "",
) -> None:
    """Write blocks to a worksheet, tracking column widths."""
    pal = fmt.palette
    row = 1
    col_widths: dict[int, float] = {}

    def _track_width(col: int, value: Any) -> None:
        if not fmt.auto_width:
            return
        w = len(str(value)) * 1.18 if value else fmt.min_col_width
        col_widths[col] = max(col_widths.get(col, fmt.min_col_width), min(w, fmt.max_col_width))

    def _apply_col_widths() -> None:
        for c, w in col_widths.items():
            ws.column_dimensions[get_column_letter(c)].width = max(w, fmt.min_col_width)

    # Optional sheet title header row (uses sheet_title if passed)
    if sheet_title:
        cell = ws.cell(row=row, column=1, value=sheet_title)
        cell.font = _font(bold=True, size=13, color="FFFFFF")
        cell.fill = _fill(pal["section_h2_bg"])
        cell.alignment = _align(wrap=False)
        ws.row_dimensions[row].height = fmt.section_row_height + 4
        row += 1

    alt_counter = 0  # toggle alt row fill

    for block in blocks:

        # ── Table ─────────────────────────────────────────────────────────
        if isinstance(block, _TableBlock):
            n_cols = max(len(block.headers), *(len(r) for r in block.rows) if block.rows else [1])

            # Column header row
            for c_idx, hdr in enumerate(block.headers, start=1):
                cell = ws.cell(row=row, column=c_idx, value=hdr)
                cell.font = _font(bold=True, size=9)
                cell.fill = _fill(pal["col_header_bg"])
                cell.alignment = _align(wrap=True, vert="center")
                cell.border = _border_thin()
                _track_width(c_idx, hdr)
            ws.row_dimensions[row].height = fmt.header_row_height
            header_row = row
            row += 1

            # Freeze pane just below header
            if fmt.freeze_header:
                ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

            alt_counter = 0
            for data_row_vals in block.rows:
                is_alt = bool(alt_counter % 2)
                alt_counter += 1
                # Detect total/summary rows (first cell starts with "TOTAL", "Total", "NET")
                is_total = bool(
                    data_row_vals
                    and str(data_row_vals[0]).upper().startswith(("TOTAL", "NET", "GRAND"))
                )
                row_fill = (
                    _fill(pal["total_row_bg"])
                    if is_total
                    else (_fill(pal["alt_row_bg"]) if is_alt else None)
                )
                row_font = _font(bold=True, size=9) if is_total else _font(size=9)

                for c_idx in range(1, n_cols + 1):
                    val_str = (data_row_vals[c_idx - 1] if c_idx <= len(data_row_vals) else "")
                    is_num, num_val = _looks_like_number(val_str)
                    cell_val: Any = num_val if is_num else (val_str or None)
                    cell = ws.cell(row=row, column=c_idx, value=cell_val)
                    cell.font = row_font
                    if row_fill:
                        cell.fill = row_fill
                    cell.alignment = _align(wrap=fmt.wrap_text)
                    cell.border = _border_thin()
                    # Number format
                    if is_num:
                        if fmt.auto_currency and _looks_like_currency(val_str, fmt.currency_symbol):
                            cell.number_format = fmt.currency_format
                        else:
                            cell.number_format = fmt.number_format
                    _track_width(c_idx, val_str)
                ws.row_dimensions[row].height = fmt.data_row_height
                row += 1

            row += 1  # spacer after table

        # ── Section header (H3/H4) ────────────────────────────────────────
        elif isinstance(block, _SectionBlock):
            bg = pal["section_h3_bg"] if block.level == 3 else pal["subheader_bg"]
            fg = "FFFFFF" if block.level == 3 else "000000"
            cell = ws.cell(row=row, column=1, value=block.text)
            cell.font = _font(bold=True, size=10, color=fg)
            cell.fill = _fill(bg)
            cell.alignment = _align(wrap=False)
            ws.row_dimensions[row].height = fmt.section_row_height
            _track_width(1, block.text)
            row += 1

        # ── Separator ─────────────────────────────────────────────────────
        elif isinstance(block, _SeparatorBlock):
            ws.row_dimensions[row].height = 6
            row += 1

        # ── Paragraph / subheader ─────────────────────────────────────────
        elif isinstance(block, _ParagraphBlock):
            if block.bold:
                cell = ws.cell(row=row, column=1, value=block.text)
                cell.font = _font(bold=True, size=9)
                cell.fill = _fill(pal["subheader_bg"])
                cell.alignment = _align(wrap=fmt.wrap_text)
                ws.row_dimensions[row].height = fmt.data_row_height
            else:
                cell = ws.cell(row=row, column=1, value=block.text)
                cell.font = _font(italic=True, size=8, color=pal["note_text"])
                cell.alignment = _align(wrap=fmt.wrap_text)
                ws.row_dimensions[row].height = fmt.data_row_height
            _track_width(1, block.text)
            row += 1

        # ── Bullet list ───────────────────────────────────────────────────
        elif isinstance(block, _BulletBlock):
            for item in block.items:
                cell = ws.cell(row=row, column=1, value=f"  • {item}")
                cell.font = _font(size=9)
                cell.alignment = _align(wrap=fmt.wrap_text)
                ws.row_dimensions[row].height = fmt.data_row_height
                _track_width(1, item)
                row += 1

        # ── Code block ────────────────────────────────────────────────────
        elif isinstance(block, _CodeBlock):
            for code_line in block.lines:
                cell = ws.cell(row=row, column=1, value=code_line)
                cell.font = _font(size=8, name="Courier New", color="333333")
                cell.alignment = _align(wrap=False)
                ws.row_dimensions[row].height = fmt.data_row_height
                row += 1

    _apply_col_widths()


def _apply_advanced_features(ws: Any, fmt: XlsxFormatConfig) -> None:
    """Apply optional merged ranges, conditional formatting, and data validation."""
    for rng in fmt.merged_ranges:
        ws.merge_cells(rng)

    # Named Ranges (Workbook level)
    from openpyxl.workbook.defined_name import DefinedName
    wb = ws.parent
    for name, range_str in fmt.named_ranges.items():
        # Scoped to current worksheet by prefixing sheet name if not already present
        full_ref = range_str if "!" in range_str else f"'{ws.title}'!{range_str}"
        defn = DefinedName(name, attr_text=full_ref)
        wb.defined_names.add(defn)

    for rule in fmt.conditional_rules:
        rule_type = str(rule.get("type", "")).lower()
        cell_range = rule.get("range")
        if not cell_range:
            continue

        if rule_type == "cell_is":
            ws.conditional_formatting.add(
                cell_range,
                CellIsRule(
                    operator=rule.get("operator", "greaterThan"),
                    formula=rule.get("formula", ["0"]),
                    stopIfTrue=bool(rule.get("stop_if_true", False)),
                    fill=_fill(str(rule.get("fill", "FFF2CC"))),
                ),
            )
        elif rule_type == "color_scale":
            ws.conditional_formatting.add(
                cell_range,
                ColorScaleRule(
                    start_type=rule.get("start_type", "min"),
                    start_color=rule.get("start_color", "F8696B"),
                    mid_type=rule.get("mid_type", "percentile"),
                    mid_value=rule.get("mid_value", 50),
                    mid_color=rule.get("mid_color", "FFEB84"),
                    end_type=rule.get("end_type", "max"),
                    end_color=rule.get("end_color", "63BE7B"),
                ),
            )
        elif rule_type == "data_bar":
            ws.conditional_formatting.add(
                cell_range,
                DataBarRule(
                    start_type=rule.get("start_type", "min"),
                    end_type=rule.get("end_type", "max"),
                    color=rule.get("color", "638EC6"),
                ),
            )
        elif rule_type == "contains_text":
            needle = str(rule.get("text", ""))
            if needle:
                ws.conditional_formatting.add(
                    cell_range,
                    FormulaRule(
                        formula=[f'NOT(ISERROR(SEARCH("{needle}",A1)))'],
                        stopIfTrue=bool(rule.get("stop_if_true", False)),
                        fill=_fill(str(rule.get("fill", "E2F0D9"))),
                    ),
                )

    for cfg in fmt.data_validations:
        cell_range = cfg.get("range")
        if not cell_range:
            continue
        validation = DataValidation(
            type=cfg.get("type", "list"),
            operator=cfg.get("operator"),
            formula1=cfg.get("formula1"),
            formula2=cfg.get("formula2"),
            allow_blank=bool(cfg.get("allow_blank", True)),
            showErrorMessage=True,
            errorTitle=cfg.get("error_title", "Invalid Value"),
            error=cfg.get("error", "Value does not satisfy validation rule."),
        )
        ws.add_data_validation(validation)
        validation.add(cell_range)

    # Cell Comments
    from openpyxl.comments import Comment
    for cell_ref, text in fmt.cell_comments.items():
        ws[cell_ref].comment = Comment(text, "qdocs")


# ---------------------------------------------------------------------------
# Public converter
# ---------------------------------------------------------------------------

def convert_md_to_xlsx(
    source: Path,
    target: Path,
    *,
    cover: CoverPageConfig | None = None,
    revision: int = 1,
    fmt: XlsxFormatConfig | None = None,
    sheet_title: bool = True,
) -> None:
    """Convert a Markdown file to a multi-sheet XLSX workbook.

    Args:
        source:      Path to the source .md file.
        target:      Destination .xlsx path (parent dirs created if needed).
        cover:       Cover page config; ``None`` disables cover sheet.
        revision:    Revision number written to cover page.
        fmt:         Formatting/locale config.  Defaults to ``XlsxFormatConfig()``.
        sheet_title: Whether to render the H2 heading as a title row on the sheet.

    Raises:
        ConversionError: If openpyxl is unavailable or conversion fails.
    """
    if not _AVAILABLE:
        msg = "openpyxl not installed. Run: uv pip install openpyxl"
        raise ConversionError(msg)
    if not source.exists():
        msg = f"Source file not found: {source}"
        raise ConversionError(msg)

    fmt = fmt or XlsxFormatConfig()

    try:
        content = source.read_text(encoding="utf-8")
        doc_title, sheets = _parse_md(content)
        if not doc_title:
            doc_title = source.stem.replace("-", " ").replace("_", " ").title()

        wb = openpyxl.Workbook()
        # Remove the default empty sheet
        if "Sheet" in wb.sheetnames:
            del wb["Sheet"]

        # Cover sheet
        if cover and cover.enabled:
            _build_cover_sheet(wb, doc_title, cover, revision, fmt)

        # Data sheets
        for sheet_name, blocks in sheets:
            ws = wb.create_sheet(title=sheet_name or source.stem[:31])
            ws.sheet_view.showGridLines = False
            if fmt.tab_color:
                ws.sheet_properties.tabColor = fmt.tab_color
            title_for_sheet = sheet_name if sheet_title else ""
            _render_blocks(ws, blocks, fmt, sheet_title=title_for_sheet)
            _apply_advanced_features(ws, fmt)

        target.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(target))
        logger.debug(f"MD→XLSX: {source.name} → {target.name}  sheets={len(sheets)}")

    except ConversionError:
        raise
    except Exception as exc:
        msg = f"Failed to convert {source.name} to XLSX: {exc}"
        raise ConversionError(msg) from exc
