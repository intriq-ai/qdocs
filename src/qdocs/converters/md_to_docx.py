"""Markdown → DOCX converter using python-docx.

Cover page driven by CoverPageConfig (profile logo, company, classification,
author, version, revision).
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any

import markdown
from bs4 import BeautifulSoup, Tag
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path

    from docx.document import Document as DocumentType

from itertools import groupby as _groupby

from qdocs.converters.highlighter import (
    code_hash,
    highlight_to_docx_block,
    scan_fenced_langs,
)

# Import the canonical emoji → ASCII map from the PDF converter so both
# converters stay in sync without duplicating the mapping.
from qdocs.converters.md_to_pdf import _replace_emojis
from qdocs.converters.utils import find_image
from qdocs.exceptions import ConversionError
from qdocs.models.profile import CoverPageConfig, DocumentClassification


def _replace_mermaid_with_images(md_content: str, md_file: Path) -> str:
    """Replace ```mermaid blocks with ![](path) PNG references for DOCX embedding.

    Uses PNG only (not SVG) because python-docx cannot embed SVG images.
    Looks for pre-rendered PNGs in the adjacent __diagrams__/ folder.
    """
    import re as _re

    diagrams_dir = md_file.parent / "__diagrams__"
    if not diagrams_dir.exists():
        return md_content

    pattern = r"```mermaid\s*\n(.*?)\n```"
    matches = list(_re.finditer(pattern, md_content, _re.DOTALL))
    if not matches:
        return md_content

    result = md_content
    total = len(matches)
    for n, match in enumerate(reversed(matches), 1):
        idx = total - n + 1
        stem = md_file.stem
        # PNG only — SVG cannot be embedded by python-docx
        # PNG sidecars are stored in __diagrams__/png/
        path = diagrams_dir / "png" / f"{stem}_diagram_{idx}.png"
        if path.exists():
            img_md = f"\n![Diagram {idx}](__diagrams__/png/{path.name})\n"
            result = result[: match.start()] + img_md + result[match.end() :]
        else:
            logger.warning(f"DOCX diagram PNG not found: {path}")
    return result


# ---------------------------------------------------------------------------
# Metadata header parser (matches md_to_pdf behaviour)
# ---------------------------------------------------------------------------


def _parse_metadata_header(content: str) -> tuple[dict[str, str] | None, str]:
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


def _classification_color(cls: DocumentClassification) -> RGBColor:
    mapping = {
        DocumentClassification.PUBLIC: RGBColor(0, 100, 0),
        DocumentClassification.INTERNAL: RGBColor(26, 26, 140),
        DocumentClassification.CONFIDENTIAL: RGBColor(204, 0, 0),
        DocumentClassification.RESTRICTED: RGBColor(139, 69, 0),
        DocumentClassification.TOP_SECRET: RGBColor(75, 0, 0),
    }
    return mapping.get(cls, RGBColor(204, 0, 0))


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
        "SOC",
        "EEA",
    }
)


def _clean_title(stem: str) -> str:
    cleaned = re.sub(r"^[\d.]+-(?:P-)?", "", stem)
    cleaned = cleaned.replace("-", " ").replace("_", " ").title()
    words = cleaned.split()
    return " ".join(w.upper() if w.upper() in _ACRONYMS else w for w in words)


def _add_cover_badges(doc: DocumentType, badge_paths: list[Path]) -> None:
    """Render a small centered single-row badge table on the cover page."""
    if not badge_paths:
        return
    n = len(badge_paths)
    tbl = doc.add_table(rows=1, cols=n)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER  # type: ignore[attr-defined]
    for i, bp in enumerate(badge_paths):
        cell = tbl.rows[0].cells[i]
        para = cell.paragraphs[0]
        para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        try:
            run = para.add_run()
            run.add_picture(str(bp), width=Inches(1.5))
        except Exception as exc:
            logger.warning(f"Cover badge failed ({bp.name}): {exc}")
            para.add_run(bp.stem)
    doc.add_paragraph()


def _remove_table_borders(tbl: Any) -> None:
    """Set all borders on a table and its cells to invisible (0pt, no colour)."""
    border_sides = ("top", "left", "bottom", "right", "insideH", "insideV")

    def _make_border_el(name: str) -> Any:
        el = OxmlElement(f"w:{name}")
        el.set(qn("w:val"), "none")
        el.set(qn("w:sz"), "0")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "auto")
        return el

    # Table-level borders
    tbl_xml = tbl._tbl
    tbl_pr = tbl_xml.tblPr
    if tbl_pr is None:
        tbl_pr = OxmlElement("w:tblPr")
        tbl_xml.insert(0, tbl_pr)
    for existing in tbl_pr.findall(qn("w:tblBorders")):
        tbl_pr.remove(existing)
    tbl_borders = OxmlElement("w:tblBorders")
    for side in border_sides:
        tbl_borders.append(_make_border_el(side))
    tbl_pr.append(tbl_borders)

    # Cell-level borders — Word resolves these over table-level borders
    for row in tbl.rows:
        for cell in row.cells:
            tc = cell._tc
            tc_pr = tc.tcPr
            if tc_pr is None:
                tc_pr = OxmlElement("w:tcPr")
                tc.insert(0, tc_pr)
            for existing in tc_pr.findall(qn("w:tcBorders")):
                tc_pr.remove(existing)
            tc_borders = OxmlElement("w:tcBorders")
            for side in border_sides:
                tc_borders.append(_make_border_el(side))
            tc_pr.append(tc_borders)


def _add_cover_page(
    doc: DocumentType,
    source: Path,
    cover: CoverPageConfig,
    revision: int | None,
    title_override: str | None = None,
) -> None:
    """Add a cover page to the DOCX document."""
    # Vertical padding
    for _ in range(5):
        doc.add_paragraph()

    # Logo
    if cover.profile.logo_path and cover.profile.logo_path.exists():
        try:
            para = doc.add_paragraph()
            para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            para.add_run().add_picture(str(cover.profile.logo_path), width=Inches(1.5))
        except Exception as exc:
            logger.warning(f"DOCX cover logo failed: {exc}")

    for _ in range(2):
        doc.add_paragraph()

    # Chapter label
    if cover.chapter:
        p = doc.add_paragraph(cover.chapter)
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        if p.runs:
            p.runs[0].font.size = Pt(11)
            p.runs[0].italic = True
            p.runs[0].font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    # Title
    title = title_override if title_override is not None else _clean_title(source.stem)
    heading = doc.add_heading(title, level=0)
    heading.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER

    # Author
    if cover.author:
        p = doc.add_paragraph(f"Author: {cover.author}")
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER

    # Author email
    if cover.author_email:
        p = doc.add_paragraph(cover.author_email)
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        if p.runs:
            p.runs[0].font.size = Inches(0.12)
            p.runs[0].font.color.rgb = RGBColor(0x33, 0x33, 0x99)

    # Version
    if cover.show_version and cover.version_str:
        p = doc.add_paragraph(f"Version {cover.version_str}")
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        if p.runs:
            p.runs[0].font.size = Inches(0.15)

    # Date
    if cover.show_date:
        p = doc.add_paragraph(date.today().strftime("%d %B %Y"))
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        if p.runs:
            p.runs[0].font.size = Inches(0.12)

    # Badges — centred below date, above footer block
    if cover.profile.badge_paths:
        _add_cover_badges(doc, cover.profile.badge_paths)

    # Spacer and footer
    for _ in range(1):
        doc.add_paragraph()

    # Company
    if cover.profile.company_name:
        p = doc.add_paragraph(cover.profile.company_name)
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        if p.runs:
            p.runs[0].bold = True

    # Address
    if cover.profile.address:
        p = doc.add_paragraph(cover.profile.address)
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        if p.runs:
            p.runs[0].font.size = Inches(0.11)

    # Copyright
    if cover.profile.copyright:
        p = doc.add_paragraph(cover.profile.copyright)
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        if p.runs:
            p.runs[0].font.size = Inches(0.10)

    doc.add_paragraph()

    # Classification + revision
    footer_parts = [cover.classification.value]
    if cover.show_revision and revision is not None:
        footer_parts.append(f"Rev. {revision}")
    footer_text = "  ·  ".join(footer_parts)

    p = doc.add_paragraph(footer_text)
    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    if p.runs:
        p.runs[0].bold = True
        p.runs[0].font.color.rgb = _classification_color(cover.classification)


# ---------------------------------------------------------------------------
# Metadata table
# ---------------------------------------------------------------------------


def _add_metadata_table(doc: DocumentType, metadata: dict[str, str]) -> None:
    if not metadata:
        return
    tbl = doc.add_table(rows=len(metadata), cols=2)
    tbl.style = "Light Grid Accent 1"
    for i, (key, value) in enumerate(metadata.items()):
        row = tbl.rows[i]
        row.cells[0].text = key
        if row.cells[0].paragraphs[0].runs:
            row.cells[0].paragraphs[0].runs[0].bold = True
        row.cells[1].text = value
    doc.add_paragraph()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def convert_md_to_docx(
    source: Path,
    target: Path,
    cover: CoverPageConfig | None = None,
    revision: int | None = None,
    skip_header: bool = True,
    page_numbers: bool = True,
    page_breaks: bool = False,
) -> None:
    """Convert a markdown file to DOCX.

    Args:
        source: Source .md file.
        target: Destination .docx file.
        cover: Cover page config; defaults to CoverPageConfig.default() if None.
        revision: Revision number shown on cover page.
        skip_header: If True, metadata header table is omitted from body.
        page_breaks: Insert a page break after the cover page (default: False).

    Raises:
        ConversionError: On any failure.
    """
    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)

    if cover is None:
        cover = CoverPageConfig.default()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding="utf-8")
        metadata, body = _parse_metadata_header(content)
        render_content = body if skip_header else content

        # Replace ```mermaid blocks with PNG image references from __diagrams__/
        render_content = _replace_mermaid_with_images(render_content, source)

        lang_map = scan_fenced_langs(render_content)

        # Use the profile's code theme, or fallback to friendly if none is set
        code_theme = cover.profile.code_theme if cover and cover.profile else "friendly"

        # Use fenced_code NOT codehilite — codehilite pre-renders spans with CSS
        # classes that BeautifulSoup strips, losing all language and color info.
        # fenced_code preserves the language as a class on the <code> tag.
        html = markdown.markdown(
            render_content, extensions=["extra", "fenced_code", "tables"]
        )
        soup = BeautifulSoup(html, "html.parser")
        doc = Document()

        if cover.enabled:
            _add_cover_page(doc, source, cover, revision)
            if page_breaks:
                doc.add_page_break()

        if metadata and not skip_header:
            _add_metadata_table(doc, metadata)

        _render_soup_to_doc(doc, soup, source.parent, lang_map, code_theme)

        if page_numbers:
            _add_page_number_footer(doc)

        doc.save(str(target))
        logger.debug(f"DOCX: {source.name} → {target.name}")

    except Exception as exc:
        msg = f"Failed to convert {source.name} to DOCX: {exc}"
        raise ConversionError(msg) from exc


# ---------------------------------------------------------------------------
# Hyperlink helper
# ---------------------------------------------------------------------------


def _add_hyperlink(para: Any, url: str, text: str) -> None:
    """Append a clickable hyperlink run to *para* using OOXML relationships."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    try:
        r_id = para.part.relate_to(url, RT.HYPERLINK, is_external=True)
    except Exception:
        # Fallback: plain text
        para.add_run(text)
        return

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run_el = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    r_style = OxmlElement("w:rStyle")
    r_style.set(qn("w:val"), "Hyperlink")
    r_pr.append(r_style)
    run_el.append(r_pr)

    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run_el.append(t)

    hyperlink.append(run_el)
    para._p.append(hyperlink)


def _add_rich_content_to_para(
    para: Any, el: Any, source_dir: Path | None = None
) -> None:
    """Walk BeautifulSoup element children into *para* with inline formatting + hyperlinks."""
    from bs4 import NavigableString

    for child in el.children:
        if isinstance(child, NavigableString):
            txt = _replace_emojis(str(child))
            if txt:
                para.add_run(txt)
        elif child.name == "img" and source_dir is not None:
            src = (child.get("src") or "").strip()
            if src:
                img_path = find_image(src, source_dir)
                if img_path:
                    try:
                        from PIL import Image as _PILImage
                        _pil = _PILImage.open(str(img_path))
                        _pw, _ph = _pil.size
                        _pil.close()
                        # Cap at 1.2in for inline/table use; maintain aspect ratio
                        _max_in = 1.2
                        _nat_w_in = _pw / 96.0
                        _scale = min(1.0, _max_in / _nat_w_in)
                        _final_w = Inches(_nat_w_in * _scale)
                        _final_h = Inches((_ph / 96.0) * _scale)
                        run = para.add_run()
                        run.add_picture(str(img_path), width=_final_w, height=_final_h)
                    except Exception as exc:
                        logger.warning(f"Inline image failed ({src}): {exc}")
                        alt = child.get("alt") or src
                        para.add_run(f"[{alt}]")
        elif child.name == "a":
            href = (child.get("href") or "").strip()
            link_text = _replace_emojis(child.get_text())
            if href and link_text:
                _add_hyperlink(para, href, link_text)
            elif link_text:
                para.add_run(link_text)
        elif child.name in ("strong", "b"):
            run = para.add_run(_replace_emojis(child.get_text()))
            run.bold = True
        elif child.name in ("em", "i"):
            run = para.add_run(_replace_emojis(child.get_text()))
            run.italic = True
        elif child.name == "code":
            run = para.add_run(_replace_emojis(child.get_text()))
            run.font.name = "Courier New"
            run.font.size = Pt(8)
        else:
            _add_rich_content_to_para(para, child, source_dir)


# ---------------------------------------------------------------------------
# Body renderer helper
# ---------------------------------------------------------------------------


def _render_soup_to_doc(
    doc: Any,
    soup: BeautifulSoup,
    base_dir: Path,
    lang_map: dict[str, str],
    code_theme: str,
) -> None:
    """
    Walk the BeautifulSoup tree and append elements to the python-docx Document.
    """
    for element in soup.children:
        if not isinstance(element, Tag):
            continue

        tag = element.name
        if tag == "h1":
            h = doc.add_heading(_replace_emojis(element.get_text()), level=1)
            h.paragraph_format.space_before = Pt(20)
            h.paragraph_format.space_after = Pt(10)
        elif tag == "h2":
            h = doc.add_heading(_replace_emojis(element.get_text()), level=2)
            h.paragraph_format.space_before = Pt(16)
            h.paragraph_format.space_after = Pt(8)
        elif tag == "h3":
            h = doc.add_heading(_replace_emojis(element.get_text()), level=3)
            h.paragraph_format.space_before = Pt(14)
            h.paragraph_format.space_after = Pt(6)
        elif tag == "h4":
            h = doc.add_heading(_replace_emojis(element.get_text()), level=4)
            h.paragraph_format.space_before = Pt(10)
            h.paragraph_format.space_after = Pt(4)
        elif tag == "p":
            img = element.find("img")
            if img and img.get("src"):
                img_path = find_image(img["src"], base_dir)  # ty: ignore[invalid-argument-type]
                if img_path:
                    try:
                        from PIL import Image as _PILImage

                        _pil = _PILImage.open(str(img_path))
                        _pw, _ph = _pil.size
                        _pil.close()
                        _max_w_in = 5.5  # inches — A4 printable width with margins
                        _max_h_in = 3.5  # inches — reasonable max height
                        # Compute natural size at 96 dpi
                        _nat_w_in = _pw / 96.0
                        _nat_h_in = _ph / 96.0
                        # Scale down to fit; never upscale
                        _scale = min(
                            _max_w_in / _nat_w_in if _nat_w_in > _max_w_in else 1.0,
                            _max_h_in / _nat_h_in if _nat_h_in > _max_h_in else 1.0,
                        )
                        _final_w = Inches(_nat_w_in * _scale)
                        _final_h = Inches(_nat_h_in * _scale)
                        _para = doc.add_paragraph()
                        _para.alignment = 1  # WD_ALIGN_PARAGRAPH.CENTER
                        _run = _para.add_run()
                        _run.add_picture(str(img_path), width=_final_w, height=_final_h)
                    except Exception:
                        try:
                            doc.add_picture(str(img_path), width=Inches(5.5))
                        except Exception as exc:
                            logger.warning(f"DOCX image failed {img_path}: {exc}")
                            doc.add_paragraph(f"[Image: {img.get('alt', 'N/A')}]")
            else:
                para = doc.add_paragraph()
                _add_rich_content_to_para(para, element)
        elif tag == "ul":
            for li in element.find_all("li", recursive=False):
                para = doc.add_paragraph(style="List Bullet")
                para.paragraph_format.space_before = Pt(2)
                para.paragraph_format.space_after = Pt(4)
                _add_rich_content_to_para(para, li)
        elif tag == "ol":
            for li in element.find_all("li", recursive=False):
                para = doc.add_paragraph(style="List Number")
                para.paragraph_format.space_before = Pt(2)
                para.paragraph_format.space_after = Pt(4)
                _add_rich_content_to_para(para, li)
        elif tag in ("div", "pre"):
            code_el = element.find("code")
            src = (code_el.get_text() if code_el else element.get_text()).strip()
            lang = "text"
            if lang_map:
                lang = lang_map.get(code_hash(src + "\n")) or lang_map.get(
                    code_hash(src), "text"
                )
            if lang == "text" and code_el:
                for cls in code_el.get("class") or []:
                    if cls.startswith("language-"):
                        lang = cls[9:]
                        break
            highlight_to_docx_block(doc, src, lang, code_theme)
        elif tag == "table":
            rows = element.find_all("tr")
            if rows:
                # Detect image-only table: any cell in any data row contains an img
                # (robust — works even if cells have alt text or separator rows)
                data_cells = [c for r in rows for c in r.find_all(["th", "td"])]
                is_image_table = bool(data_cells) and any(
                    c.find("img") for c in data_cells
                )
                num_cols = max(len(r.find_all(["th", "td"])) for r in rows)
                tbl = doc.add_table(rows=len(rows), cols=num_cols)
                tbl.style = None if is_image_table else "Light Grid Accent 1"
                for i, row in enumerate(rows):
                    cells = row.find_all(["th", "td"])
                    for j, cell in enumerate(cells):
                        if j < num_cols:
                            cell_para = tbl.rows[i].cells[j].paragraphs[0]
                            for run in cell_para.runs:
                                run.text = ""
                            _add_rich_content_to_para(cell_para, cell, base_dir)
                # Apply border removal after cells are populated so tcPr elements exist
                if is_image_table:
                    _remove_table_borders(tbl)


# ---------------------------------------------------------------------------
# Page number footer
# ---------------------------------------------------------------------------


def _add_docx_toc(doc: DocumentType) -> None:
    """Insert a Word-native TOC field (auto-updates on document open in Word/LibreOffice).

    Levels: Heading1 (chapters) and Heading2 (sections) are included.
    """
    doc.add_heading("Table of Contents", level=1)
    para = doc.add_paragraph()
    run = para.add_run()

    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = ' TOC \\o "1-2" \\h \\z \\u '
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "[Right-click → Update Field to refresh Table of Contents]"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")

    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_sep)
    run._r.append(placeholder)
    run._r.append(fld_end)

    doc.add_paragraph()
    doc.add_page_break()


def _add_chapter_heading(doc: DocumentType, name: str, num: int) -> None:
    """Add a chapter divider heading (Heading1) for book DOCX exports."""
    doc.add_page_break()
    p = doc.add_paragraph(f"CHAPTER {num}")
    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    if p.runs:
        p.runs[0].font.size = Pt(11)
        p.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    heading = doc.add_heading(name, level=1)
    heading.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    doc.add_paragraph()
    doc.add_page_break()


def _group_by_chapter_docx(sources: list[Path]) -> list[tuple[str, list[Path]]]:
    """Group source files by parent directory (= chapter). Preserves sort order."""
    chapters: list[tuple[str, list[Path]]] = []
    for parent_dir, group in _groupby(sources, key=lambda p: p.parent):
        chapters.append((_clean_title(parent_dir.name), list(group)))
    return chapters


def _add_page_number_footer(doc: DocumentType) -> None:
    """Add 'Page N' at bottom-right of all section footers."""
    for section in doc.sections:
        footer = section.footer
        footer.is_linked_to_previous = False
        para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        para.alignment = WD_PARAGRAPH_ALIGNMENT.RIGHT

        run = para.add_run("Page ")
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(136, 136, 136)

        fld_run = para.add_run()
        fld_run.font.size = Pt(8)
        fld_run.font.color.rgb = RGBColor(136, 136, 136)

        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        fld_run._r.append(fld_begin)

        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = " PAGE "
        fld_run._r.append(instr)

        fld_sep = OxmlElement("w:fldChar")
        fld_sep.set(qn("w:fldCharType"), "separate")
        fld_run._r.append(fld_sep)

        placeholder = OxmlElement("w:t")
        placeholder.text = "1"
        fld_run._r.append(placeholder)

        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")
        fld_run._r.append(fld_end)


# ---------------------------------------------------------------------------
# Book export (multi-file → single DOCX)
# ---------------------------------------------------------------------------


def convert_book_to_docx(
    sources: list[Path],
    target: Path,
    cover: CoverPageConfig | None = None,
    revision: int | None = None,
    book_title: str | None = None,
    chapters: bool = False,
    page_numbers: bool = True,
    page_breaks: bool = False,
) -> None:
    """Combine multiple markdown files into a single DOCX book."""
    if not sources:
        msg = "No source files provided for DOCX book."
        raise ConversionError(msg)

    if cover is None:
        cover = CoverPageConfig.default()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = Document()

        if cover.enabled:
            _add_cover_page(doc, sources[0], cover, revision, book_title)
            doc.add_page_break()

        _add_docx_toc(doc)
        doc.add_page_break()

        # Use the profile's code theme, or fallback to friendly if none is set
        code_theme = cover.profile.code_theme if cover and cover.profile else "friendly"

        grouped = _group_by_chapter_docx(sources) if chapters else [("", sources)]
        multi_chapter = chapters and len(grouped) > 1
        document_index = 0
        for chapter_number, (chapter_name, chapter_sources) in enumerate(grouped, 1):
            if multi_chapter:
                _add_chapter_heading(doc, chapter_name, chapter_number)

            for source in chapter_sources:
                if not source.exists():
                    logger.warning(f"Book source not found, skipping: {source}")
                    continue
                if document_index > 0 and page_breaks:
                    doc.add_page_break()

                section_title = _clean_title(source.stem)
                heading = doc.add_heading(section_title, level=2 if multi_chapter else 1)
                heading.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT

                content = source.read_text(encoding="utf-8")
                _, body = _parse_metadata_header(content)
                lang_map = scan_fenced_langs(body)
                html = markdown.markdown(body, extensions=["extra", "fenced_code", "tables"])
                soup = BeautifulSoup(html, "html.parser")
                _render_soup_to_doc(doc, soup, source.parent, lang_map, code_theme)
                document_index += 1

        if page_numbers:
            _add_page_number_footer(doc)

        doc.save(str(target))
        logger.debug(f"Book DOCX: {len(sources)} files → {target.name}")

    except ConversionError:
        raise
    except Exception as exc:
        msg = f"Failed to build book DOCX: {exc}"
        raise ConversionError(msg) from exc
