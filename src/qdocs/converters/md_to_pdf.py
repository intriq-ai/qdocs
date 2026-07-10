"""Markdown → PDF converter using ReportLab.

Cover page driven by CoverPageConfig (profile logo, company, classification,
author, version, revision).  Mermaid blocks replaced with pre-rendered SVG/PNG
images from adjacent __diagrams__/ folder.
"""

from __future__ import annotations

import contextlib
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from itertools import groupby as _groupby
from pathlib import Path
from typing import Any

import markdown
from bs4 import BeautifulSoup, Tag
from loguru import logger
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    PageBreak,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents
from svglib.svglib import svg2rlg

from qdocs.converters.highlighter import (
    code_hash,
    highlight_to_pdf,
    scan_fenced_langs,
)
from qdocs.converters.utils import find_image
from qdocs.exceptions import ConversionError
from qdocs.models.profile import CoverPageConfig

# ---------------------------------------------------------------------------
# Cover page title font size thresholds
# ---------------------------------------------------------------------------
_TITLE_FS_SHORT = 28
_TITLE_FS_MED = 24
_TITLE_FS_LONG = 22
_TITLE_FS_XL = 18
_TITLE_T_SHORT = 40
_TITLE_T_MED = 60
_TITLE_T_LONG = 80
_TABLE_COLS_WIDE = 4
_TABLE_COLS_WIDER = 6

# ---------------------------------------------------------------------------
# SVG pre-processing helpers
# ---------------------------------------------------------------------------


def _convert_foreignobject_to_text(svg_path: Path) -> Path:
    """Replace SVG <foreignObject> (Mermaid HTML labels) with native <text>."""
    try:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
        tree = ET.parse(svg_path)
        root = tree.getroot()
        ns = {"svg": "http://www.w3.org/2000/svg"}

        for fo in root.findall(".//svg:foreignObject", ns):
            text_content = "".join(
                (e.text or "") + (e.tail or "") for e in fo.iter()
            ).strip()
            if not text_content:
                continue

            x = float(fo.get("x", 0))
            y = float(fo.get("y", 0))
            width = float(fo.get("width", 100))
            height = float(fo.get("height", 20))

            parent = next((p for p in root.iter() if fo in list(p)), None)
            if parent is None:
                continue

            lines = [ln.strip() for ln in text_content.split("\n") if ln.strip()]
            base_fs = 12
            max_chars = max(len(ln) for ln in lines) if lines else 1
            estimated_w = max_chars * base_fs * 0.6
            fs = (
                max(8, int(base_fs * (width * 0.9) / estimated_w))
                if estimated_w > width * 0.9
                else base_fs
            )
            line_h = fs * 1.2
            if len(lines) * line_h > height * 0.9:
                fs = max(7, int(fs * (height * 0.9) / (len(lines) * line_h)))
                line_h = fs * 1.2

            cx = x + width / 2
            cy = y + height / 2 - len(lines) * line_h / 2 + fs * 0.8
            text_el = ET.Element(
                "{http://www.w3.org/2000/svg}text",
                {
                    "x": str(cx),
                    "y": str(cy),
                    "text-anchor": "middle",
                    "font-family": "Arial,Helvetica,sans-serif",
                    "font-size": str(fs),
                    "fill": "#333",
                },
            )
            if len(lines) == 1:
                text_el.text = lines[0]
            else:
                for i, line in enumerate(lines):
                    ts = ET.SubElement(
                        text_el,
                        "{http://www.w3.org/2000/svg}tspan",
                        {"x": str(cx), "dy": str(line_h) if i > 0 else "0"},
                    )
                    ts.text = line

            idx = list(parent).index(fo)
            parent.remove(fo)
            parent.insert(idx, text_el)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".svg", delete=False, encoding="utf-8"
        ) as tmp:
            tree.write(tmp.name, encoding="unicode", xml_declaration=True)
        return Path(tmp.name)

    except Exception as exc:
        logger.warning(f"SVG foreignObject preprocessing failed for {svg_path}: {exc}")
        return svg_path


def _inject_svg_background(svg_path: Path) -> Path:
    """Insert a background <rect> for any background-color in the root <svg> style.

    svglib ignores the CSS background-color property on the root <svg> element.
    We materialise it as a <rect fill="..."> inserted as the first child so
    ReportLab renders the coloured background rectangle in the PDF.

    Returns the original path if no background-color is found or on error.
    Returns a new temp Path if a rect was injected (caller must unlink it).
    """
    import re as _re

    try:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
        tree = ET.parse(svg_path)
        root = tree.getroot()

        style = root.get("style", "")
        m = _re.search(r"background-color:\s*([^;\"]+)", style)
        if not m:
            return svg_path

        bg_color = m.group(1).strip()
        vb = root.get("viewBox", "")
        parts = vb.replace(",", " ").split()
        if len(parts) == 4:
            vx, vy, vw, vh = parts
        else:
            vw = root.get("width", "100%")
            vh = root.get("height", "100%")
            vx, vy = "0", "0"

        rect = ET.Element(
            "{http://www.w3.org/2000/svg}rect",
            {"x": vx, "y": vy, "width": vw, "height": vh, "fill": bg_color},
        )
        root.insert(0, rect)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".svg", delete=False, encoding="utf-8"
        ) as tmp:
            tree.write(tmp.name, encoding="unicode", xml_declaration=True)
        return Path(tmp.name)

    except Exception as exc:
        logger.warning(f"SVG background injection failed for {svg_path}: {exc}")
        return svg_path


def _fix_svg_dash_patterns(drawing: Any) -> Any:
    """Sanitise invalid ReportLab strokeDashArray values (recursively)."""
    if drawing is None:
        return drawing

    def fix(node: Any) -> None:
        if hasattr(node, "strokeDashArray") and node.strokeDashArray:
            fixed = [
                max(d, 1) for d in node.strokeDashArray if isinstance(d, (int, float))
            ]
            node.strokeDashArray = (
                fixed if fixed and any(d > 0 for d in fixed) else None
            )
        if hasattr(node, "contents"):
            for child in node.contents:
                fix(child)

    fix(drawing)
    return drawing


# ---------------------------------------------------------------------------
# Emoji / markup helpers
# ---------------------------------------------------------------------------

# Canonical mapping from Unicode emoji / symbols → ASCII/Latin-1 equivalents
# safe for Helvetica (the PDF font used throughout this converter).
#
# Helvetica covers Latin-1 (U+0000–U+00FF) plus a handful of common symbols
# from Postscript standard encoding.  Any codepoint outside that set renders
# as a filled black rectangle ("missing glyph" box).
#
# Rules:
#   • Prefer visually meaningful ASCII stand-ins (✅ → ✓, not "OK")
#   • Status / traffic-light emoji → filled/open circles or letters
#   • Variation Selector 16 (U+FE0F) and ZWJ sequences → stripped entirely
#   • Add new entries here; _replace_emojis() applies them all.
#
# NOTE: order matters when one entry is a prefix of another — longer strings
# must appear first.  We sort by length descending at runtime (see below).
#
EMOJI_REPLACEMENTS: dict[str, str] = {
    # ── Status / verdict ─────────────────────────────────────────────────────
    "✅": "✓",  # U+2705  WHITE HEAVY CHECK MARK
    "❌": "✗",  # U+274C  CROSS MARK
    "⚠️": "!",  # U+26A0 + U+FE0F  WARNING SIGN + variation selector
    "⚠": "!",  # U+26A0  WARNING SIGN (bare, no VS-16)
    "❓": "?",  # U+2753  BLACK QUESTION MARK ORNAMENT
    "❗": "!",  # U+2757  HEAVY EXCLAMATION MARK ORNAMENT
    "ℹ️": "i",  # U+2139 + U+FE0F  INFORMATION SOURCE
    "ℹ": "i",  # U+2139  bare
    # ── Traffic-light / coloured circles / squares ────────────────────────
    "🔴": "●",  # U+1F534  RED CIRCLE
    "🟠": "●",  # U+1F7E0  ORANGE CIRCLE
    "🟡": "●",  # U+1F7E1  YELLOW CIRCLE  ← was missing
    "🟢": "●",  # U+1F7E2  GREEN CIRCLE
    "🔵": "○",  # U+1F535  BLUE CIRCLE
    "⚫": "●",  # U+26AB  MEDIUM BLACK CIRCLE
    "⚪": "○",  # U+26AA  MEDIUM WHITE CIRCLE
    "🟥": "■",  # U+1F7E5  RED SQUARE
    "🟧": "■",  # U+1F7E7  ORANGE SQUARE
    "🟨": "■",  # U+1F7E8  YELLOW SQUARE
    "🟩": "■",  # U+1F7E9  GREEN SQUARE
    "🟦": "■",  # U+1F7E6  BLUE SQUARE
    "⬛": "■",  # U+2B1B  BLACK LARGE SQUARE
    "⬜": "□",  # U+2B1C  WHITE LARGE SQUARE
    # ── Documents / files / objects ──────────────────────────────────────
    "📝": "[Doc]",
    "📄": "[Doc]",
    "📁": "[Folder]",
    "📂": "[Folder]",
    "📊": "[Chart]",
    "📈": "[Up]",
    "📉": "[Down]",
    "📋": "[List]",
    "📚": "[Books]",
    "📅": "[Cal]",
    "📞": "[Tel]",
    "📧": "[Email]",
    "📦": "[Pkg]",
    "💡": "[Idea]",
    "🔑": "[Key]",
    # ── Symbols / actions ─────────────────────────────────────────────────
    "🔍": "[Search]",
    "🔗": "[Link]",
    "🔐": "[Lock]",
    "🔄": "[Sync]",
    "🚀": "[Launch]",
    "🚨": "[Alert]",
    "🎯": "[Target]",
    "🌍": "[Globe]",
    "🌐": "[Globe]",
    "⭐": "*",
    "🔔": "[Bell]",
    "✨": "*",
    # ── Unicode combining / invisible ────────────────────────────────────
    "\ufe0f": "",  # U+FE0F  VARIATION SELECTOR-16 (text/emoji presentation)
    "\u200b": "",  # U+200B  ZERO WIDTH SPACE
    "\u200d": "",  # U+200D  ZERO WIDTH JOINER
    "\u200c": "",  # U+200C  ZERO WIDTH NON-JOINER
    "\ufeff": "",  # U+FEFF  BYTE ORDER MARK / ZERO WIDTH NO-BREAK SPACE
}

# Pre-sort longest-first so multi-codepoint sequences (e.g. "⚠️" = ⚠ + VS16)
# are replaced before their shorter prefixes.
_EMOJI_SORTED: list[tuple[str, str]] = sorted(
    EMOJI_REPLACEMENTS.items(), key=lambda kv: len(kv[0]), reverse=True
)


def _replace_emojis(text: str) -> str:
    """Replace emoji / unsupported Unicode with Helvetica-safe ASCII equivalents."""
    for emoji, repl in _EMOJI_SORTED:
        text = text.replace(emoji, repl)
    return text


def _escape_reportlab_markup(text: str) -> str:
    """Escape patterns that ReportLab's XML paragraph parser would misread."""
    text = text.replace("[X]", "☑").replace("[x]", "☑")
    text = text.replace("[_]", "☐").replace("[ ]", "☐")
    text = re.sub(r"/\*", "/&#42;", text)
    text = re.sub(r"(\w+)_(\*)", r"\1&#95;\2", text)
    return re.sub(r"(\w+)\*(\s|$|,|;|\))", r"\1&#42;\2", text)


def _escape_reportlab_markup_safe(text: str) -> str:
    """Like _escape_reportlab_markup but preserves fenced code block content.

    Stashes ``` ... ``` blocks and inline `code` before applying escapes so
    that glob patterns (e.g. /vdr/**) inside code blocks are not corrupted.
    """
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00STASH{len(stash) - 1}\x00"

    # Stash fenced code blocks first, then inline code
    protected = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)
    protected = re.sub(r"`[^`\n]+`", _stash, protected)
    escaped = _escape_reportlab_markup(protected)
    for i, block in enumerate(stash):
        escaped = escaped.replace(f"\x00STASH{i}\x00", block)
    return escaped


def _validate_and_clean_markup(text: str) -> str:
    """Strip all XML tags if any tag pair is unbalanced."""
    for tag in ("i", "b", "u", "font", "a"):
        if len(re.findall(rf"<{tag}\b", text, re.IGNORECASE)) != len(
            re.findall(rf"</{tag}>", text, re.IGNORECASE)
        ):
            return re.sub(r"<[^>]+>", "", text)
    return text


def _html_to_reportlab_markup(element: Any) -> str:
    """Convert BeautifulSoup element tree → ReportLab <b>/<i>/<u>/<font> markup."""
    if isinstance(element, str):
        return element
    if not list(element.children):
        return element.get_text()

    parts: list[str] = []
    for child in element.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.name in {"strong", "b"}:
            inner = _html_to_reportlab_markup(child)
            parts.append(f"<b>{inner}</b>" if inner.strip() else inner)
        elif child.name in {"em", "i"}:
            inner = _html_to_reportlab_markup(child)
            if not inner.strip():
                pass
            elif "\n" in inner:
                parts.append(inner)
            else:
                parts.append(f"<i>{inner}</i>")
        elif child.name == "u":
            inner = _html_to_reportlab_markup(child)
            parts.append(f"<u>{inner}</u>" if inner.strip() else inner)
        elif child.name == "br":
            parts.append("<br/>")
        elif child.name == "code":
            raw = child.get_text()
            txt = (
                raw.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", " ")
                .strip()
            )
            parts.append(f"<font name='Courier'>{txt}</font>" if txt else "")
        elif child.name == "a":
            href = (child.get("href") or "").strip()
            inner = _html_to_reportlab_markup(child)
            if href and inner.strip():
                href_safe = href.replace("&", "&amp;").replace('"', "&quot;")
                parts.append(f'<a href="{href_safe}" color="#2563eb">{inner}</a>')
            else:
                parts.append(inner)
        else:
            parts.append(_html_to_reportlab_markup(child))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Metadata header parser
# ---------------------------------------------------------------------------


def _parse_metadata_header(content: str) -> tuple[dict[str, str] | None, str]:
    """Parse **Key:** Value metadata lines after H1, stopping at ## or ---."""
    lines = content.split("\n")
    metadata: dict[str, str] = {}
    meta_start: int | None = None
    meta_end: int | None = None
    found_h1 = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            found_h1 = True
            continue
        if found_h1 and stripped.startswith(("## ", "### ")):
            break
        if found_h1 and stripped.startswith("**") and ":**" in stripped:
            if meta_start is None:
                meta_start = i
            parts = stripped.split(":**", 1)
            if len(parts) == 2:
                key = parts[0].strip("* ")
                value = parts[1].strip()
                if key and value:
                    metadata[key] = value
        elif stripped == "---" and meta_start is not None:
            meta_end = i
            break

    if metadata and meta_end is not None:
        remaining = "\n".join(lines[:meta_start] + lines[meta_end + 1 :]).strip()
        return metadata, remaining
    return None, content


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------


def _create_cover_page(
    source: Path,
    cover: CoverPageConfig,
    revision: int | None,
    styles: Any,
    title_override: str | None = None,
) -> list[Any]:
    """Build cover page flowables from CoverPageConfig."""
    flowables: list[Any] = []
    flowables.append(Spacer(1, 2.0 * inch))

    # Logo
    if cover.profile.logo_path and cover.profile.logo_path.exists():
        try:
            from PIL import Image as PILImage

            pil = PILImage.open(cover.profile.logo_path)
            aspect = pil.width / pil.height
            w = 2.5 * inch
            logo = Image(str(cover.profile.logo_path), width=w, height=w / aspect)
            logo.hAlign = "CENTER"
            flowables.append(logo)
            flowables.append(Spacer(1, 0.5 * inch))
        except Exception as exc:
            logger.warning(f"Cover logo render failed: {exc}")

    # Chapter label (shown on cover when cover.chapter is set)
    if cover.chapter:
        flowables.append(
            Paragraph(
                cover.chapter,
                ParagraphStyle(
                    "CoverChapter",
                    parent=styles["Normal"],
                    fontSize=11,
                    textColor=colors.HexColor("#555555"),
                    alignment=TA_CENTER,
                    fontName="Helvetica-Oblique",
                    spaceAfter=6,
                ),
            )
        )
        flowables.append(Spacer(1, 0.1 * inch))

    # Title
    title = title_override if title_override is not None else _clean_title(source.stem)
    title_len = len(title)
    fs = (
        _TITLE_FS_SHORT
        if title_len <= _TITLE_T_SHORT
        else _TITLE_FS_MED
        if title_len <= _TITLE_T_MED
        else _TITLE_FS_LONG
        if title_len <= _TITLE_T_LONG
        else _TITLE_FS_XL
    )
    flowables.append(
        Paragraph(
            title,
            ParagraphStyle(
                "CoverTitle",
                parent=styles["Normal"],
                fontSize=fs,
                leading=fs + 4,
                textColor=colors.HexColor("#1a1a1a"),
                alignment=TA_CENTER,
                fontName="Helvetica-Bold",
                wordWrap="CJK",
            ),
        )
    )
    flowables.append(Spacer(1, 0.2 * inch))

    # Author
    if cover.author:
        flowables.append(
            Paragraph(
                f"Author: {cover.author}",
                ParagraphStyle(
                    "CoverAuthor",
                    parent=styles["Normal"],
                    fontSize=11,
                    textColor=colors.HexColor("#555555"),
                    alignment=TA_CENTER,
                ),
            )
        )
        flowables.append(Spacer(1, 0.1 * inch))

    # Version (in subtitle position, below title)
    if cover.show_version and cover.version_str:
        flowables.append(
            Paragraph(
                f"Version {cover.version_str}",
                ParagraphStyle(
                    "CoverVersion",
                    parent=styles["Normal"],
                    fontSize=13,
                    textColor=colors.HexColor("#666666"),
                    alignment=TA_CENTER,
                ),
            )
        )
        flowables.append(Spacer(1, 0.1 * inch))

    # Date
    if cover.show_date:
        flowables.append(
            Paragraph(
                date.today().strftime("%d %B %Y"),
                ParagraphStyle(
                    "CoverDate",
                    parent=styles["Normal"],
                    fontSize=10,
                    textColor=colors.HexColor("#888888"),
                    alignment=TA_CENTER,
                ),
            )
        )

    # Push footer down
    flowables.append(Spacer(1, 3.0 * inch))

    # Company name
    if cover.profile.company_name:
        flowables.append(
            Paragraph(
                cover.profile.company_name,
                ParagraphStyle(
                    "CoverCompany",
                    parent=styles["Normal"],
                    fontSize=11,
                    textColor=colors.HexColor("#333333"),
                    alignment=TA_CENTER,
                    fontName="Helvetica-Bold",
                    spaceAfter=4,
                ),
            )
        )

    # Address
    if cover.profile.address:
        flowables.append(
            Paragraph(
                cover.profile.address,
                ParagraphStyle(
                    "CoverAddress",
                    parent=styles["Normal"],
                    fontSize=9,
                    textColor=colors.HexColor("#666666"),
                    alignment=TA_CENTER,
                    spaceAfter=4,
                ),
            )
        )

    # Copyright
    if cover.profile.copyright:
        flowables.append(
            Paragraph(
                cover.profile.copyright,
                ParagraphStyle(
                    "CoverCopyright",
                    parent=styles["Normal"],
                    fontSize=8,
                    textColor=colors.HexColor("#666666"),
                    alignment=TA_CENTER,
                    spaceAfter=6,
                ),
            )
        )

    # Classification + revision footer line
    footer_parts: list[str] = [cover.classification.value]
    if cover.show_revision and revision is not None:
        footer_parts.append(f"Rev. {revision}")
    flowables.append(
        Paragraph(
            "  ·  ".join(footer_parts),
            ParagraphStyle(
                "CoverFooter",
                parent=styles["Normal"],
                fontSize=8,
                textColor=colors.HexColor(cover.classification.color_hex),
                alignment=TA_CENTER,
                fontName="Helvetica-Bold",
            ),
        )
    )

    return flowables


_ACRONYMS = frozenset(
    {
        "API",
        "APIS",
        "SDK",
        "AWS",
        "ECS",
        "EC2",
        "S3",
        "RDS",
        "VPC",
        "IAM",
        "KMS",
        "ACM",
        "ALB",
        "CDN",
        "ELB",
        "ASG",
        "ECR",
        "EKS",
        "SQS",
        "SNS",
        "SES",
        "CI",
        "CD",
        "CICD",
        "ML",
        "AI",
        "LLM",
        "NLP",
        "GPU",
        "CPU",
        "RAM",
        "SSH",
        "SSL",
        "TLS",
        "VPN",
        "DNS",
        "HTTP",
        "HTTPS",
        "MFA",
        "SSO",
        "RBAC",
        "ACL",
        "IDS",
        "IPS",
        "SIEM",
        "DLP",
        "DMARC",
        "SPF",
        "DKIM",
        "ISO",
        "GDPR",
        "CCPA",
        "PCI",
        "DSS",
        "HIPAA",
        "NIST",
        "CIS",
        "ISMS",
        "UK",
        "EU",
        "US",
        "IT",
        "HR",
        "QA",
        "BC",
        "DR",
        "BCP",
        "DRP",
        "RTO",
        "RPO",
        "MTTR",
        "PII",
        "PHI",
        "DSAR",
        "DPIA",
        "ROI",
        "KPI",
        "OKR",
        "ETL",
        "ERP",
        "CRM",
        "SDLC",
        "SAAS",
        "PAAS",
        "IAAS",
        "URL",
        "URI",
        "UUID",
        "DB",
        "UI",
        "UX",
        "CFO",
        "CTO",
        "CEO",
        "CISO",
        "PDF",
        "DOCX",
        "JSON",
        "YAML",
        "TOML",
        "XML",
        "HTML",
    }
)


def _clean_title(stem: str) -> str:
    """Convert a filename stem to a human-readable title, preserving acronyms."""
    # Strip leading module numbers: "6.2.1-" or "18.1.1-P-"
    cleaned = re.sub(r"^[\d.]+-(?:P-)?", "", stem)
    cleaned = cleaned.replace("-", " ").replace("_", " ").title()
    # Restore acronym capitalisation
    words = cleaned.split()
    return " ".join(w.upper() if w.upper() in _ACRONYMS else w for w in words)


# ---------------------------------------------------------------------------
# Mermaid block → image substitution
# ---------------------------------------------------------------------------


def _replace_mermaid_with_images(md_content: str, md_file: Path) -> str:
    """Replace ```mermaid blocks with ![](path) image references."""
    diagrams_dir = md_file.parent / "__diagrams__"
    if not diagrams_dir.exists():
        return md_content

    pattern = r"```mermaid\s*\n(.*?)\n```"
    matches = list(re.finditer(pattern, md_content, re.DOTALL))
    if not matches:
        return md_content

    result = md_content
    total = len(matches)
    for n, match in enumerate(reversed(matches), 1):
        idx = total - n + 1
        stem = md_file.stem
        for ext in ("svg", "png"):
            path = diagrams_dir / f"{stem}_diagram_{idx}.{ext}"
            if path.exists():
                img_md = f"\n![Diagram {idx}](__diagrams__/{path.name})\n"
                result = result[: match.start()] + img_md + result[match.end() :]
                break
        else:
            logger.warning(f"Diagram {idx} not found in {diagrams_dir}")
    return result


# ---------------------------------------------------------------------------
# Styles + layout helpers
# ---------------------------------------------------------------------------


def _setup_custom_styles(styles: Any) -> None:
    styles.add(
        ParagraphStyle(
            "CustomHeading1",
            parent=styles["Heading1"],
            fontSize=18,
            textColor=colors.HexColor("#2c3e50"),
            spaceAfter=14,
            spaceBefore=20,
        )
    )
    styles.add(
        ParagraphStyle(
            "CustomHeading2",
            parent=styles["Heading2"],
            fontSize=14,
            textColor=colors.HexColor("#34495e"),
            spaceAfter=12,
            spaceBefore=16,
        )
    )
    styles.add(
        ParagraphStyle(
            "CustomBody",
            parent=styles["BodyText"],
            fontSize=10,
            alignment=TA_LEFT,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            "ListItem",
            parent=styles["BodyText"],
            fontSize=10,
            leftIndent=20,
            bulletIndent=10,
            spaceAfter=4,
            spaceBefore=2,
            leading=15,
        )
    )
    styles.add(
        ParagraphStyle(
            "ChapterTitle",
            parent=styles["Normal"],
            fontSize=26,
            textColor=colors.HexColor("#1a1a1a"),
            spaceAfter=12,
            spaceBefore=12,
            fontName="Helvetica-Bold",
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            "ChapterLabel",
            parent=styles["Normal"],
            fontSize=11,
            textColor=colors.HexColor("#888888"),
            spaceAfter=8,
            fontName="Helvetica",
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            "TOCHeading",
            parent=styles["Heading1"],
            fontSize=16,
            textColor=colors.HexColor("#2c3e50"),
            spaceAfter=12,
            spaceBefore=12,
        )
    )


def _strip_blank_lines(text: str) -> str:
    """Strip leading/trailing blank lines while preserving per-line indentation."""
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _render_code_block(
    code_text: str,
    lang: str = "text",
    theme: str = "github-dark",
    page_w: float = 6.5 * inch,
    styles: Any = None,
) -> list[Any]:
    code = _strip_blank_lines(code_text)
    if not code:
        return []
    return highlight_to_pdf(code, lang, theme, page_w)


def _create_metadata_table(metadata: dict[str, str], styles: Any) -> list[Any]:
    if not metadata:
        return []
    data = [
        [Paragraph(f"<b>{k}</b>", styles["Normal"]), Paragraph(v, styles["Normal"])]
        for k, v in metadata.items()
    ]
    tbl = Table(data, colWidths=[2.5 * inch, 4.0 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return [tbl, Spacer(1, 0.3 * inch)]


# ---------------------------------------------------------------------------
# Markdown → flowables
# ---------------------------------------------------------------------------


def _markdown_to_flowables(
    md_content: str,
    md_dir: Path,
    styles: Any,
    landscape_mode: bool,
    lang_map: dict[str, str] | None = None,
    code_theme: str = "github-dark",
) -> list[Any]:
    """Convert document body markdown → list of ReportLab flowables."""
    md_content = _escape_reportlab_markup_safe(md_content)
    html = markdown.markdown(md_content, extensions=["extra", "codehilite", "tables"])
    soup = BeautifulSoup(html, "html.parser")
    flowables: list[Any] = []
    page_w = 10.2 * inch if landscape_mode else 6.5 * inch

    cell_style = ParagraphStyle(
        "TCell", parent=styles["BodyText"], fontSize=8, leading=10, alignment=TA_LEFT
    )
    hdr_style = ParagraphStyle(
        "THdr",
        parent=styles["BodyText"],
        fontSize=8,
        leading=10,
        fontName="Helvetica-Bold",
        textColor=colors.whitesmoke,
    )

    for el in soup.children:
        if not isinstance(el, Tag):
            continue

        tag = el.name

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            style_map = {
                1: "CustomHeading1",
                2: "CustomHeading2",
                3: "Heading3",
                4: "Heading4",
                5: "Heading5",
                6: "Heading6",
            }
            flowables.append(
                Paragraph(_replace_emojis(el.get_text()), styles[style_map[level]])
            )
            flowables.append(Spacer(1, 0.12 * inch))

        elif tag == "p":
            img = el.find("img")
            if img and img.get("src"):
                img_path = find_image(img["src"], md_dir)  # ty: ignore[invalid-argument-type]
                if img_path:
                    _render_image(img_path, flowables)
            else:
                visible = [
                    c for c in el.children if not (isinstance(c, str) and not c.strip())
                ]
                if (
                    len(visible) == 1
                    and isinstance(visible[0], Tag)
                    and visible[0].name == "code"
                    and "\n" in visible[0].get_text()
                ):
                    src_stripped = _strip_blank_lines(visible[0].get_text())
                    lang = "text"
                    if lang_map:
                        lang = lang_map.get(
                            code_hash(src_stripped + "\n"), None
                        ) or lang_map.get(code_hash(src_stripped), "text")
                    flowables.extend(
                        _render_code_block(
                            src_stripped, lang, code_theme, page_w, styles
                        )
                    )
                    continue
                markup = _validate_and_clean_markup(
                    _escape_reportlab_markup(
                        _replace_emojis(_html_to_reportlab_markup(el))
                    )
                )
                flowables.append(Paragraph(markup, styles["CustomBody"]))
                flowables.append(Spacer(1, 0.08 * inch))

        elif tag == "ul":
            for li in el.find_all("li", recursive=False):
                text = _validate_and_clean_markup(
                    _escape_reportlab_markup(
                        _replace_emojis(_html_to_reportlab_markup(li))
                    )
                )
                flowables.append(Paragraph(f"• {text}", styles["ListItem"]))
            flowables.append(Spacer(1, 0.12 * inch))

        elif tag == "ol":
            for i, li in enumerate(el.find_all("li", recursive=False), 1):
                text = _validate_and_clean_markup(
                    _escape_reportlab_markup(
                        _replace_emojis(_html_to_reportlab_markup(li))
                    )
                )
                flowables.append(Paragraph(f"{i}. {text}", styles["ListItem"]))
            flowables.append(Spacer(1, 0.12 * inch))

        elif tag in ("div", "pre"):
            code_el = el.find("code")
            src = code_el.get_text() if code_el else el.get_text()
            src_stripped = _strip_blank_lines(src)
            # Resolve language: check pre-scanned fenced-block map by code hash
            lang = "text"
            if lang_map:
                lang = lang_map.get(
                    code_hash(src_stripped + "\n"), None
                ) or lang_map.get(code_hash(src_stripped), "text")
            # Also check for class="language-X" on the code element
            if lang == "text" and code_el:
                for cls in code_el.get("class") or []:
                    if cls.startswith("language-"):
                        lang = cls[9:]
                        break
            flowables.extend(
                _render_code_block(src_stripped, lang, code_theme, page_w, styles)
            )

        elif tag == "hr":
            flowables.append(Spacer(1, 0.1 * inch))
            flowables.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
            flowables.append(Spacer(1, 0.1 * inch))

        elif tag == "table":
            rows = el.find_all("tr")
            if not rows:
                continue
            num_cols = max(len(r.find_all(["th", "td"])) for r in rows)
            col_w = page_w / num_cols
            col_widths = [col_w] * num_cols
            font_sz = (
                9
                if num_cols <= _TABLE_COLS_WIDE
                else 8
                if num_cols <= _TABLE_COLS_WIDER
                else 7
            )

            formatted: list[list] = []
            for i, row in enumerate(rows):
                cells = row.find_all(["th", "td"])
                style = (
                    hdr_style
                    if i == 0
                    else ParagraphStyle(
                        f"TC{i}",
                        parent=cell_style,
                        fontSize=font_sz,
                        leading=font_sz + 2,
                    )
                )
                safe_cells = [
                    Paragraph(
                        _replace_emojis(str(c.get_text().strip()))
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"),
                        style,
                    )
                    for c in cells
                ]
                # Pad to num_cols
                while len(safe_cells) < num_cols:
                    safe_cells.append(Paragraph("", cell_style))
                formatted.append(safe_cells)

            tbl = Table(formatted, colWidths=col_widths, repeatRows=1)
            tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#555555")),
                        ("BACKGROUND", (0, 1), (-1, -1), colors.beige),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            flowables.append(tbl)
            flowables.append(Spacer(1, 0.2 * inch))

    return flowables


_IMG_MAX_W = 6.0 * inch
_IMG_MAX_H = 3.2 * inch  # ≤ 1/3 of A4 usable height (~9.7 in)


def _render_image(img_path: Path, flowables: list) -> None:
    """Append an image flowable, handling SVG via svglib.

    Diagrams are capped at _IMG_MAX_W × _IMG_MAX_H (≤ 1/3 page height)
    while preserving aspect ratio.
    """
    try:
        if img_path.suffix.lower() == ".svg":
            processed = _convert_foreignobject_to_text(img_path)
            bg_injected = _inject_svg_background(processed)
            if processed not in (bg_injected, img_path):
                with contextlib.suppress(Exception):
                    processed.unlink()
            processed = bg_injected
            drawing = svg2rlg(str(processed))
            if processed != img_path:
                with contextlib.suppress(Exception):
                    processed.unlink()
            if drawing:
                drawing = _fix_svg_dash_patterns(drawing)
                scale = min(
                    _IMG_MAX_W / drawing.width, _IMG_MAX_H / drawing.height, 1.0
                )
                drawing.width *= scale
                drawing.height *= scale
                drawing.scale(scale, scale)
                drawing.hAlign = "CENTER"
                flowables.append(drawing)
                flowables.append(Spacer(1, 0.2 * inch))
        else:
            try:
                from PIL import Image as PILImage

                pil = PILImage.open(img_path)
                aspect = pil.width / pil.height
            except Exception:
                aspect = 4 / 3
            w = min(_IMG_MAX_W, _IMG_MAX_H * aspect)
            h = w / aspect
            img = Image(str(img_path), width=w, height=h)
            img.hAlign = "CENTER"
            flowables.append(img)
            flowables.append(Spacer(1, 0.2 * inch))
    except Exception as exc:
        logger.warning(f"Failed to render image {img_path}: {exc}")


# ---------------------------------------------------------------------------
# Footer canvas callback
# ---------------------------------------------------------------------------


def _make_footer_callback(page_numbers: bool, cover_enabled: bool) -> Any:
    """Create a ReportLab canvas callback that draws 'Page N' in footer bottom-right.

    Skips page 1 when cover is enabled (cover has no page number).
    Body pages are numbered from 1 (offset applied when cover is present).
    """

    def _footer(canvas: Any, doc: Any) -> None:
        if not page_numbers:
            return
        if cover_enabled and doc.page == 1:
            return
        display = (doc.page - 1) if cover_enabled else doc.page
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        page_w, _ = doc.pagesize
        canvas.drawRightString(page_w - 0.75 * inch, 0.5 * inch, f"Page {display}")
        canvas.restoreState()

    return _footer


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def convert_md_to_pdf(
    source: Path,
    target: Path,
    cover: CoverPageConfig | None = None,
    revision: int | None = None,
    skip_header: bool = True,
    landscape_mode: bool = False,
    page_numbers: bool = True,
) -> None:
    """Convert a markdown file to PDF.

    Args:
        source: Source .md file.
        target: Destination .pdf file.
        cover: Cover page config; defaults to CoverPageConfig.default() if None.
               Pass CoverPageConfig.disabled() to suppress the cover page.
        revision: Revision number shown on cover page.
        skip_header: If True, metadata header table is omitted from body.
        landscape_mode: Render in landscape A4 orientation.

    Raises:
        ConversionError: On any render failure.
    """
    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)

    if cover is None:
        cover = CoverPageConfig.default()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        styles = getSampleStyleSheet()
        _setup_custom_styles(styles)

        content = source.read_text(encoding="utf-8")
        content = _replace_mermaid_with_images(content, source)
        metadata, body = _parse_metadata_header(content)

        pagesize = landscape(A4) if landscape_mode else A4
        doc = SimpleDocTemplate(
            str(target),
            pagesize=pagesize,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=54,
        )

        story: list[Any] = []

        if cover.enabled:
            story.extend(_create_cover_page(source, cover, revision, styles))
            story.append(PageBreak())

        if metadata and not skip_header:
            story.extend(_create_metadata_table(metadata, styles))

        lang_map = scan_fenced_langs(body)
        code_theme = cover.profile.code_theme if cover else "github-dark"
        story.extend(
            _markdown_to_flowables(
                body, source.parent, styles, landscape_mode, lang_map, code_theme
            )
        )
        footer_fn = _make_footer_callback(page_numbers, cover.enabled)
        doc.build(story, onFirstPage=footer_fn, onLaterPages=footer_fn)
        logger.debug(f"PDF: {source.name} → {target.name}")

    except Exception as exc:
        msg = f"Failed to convert {source.name} to PDF: {exc}"
        raise ConversionError(msg) from exc


# ---------------------------------------------------------------------------
# Chapter page + TOC helpers
# ---------------------------------------------------------------------------


def _create_chapter_page(name: str, num: int, styles: Any) -> list[Any]:
    """Build a chapter divider page (large centered chapter name, full page)."""
    return [
        Spacer(1, 3.0 * inch),
        Paragraph(f"CHAPTER {num}", styles["ChapterLabel"]),
        Spacer(1, 0.2 * inch),
        HRFlowable(
            width="60%", thickness=2, color=colors.HexColor("#2c3e50"), hAlign="CENTER"
        ),
        Spacer(1, 0.25 * inch),
        Paragraph(name, styles["ChapterTitle"]),
        PageBreak(),
    ]


def _group_by_chapter(sources: list[Path]) -> list[tuple[str, list[Path]]]:
    """Group source files by parent directory (= chapter). Preserves sort order."""
    chapters: list[tuple[str, list[Path]]] = []
    for parent_dir, group in _groupby(sources, key=lambda p: p.parent):
        chapters.append((_clean_title(parent_dir.name), list(group)))
    return chapters


def _build_toc_flowables(styles: Any) -> tuple[list[Any], TableOfContents]:
    """Build the TOC page; returns (story flowables, toc object for doc template)."""
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle(
            "TOCChapter",
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=16,
            leftIndent=0,
            spaceBefore=6,
            spaceAfter=2,
            textColor=colors.HexColor("#1a1a1a"),
        ),
        ParagraphStyle(
            "TOCSection",
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            leftIndent=20,
            spaceBefore=1,
            spaceAfter=1,
            textColor=colors.HexColor("#333333"),
        ),
        ParagraphStyle(
            "TOCSubsection",
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            leftIndent=40,
            spaceBefore=0,
            spaceAfter=0,
            textColor=colors.HexColor("#555555"),
        ),
    ]
    return [
        Paragraph("Table of Contents", styles["TOCHeading"]),
        Spacer(1, 0.15 * inch),
        toc,
        PageBreak(),
    ], toc


class _TocDocTemplate(BaseDocTemplate):
    """BaseDocTemplate that emits TOC entries for ChapterTitle and heading paragraphs."""

    def __init__(
        self,
        filename: str,
        toc: TableOfContents,
        footer_fn: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(filename, **kwargs)
        self._toc_ref = toc
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="main",
        )
        self.addPageTemplates([PageTemplate("main", frames=[frame], onPage=footer_fn)])

    def afterFlowable(self, flowable: Any) -> None:
        """Emit TOC entries for chapter and heading paragraphs."""
        if not isinstance(flowable, Paragraph):
            return
        sname = flowable.style.name
        if sname == "ChapterTitle":
            self.notify("TOCEntry", (0, flowable.getPlainText(), self.page))
        elif sname == "CustomHeading1":
            self.notify("TOCEntry", (1, flowable.getPlainText(), self.page))
        elif sname == "CustomHeading2":
            self.notify("TOCEntry", (2, flowable.getPlainText(), self.page))


# ---------------------------------------------------------------------------
# Book export (multi-file → single PDF)
# ---------------------------------------------------------------------------


def convert_book_to_pdf(
    sources: list[Path],
    target: Path,
    cover: CoverPageConfig | None = None,
    book_title: str | None = None,
    page_numbers: bool = True,
    landscape_mode: bool = False,
    toc: bool = True,
) -> None:
    """Combine multiple markdown files into a single book PDF.

    Files are automatically grouped into chapters by parent directory.
    A Table of Contents page is inserted after the cover by default.

    Args:
        sources: Ordered list of .md source files.
        target: Destination .pdf file.
        cover: Cover page config; book_title overrides the title shown on cover.
        book_title: Title for the book cover page (default: parent folder name).
        page_numbers: Render 'Page N' in footer bottom-right (default True).
        landscape_mode: Render in landscape A4 orientation.
        toc: Insert a Table of Contents page after the cover (default True).

    Raises:
        ConversionError: On any render failure.
    """
    if not sources:
        msg = "No sources provided for book export"
        raise ConversionError(msg)

    if cover is None:
        cover = CoverPageConfig.default()

    title = book_title or _clean_title(sources[0].parent.name)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        styles = getSampleStyleSheet()
        _setup_custom_styles(styles)

        pagesize = landscape(A4) if landscape_mode else A4
        footer_fn = _make_footer_callback(page_numbers, cover.enabled)

        story: list[Any] = []
        toc_obj: TableOfContents | None = None

        if cover.enabled:
            story.extend(
                _create_cover_page(
                    sources[0],
                    cover,
                    revision=None,
                    styles=styles,
                    title_override=title,
                )
            )
            story.append(PageBreak())

        if toc:
            toc_flowables, toc_obj = _build_toc_flowables(styles)
            story.extend(toc_flowables)

        chapters = _group_by_chapter(sources)
        multi_chapter = len(chapters) > 1

        for chap_num, (chapter_name, chapter_sources) in enumerate(chapters, 1):
            if multi_chapter:
                story.extend(_create_chapter_page(chapter_name, chap_num, styles))

            for i, source in enumerate(chapter_sources):
                if not source.exists():
                    logger.warning(f"Book source not found, skipping: {source}")
                    continue

                if i > 0:
                    story.append(PageBreak())

                section_title = _clean_title(source.stem)
                story.append(Paragraph(section_title, styles["CustomHeading1"]))
                story.append(
                    HRFlowable(
                        width="100%", thickness=1, color=colors.HexColor("#cccccc")
                    )
                )
                story.append(Spacer(1, 0.15 * inch))

                content = source.read_text(encoding="utf-8")
                content = _replace_mermaid_with_images(content, source)
                _, body = _parse_metadata_header(content)
                lang_map = scan_fenced_langs(body)
                code_theme = cover.profile.code_theme
                story.extend(
                    _markdown_to_flowables(
                        body,
                        source.parent,
                        styles,
                        landscape_mode,
                        lang_map,
                        code_theme,
                    )
                )

        if toc and toc_obj is not None:
            doc = _TocDocTemplate(
                str(target),
                toc=toc_obj,
                footer_fn=footer_fn,
                pagesize=pagesize,
                rightMargin=72,
                leftMargin=72,
                topMargin=72,
                bottomMargin=54,
            )
            doc.multiBuild(story)
        else:
            doc = SimpleDocTemplate(
                str(target),
                pagesize=pagesize,
                rightMargin=72,
                leftMargin=72,
                topMargin=72,
                bottomMargin=54,
            )
            doc.build(story, onFirstPage=footer_fn, onLaterPages=footer_fn)

        logger.debug(f"Book PDF: {len(sources)} files → {target.name}")

    except ConversionError:
        raise
    except Exception as exc:
        msg = f"Failed to build book PDF: {exc}"
        raise ConversionError(msg) from exc
