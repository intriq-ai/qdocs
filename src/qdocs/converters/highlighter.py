"""Syntax highlighting engine — Pygments tokens → ReportLab flowables and DOCX runs.

Strategy
--------
1.  ``scan_fenced_langs(markdown_src)`` pre-scans raw markdown for ````lang`` fences
    and returns ``{sha256(code): lang}`` so the language hint survives HTML conversion.
2.  ``highlight_to_pdf(code, lang, theme, page_w)`` tokenises with Pygments, resolves
    colours from the chosen style, and returns a ReportLab ``Table`` that gives a
    full-width coloured code block.
3.  ``highlight_to_docx_block(doc, code, lang, theme)`` adds coloured ``Run`` objects
    in a shaded table cell to a python-docx ``Document``.

Supported languages (aliases)
------------------------------
python / py, typescript / ts, javascript / js / jsx / tsx, yaml / yml,
json, hcl / terraform / tf / cfn / aws, bash / sh / shell / zsh,
sql, dockerfile / docker, mermaid → plain, pseudocode → python approx, text/plain.

Bundled themes (any Pygments style name is accepted)
------------------------------------------------------
``github-dark`` (default), ``one-dark``, ``monokai``, ``dracula``, ``nord``,
``solarized-dark``, ``github-light`` (alias → ``xcode``), ``friendly`` (print).
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any

from loguru import logger
from pygments import lex
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.token import Token

# ---------------------------------------------------------------------------
# Language alias table
# ---------------------------------------------------------------------------

_LANG_ALIASES: dict[str, str] = {
    # Python
    "py": "python",
    "python3": "python",
    # TypeScript / JS
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    # YAML
    "yml": "yaml",
    # Terraform / HCL / AWS CloudFormation
    "tf": "terraform",
    "hcl": "terraform",
    "cfn": "yaml",
    "aws": "yaml",
    "cdk": "typescript",
    # Shell
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    # Docker
    "dockerfile": "docker",
    # Mermaid — no upstream Pygments lexer; render plain
    "mermaid": "text",
    # Pseudocode — Python is the closest structural approximation
    "pseudocode": "python",
    "pseudo": "python",
    # Plain / fallback
    "plain": "text",
    "plaintext": "text",
    "none": "text",
    "": "text",
}

# Theme display aliases → Pygments style names
_THEME_ALIASES: dict[str, str] = {
    "github-light": "xcode",  # closest GitHub light equivalent
    "friendly-print": "friendly",  # low-contrast, good for printing
}

# ---------------------------------------------------------------------------
# Fenced-block pre-scanner
# ---------------------------------------------------------------------------

_FENCED_RE = re.compile(
    r"^[ \t]*`{3,}[ \t]*(?P<lang>[a-zA-Z0-9_\-+#]*)[ \t]*\r?\n(?P<code>.*?)^[ \t]*`{3,}[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def scan_fenced_langs(markdown_src: str) -> dict[str, str]:
    """Return ``{sha256_of_code: lang_hint}`` for every fenced block in the source.

    The hash key is used to reunite language information with code blocks
    after the markdown → HTML → BeautifulSoup pipeline has stripped fence info.
    Empty or missing lang hints default to ``"text"``.
    """
    result: dict[str, str] = {}
    for m in _FENCED_RE.finditer(markdown_src):
        lang = m.group("lang").strip().lower()
        code = m.group("code")
        key = hashlib.sha256(code.encode("utf-8")).hexdigest()
        result[key] = lang or "text"
    return result


def code_hash(code: str) -> str:
    """Return SHA256 hex of the given code string (for lang_map lookup)."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pygments helpers (all cached)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=32)
def _get_lexer(lang: str):
    """Return a cached Pygments lexer for the language alias."""
    normalized = _LANG_ALIASES.get(lang.lower(), lang.lower())
    try:
        return get_lexer_by_name(normalized, stripall=False)
    except Exception:
        return TextLexer()


@lru_cache(maxsize=16)
def _get_ttype_colors(style_name: str) -> tuple[dict, str, str]:
    """Return ``({_TokenType: hex_color}, bg_hex, fg_hex)`` from a Pygments style.

    Both bg_hex and fg_hex are guaranteed to be ``"#rrggbb"`` strings.
    """
    resolved = _THEME_ALIASES.get(style_name, style_name)
    try:
        style = get_style_by_name(resolved)
    except Exception:
        logger.warning(
            f"Unknown code theme '{style_name}', falling back to github-dark"
        )
        style = get_style_by_name("github-dark")

    bg = style.background_color or "#1e1e1e"
    # Default foreground: check the root Token style, fall back to a safe neutral
    root_attrs = style.style_for_token(Token)
    raw_fg = (root_attrs or {}).get("color") or ""
    fg = f"#{raw_fg}" if raw_fg else "#c9d1d9"

    ttype_colors: dict[Any, str] = {}
    for ttype, attrs in style:
        if attrs.get("color"):
            ttype_colors[ttype] = "#" + attrs["color"]

    return ttype_colors, bg, fg


@lru_cache(maxsize=16)
def _get_bold_ttypes(style_name: str) -> frozenset:
    """Return the set of token types that should be rendered bold."""
    resolved = _THEME_ALIASES.get(style_name, style_name)
    try:
        style = get_style_by_name(resolved)
    except Exception:
        style = get_style_by_name("github-dark")
    return frozenset(ttype for ttype, attrs in style if attrs.get("bold"))


def _color_for_ttype(ttype: Any, ttype_colors: dict, fg: str) -> str:
    """Walk the token type hierarchy to resolve the nearest colour."""
    t = ttype
    while t is not None:
        if t in ttype_colors:
            return ttype_colors[t]
        t = getattr(t, "parent", None)
    return fg


def _escape_xml(text: str) -> str:
    """Escape characters that are special in ReportLab paragraph XML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Core tokeniser → per-line output
# ---------------------------------------------------------------------------


def _tokenise_to_rl_lines(
    code: str,
    lang: str,
    theme: str,
) -> tuple[list[str], str, str]:
    """Tokenise code and return ``(rl_xml_lines, bg_hex, fg_hex)``.

    Each entry in ``rl_xml_lines`` is a string of ReportLab paragraph XML
    for one line, using ``<font name="Courier" color="...">`` fragments.
    Empty lines are represented as a single non-breaking space fragment.
    """
    ttype_colors, bg, fg = _get_ttype_colors(theme)
    bold_types = _get_bold_ttypes(theme)
    lexer = _get_lexer(lang)

    lines: list[str] = []
    current_parts: list[str] = []

    for ttype, value in lex(code, lexer):
        color = _color_for_ttype(ttype, ttype_colors, fg)
        is_bold = ttype in bold_types

        chunks = value.split("\n")
        for i, chunk in enumerate(chunks):
            if i > 0:
                # Newline: commit current line, start fresh
                lines.append("".join(current_parts) if current_parts else "")
                current_parts = []
            if chunk:
                esc = _escape_xml(chunk)
                # ReportLab Paragraph collapses whitespace in XML text nodes
                # (HTML-like behaviour).  Replace spaces and tabs with
                # non-breaking spaces so indentation is preserved exactly.
                esc = esc.replace("\t", "&#160;&#160;&#160;&#160;").replace(
                    " ", "&#160;"
                )
                bold_open = "<b>" if is_bold else ""
                bold_close = "</b>" if is_bold else ""
                current_parts.append(
                    f'{bold_open}<font name="Courier" color="{color}">{esc}</font>{bold_close}'
                )

    # Final line (no trailing newline)
    if current_parts:
        lines.append("".join(current_parts))

    # Strip trailing blank lines from Pygments output artifact
    while lines and not lines[-1]:
        lines.pop()

    return lines, bg, fg


# ---------------------------------------------------------------------------
# ReportLab PDF output
# ---------------------------------------------------------------------------


def highlight_to_pdf(
    code: str,
    lang: str,
    theme: str,
    page_w: float,
) -> list[Any]:
    """Return a list of ReportLab flowables for a syntax-highlighted code block.

    Uses a single-column ``Table`` so the background spans the full text width.

    Args:
        code:   Raw code string (no surrounding fences).
        lang:   Language hint (e.g. ``"python"``, ``"typescript"``).
        theme:  Pygments style name (e.g. ``"github-dark"``).
        page_w: Available text width in ReportLab points (e.g. ``6.5 * inch``).
    """
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    try:
        lines, bg, fg = _tokenise_to_rl_lines(code, lang, theme)
    except Exception as exc:
        logger.warning(f"Syntax highlight failed for '{lang}' ({theme}): {exc}")
        return _fallback_pdf_block(code)

    if not lines:
        return []

    line_style = ParagraphStyle(
        "HLLine",
        fontName="Courier",
        fontSize=8,
        leading=11,
        leftPadding=0,
        rightPadding=0,
        spaceAfter=0,
        spaceBefore=0,
        textColor=rl_colors.HexColor(fg),
    )

    # Build rows: one Paragraph per line; empty lines → non-breaking space
    rows = [
        [
            Paragraph(
                line
                if line.strip()
                else f'<font name="Courier" color="{bg}">&#160;</font>',
                line_style,
            )
        ]
        for line in lines
    ]
    n = len(rows)

    tbl = Table(rows, colWidths=[page_w])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), rl_colors.HexColor(bg)),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (0, 0), 6),  # first row top
                ("BOTTOMPADDING", (0, n - 1), (0, n - 1), 6),  # last row bottom
                ("TOPPADDING", (0, 1), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -2), 1),
            ]
        )
    )

    return [Spacer(1, 0.05 * inch), tbl, Spacer(1, 0.08 * inch)]


def _fallback_pdf_block(code: str) -> list[Any]:
    """Plain (no-colour) code block — used when highlighting fails."""
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Preformatted, Spacer

    style = ParagraphStyle(
        "CodeFallback",
        fontName="Courier",
        fontSize=8,
        leading=10,
        backColor=rl_colors.HexColor("#f5f5f5"),
        leftIndent=12,
        rightIndent=12,
        spaceBefore=4,
        spaceAfter=4,
    )
    import textwrap

    cleaned = textwrap.dedent(code).strip()
    return [
        Spacer(1, 0.05 * inch),
        Preformatted(cleaned, style),
        Spacer(1, 0.08 * inch),
    ]


# Regex for parsing ReportLab XML font fragments back into (text, color) pairs.
# Matches both plain <font ...>text</font> and bold-wrapped <b><font ...>text</font></b>.
_FONT_RUN_RE = re.compile(
    r"(?P<bold><b>)?<font[^>]*color=\"(?P<color>#[0-9a-fA-F]{6})\"[^>]*>(?P<text>[^<]*)</font>(?(bold)</b>|)",
    re.DOTALL,
)


def _shade_docx_cell(cell: Any, bg_hex: str) -> None:
    """Apply background shading to a python-docx table cell."""
    try:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        tc = cell._tc
        tc_pr = tc.get_or_add_tcPr()

        # Remove any existing shading
        for existing_shd in tc_pr.findall(qn("w:shd")):
            tc_pr.remove(existing_shd)

        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), bg_hex.lstrip("#").upper())
        tc_pr.append(shd)
    except Exception as exc:
        logger.debug(f"DOCX cell shading failed: {exc}")


def _strip_table_style(tbl: Any) -> None:
    """Remove any built-in table style so cell-level shading is not overridden."""
    try:
        from docx.oxml.ns import qn

        tbl_pr = tbl._tbl.get_or_add_tblPr()
        for tbl_style in tbl_pr.findall(qn("w:tblStyle")):
            tbl_pr.remove(tbl_style)
        # Also clear conditional formatting flags that Word uses to re-apply shading
        for tbl_look in tbl_pr.findall(qn("w:tblLook")):
            tbl_pr.remove(tbl_look)
    except Exception as exc:
        logger.debug(f"DOCX table style strip failed: {exc}")


def highlight_to_docx_block(doc: Any, code: str, lang: str, theme: str) -> None:
    """Add a syntax-highlighted code block to a python-docx ``Document``.

    Creates a single-column table with shaded background and per-token
    coloured ``Run`` objects set in Courier New 8pt.

    Args:
        doc:   python-docx ``Document`` instance.
        code:  Raw code text.
        lang:  Language hint.
        theme: Pygments style name.
    """
    from docx.shared import Pt, RGBColor

    try:
        lines, bg, fg = _tokenise_to_rl_lines(code.strip(), lang, theme)
    except Exception as exc:
        logger.warning(f"DOCX highlight failed for '{lang}': {exc}")
        _fallback_docx_block(doc, code)
        return

    if not lines:
        _fallback_docx_block(doc, code)
        return

    tbl = doc.add_table(rows=len(lines), cols=1)
    _strip_table_style(tbl)

    for row, line_xml in zip(tbl.rows, lines, strict=False):
        cell = row.cells[0]
        _shade_docx_cell(cell, bg)
        para = cell.paragraphs[0]
        para.clear()

        if not line_xml.strip():
            # Empty line — add a space run to preserve row height
            run = para.add_run(" ")
            run.font.name = "Courier New"
            run.font.size = Pt(8)
            continue

        # Re-tokenise this single line for DOCX (we can't parse RL XML easily)
        # Parse the XML fragments to extract (text, color) pairs
        for text, color_hex in _parse_rl_line_to_runs(line_xml, fg):
            if not text:
                continue
            run = para.add_run(text)
            run.font.name = "Courier New"
            run.font.size = Pt(8)
            try:
                r = int(color_hex[1:3], 16)
                g = int(color_hex[3:5], 16)
                b = int(color_hex[5:7], 16)
                run.font.color.rgb = RGBColor(r, g, b)
            except Exception:
                pass  # leave default color


def _parse_rl_line_to_runs(rl_xml: str, default_fg: str) -> list[tuple[str, str]]:
    """Extract ``[(text, hex_color)]`` pairs from a ReportLab XML line string.

    Parses ``<font name="Courier" color="#hex">text</font>`` fragments.
    """
    import re

    # Use a more robust regex that handles optional attributes and whitespace
    font_re = re.compile(
        r'<font[^>]*?color="(?P<color>#[0-9a-fA-F]{6})"[^>]*?>(?P<text>.*?)</font>', re.DOTALL
    )

    result = []
    last_end = 0

    # Strip bold tags as they complicate parsing and we don't support bold in DOCX yet
    clean_xml = rl_xml.replace("<b>", "").replace("</b>", "")

    for m in font_re.finditer(clean_xml):
        if m.start() > last_end:
            # Text outside font tags — use default fg
            plain = clean_xml[last_end : m.start()]
            if plain.strip():
                result.append((_unescape_xml(plain), default_fg))
        color = m.group("color")
        text = _unescape_xml(m.group("text"))
        if text:
            result.append((text, color))
        last_end = m.end()

    # Tail
    if last_end < len(clean_xml):
        tail = clean_xml[last_end:]
        if tail.strip():
            result.append((_unescape_xml(tail), default_fg))

    return result


def _unescape_xml(s: str) -> str:
    return (
        s.replace("&#160;", " ")  # non-breaking space → regular space (in code blocks)
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def _fallback_docx_block(doc: Any, code: str, dark_mode: bool = False) -> None:
    """Add plain monospace code block to DOCX when highlighting fails or is bypassed."""
    import textwrap

    from docx.shared import Pt, RGBColor

    tbl = doc.add_table(rows=1, cols=1)
    _strip_table_style(tbl)
    cell = tbl.cell(0, 0)

    if dark_mode:
        _shade_docx_cell(cell, "#1E1E1E")  # Dark gray/black background
        font_color = RGBColor(212, 212, 212)  # Light gray/white text
    else:
        _shade_docx_cell(cell, "#F5F5F5")  # Light gray background
        font_color = RGBColor(36, 41, 46)  # Dark text

    para = cell.paragraphs[0]
    para.clear()

    lines = textwrap.dedent(code).strip().splitlines()
    for i, line in enumerate(lines):
        if i > 0:
            para.add_run("\n")
        run = para.add_run(line or " ")
        run.font.name = "Courier New"
        run.font.size = Pt(8)
        run.font.color.rgb = font_color
