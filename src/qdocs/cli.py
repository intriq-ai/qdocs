"""qdocs CLI — document conversion, export, revision management, and profiles.

Architecture: Clypi Command tree (async).

  qdocs convert  to-pdf | to-docx | to-md
  qdocs diagrams generate | check | export
  qdocs export
  qdocs revisions  show | bump | clear
  qdocs profile    show | init | list | set-logo
  qdocs cache      status | clear
"""

import shutil
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path
from typing import override

from clypi import Command, arg
from loguru import logger
from rich.console import Console
from rich.table import Table

from qdocs.config import QdocsSettings
from qdocs.converters import (
    check_mmdc,
    convert_docx_to_md,
    convert_docx_to_pdf,
    convert_md_to_docx,
    convert_md_to_pdf,
    convert_md_to_xlsx,
    convert_pdf_to_docx,
    convert_pdf_to_md,
    convert_pptx_to_md,
    convert_xlsx_to_md,
    export_mermaid_blocks,
    generate_diagrams,
    generate_diagrams_cached,
    get_file_hash,
)
from qdocs.converters.md_to_xlsx import XlsxFormatConfig
from qdocs.exceptions import (
    CacheError,
    ConfigError,
    ConversionError,
    MmcdNotFoundError,
    ProfileError,
    QdocsError,
    RevisionError,
    SourceNotFoundError,
)
from qdocs.models.profile import DocumentClassification
from qdocs.services import ExportService, ProfileManager, RevisionManager

console = Console()


# ===========================================================================
# Internal helpers
# ===========================================================================


def _bootstrap_exports_dir(exports_dir: Path) -> None:
    """Ensure exports/ contains .gitkeep and .gitignore on first creation."""
    exports_dir.mkdir(parents=True, exist_ok=True)
    gitkeep = exports_dir / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.touch()
    gitignore = exports_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# Ignore all generated exports — keep only the marker files\n"
            "*\n"
            "!.gitignore\n"
            "!.gitkeep\n",
            encoding="utf-8",
        )


# ===========================================================================
# Helpers
# ===========================================================================


def _load_settings() -> QdocsSettings:
    return QdocsSettings.load()


def _read_version_from_md(source: Path) -> str | None:
    """Extract the Version field from a markdown metadata header, if present.

    Looks for a line matching: **Version:** <value>
    Returns the value string, or None if not found.
    """
    try:
        import re as _re

        content = source.read_text(encoding="utf-8")
        m = _re.search(r"^\*\*Version:\*\*\s*(.+)$", content, _re.MULTILINE)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def _classification_choices() -> str:
    return " | ".join(c.value for c in DocumentClassification)


# ===========================================================================
# qdocs convert
# ===========================================================================


class ToPdf(Command):
    """Convert markdown file(s) to PDF."""

    source: Path = arg(help="Source .md file or directory")
    target: Path | None = arg(
        default=None, help="Output file or directory (default: alongside source)"
    )
    pattern: str = arg(default="*.md", help="Glob pattern for batch mode")
    recursive: bool = arg(default=True, help="Recurse into subdirectories")
    profile: str = arg(default="default", help="Profile name for cover page branding")
    version: str | None = arg(default=None, help="Version string shown on cover page")
    author: str | None = arg(
        default=None, help="Author name shown on cover page (default: from profile)"
    )
    classification: str | None = arg(
        default=None,
        help=f"Classification label: {_classification_choices()} (default: from profile)",
    )
    theme: str | None = arg(
        default=None,
        help="Pygments theme (github-dark, monokai, one-dark, solarized-light). Overrides profile.",
    )
    no_cover: bool = arg(default=False, help="Suppress cover page")
    no_version: bool = arg(default=False, help="Hide version from cover page")
    no_author: bool = arg(default=False, help="Hide author from cover page")
    force: bool = arg(default=False, help="Force re-conversion ignoring cache")
    landscape: bool = arg(default=False, help="Landscape page orientation")
    no_page_numbers: bool = arg(
        default=False, help="Disable page numbers in footer (default: enabled)"
    )
    chapter: str | None = arg(
        default=None,
        help="Chapter label shown on cover page (e.g. 'Chapter 3: Django')",
    )

    @override
    async def run(self) -> None:
        settings = _load_settings()
        profile_mgr = ProfileManager(settings.profile_path)
        revision_mgr = RevisionManager(settings.cache_path)

        classification = (
            DocumentClassification(self.classification.upper())
            if self.classification
            else None
        )
        # Auto-read Version from document metadata if not supplied via CLI
        resolved_version = self.version or (
            _read_version_from_md(self.source) if self.source.is_file() else None
        )
        cover = profile_mgr.build_cover_config(
            self.profile,
            enabled=not self.no_cover,
            version_str=resolved_version,
            show_version=not self.no_version,
            classification=classification,
            author="" if self.no_author else self.author,
            chapter=self.chapter,
            code_theme=self.theme,
        )

        if self.source.is_dir():
            files = (
                list(self.source.rglob(self.pattern))
                if self.recursive
                else list(self.source.glob(self.pattern))
            )
            out_dir = self.target or (self.source.parent / f"{self.source.name}-pdf")
            out_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"[cyan]Converting {len(files)} file(s) to PDF...[/cyan]")
            ok = 0
            for f in sorted(files):
                rel = f.relative_to(self.source)
                out_file = out_dir / rel.with_suffix(".pdf")
                out_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _gen_diagrams(f, settings)
                    file_hash = get_file_hash(f)
                    rev = (
                        revision_mgr.bump(f, file_hash).revision
                        if self.force
                        else revision_mgr.record(f, file_hash).revision
                    )
                    file_version = self.version or _read_version_from_md(f)
                    file_cover = (
                        cover.model_copy(update={"version_str": file_version})
                        if file_version != resolved_version
                        else cover
                    )
                    convert_md_to_pdf(
                        f,
                        out_file,
                        cover=file_cover,
                        revision=rev,
                        landscape_mode=self.landscape,
                        page_numbers=not self.no_page_numbers,
                    )
                    ok += 1
                    console.print(f"  [green]✓[/green] {rel}")
                except Exception as exc:
                    console.print(f"  [red]✗[/red] {rel}: {exc}")
            console.print(f"[green]Done: {ok}/{len(files)}[/green]")
        else:
            out_file = self.target or (
                settings.output_dir / self.source.with_suffix(".pdf").name
            )
            out_file.parent.mkdir(parents=True, exist_ok=True)
            _gen_diagrams(self.source, settings)
            file_hash = get_file_hash(self.source)
            rev = (
                revision_mgr.bump(self.source, file_hash).revision
                if self.force
                else revision_mgr.record(self.source, file_hash).revision
            )
            convert_md_to_pdf(
                self.source,
                out_file,
                cover=cover,
                revision=rev,
                landscape_mode=self.landscape,
                page_numbers=not self.no_page_numbers,
            )
            console.print(f"[green]✓[/green] {self.source.name} → {out_file} (r{rev})")


class ToDocx(Command):
    """Convert markdown file(s) to DOCX."""

    source: Path = arg(help="Source .md file or directory")
    target: Path | None = arg(default=None, help="Output file or directory")
    pattern: str = arg(default="*.md", help="Glob pattern for batch mode")
    recursive: bool = arg(default=True, help="Recurse into subdirectories")
    profile: str = arg(default="default", help="Profile name")
    version: str | None = arg(default=None, help="Version string on cover page")
    author: str | None = arg(
        default=None, help="Author name on cover page (default: from profile)"
    )
    classification: str | None = arg(
        default=None,
        help=f"Classification label: {_classification_choices()} (default: from profile)",
    )
    theme: str | None = arg(
        default=None, help="Syntax highlight theme. Overrides profile."
    )
    no_cover: bool = arg(default=False, help="Suppress cover page")
    no_version: bool = arg(default=False, help="Hide version from cover page")
    no_author: bool = arg(default=False, help="Hide author from cover page")
    force: bool = arg(default=False, help="Force re-conversion")
    skip_header: bool = arg(default=True, help="Skip metadata header table")
    no_page_numbers: bool = arg(
        default=False, help="Disable page numbers in footer (default: enabled)"
    )
    page_breaks: bool = arg(
        default=False, help="Insert page break after cover page (default: off)"
    )
    chapter: str | None = arg(default=None, help="Chapter label shown on cover page")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        profile_mgr = ProfileManager(settings.profile_path)
        revision_mgr = RevisionManager(settings.cache_path)

        classification = (
            DocumentClassification(self.classification.upper())
            if self.classification
            else None
        )
        # Auto-read Version from document metadata if not supplied via CLI
        resolved_version = self.version or (
            _read_version_from_md(self.source) if self.source.is_file() else None
        )
        cover = profile_mgr.build_cover_config(
            self.profile,
            enabled=not self.no_cover,
            version_str=resolved_version,
            show_version=not self.no_version,
            classification=classification,
            author="" if self.no_author else self.author,
            chapter=self.chapter,
            code_theme=self.theme,
        )

        if self.source.is_dir():
            files = (
                list(self.source.rglob(self.pattern))
                if self.recursive
                else list(self.source.glob(self.pattern))
            )
            out_dir = self.target or (self.source.parent / f"{self.source.name}-docx")
            out_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"[cyan]Converting {len(files)} file(s) to DOCX...[/cyan]")
            ok = 0
            for f in sorted(files):
                rel = f.relative_to(self.source)
                out_file = out_dir / rel.with_suffix(".docx")
                out_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _gen_diagrams(f, settings, background="white")
                    file_hash = get_file_hash(f)
                    rev = (
                        revision_mgr.bump(f, file_hash).revision
                        if self.force
                        else revision_mgr.record(f, file_hash).revision
                    )
                    file_version = self.version or _read_version_from_md(f)
                    file_cover = cover.model_copy(update={"version_str": file_version}) if file_version != resolved_version else cover
                    convert_md_to_docx(
                        f,
                        out_file,
                        cover=file_cover,
                        revision=rev,
                        skip_header=self.skip_header,
                        page_numbers=not self.no_page_numbers,
                        page_breaks=self.page_breaks,
                    )
                    ok += 1
                    console.print(f"  [green]✓[/green] {rel}")
                except Exception as exc:
                    console.print(f"  [red]✗[/red] {rel}: {exc}")
            console.print(f"[green]Done: {ok}/{len(files)}[/green]")
        else:
            out_file = self.target or (
                settings.output_dir / self.source.with_suffix(".docx").name
            )
            out_file.parent.mkdir(parents=True, exist_ok=True)
            _gen_diagrams(self.source, settings, background="white")
            file_hash = get_file_hash(self.source)
            rev = (
                revision_mgr.bump(self.source, file_hash).revision
                if self.force
                else revision_mgr.record(self.source, file_hash).revision
            )
            convert_md_to_docx(
                self.source,
                out_file,
                cover=cover,
                revision=rev,
                skip_header=self.skip_header,
                page_numbers=not self.no_page_numbers,
                page_breaks=self.page_breaks,
            )
            console.print(f"[green]✓[/green] {self.source.name} → {out_file} (r{rev})")


def _next_versioned_dir(base: Path) -> tuple[Path, int]:
    """Return (next_versioned_dir, version_number) for a base output path.

    Scans for ``base-v1``, ``base-v2``, … and returns the first absent slot,
    ensuring each conversion gets a unique directory even if the input is
    unchanged.
    """
    v = 1
    while (base.parent / f"{base.name}-v{v}").exists():
        v += 1
    return base.parent / f"{base.name}-v{v}", v



# ===========================================================================
# qdocs convert to-xlsx
# ===========================================================================


class ToXlsx(Command):
    """Convert markdown file(s) to a multi-sheet XLSX workbook.

    Each H2 heading (## Sheet Name) in the Markdown creates a new worksheet.
    Tables, sections, paragraphs, and bullet lists are rendered with full styling.
    A branded cover sheet is prepended unless --no-cover is set.
    Formatting is driven by locale/currency/column-width flags.
    """

    source: Path = arg(help="Source .md file or directory")
    target: Path | None = arg(default=None, help="Output .xlsx file or directory")
    pattern: str = arg(default="*.md", help="Glob pattern for batch mode")
    recursive: bool = arg(default=True, help="Recurse into subdirectories")
    profile: str = arg(default="default", help="Profile name")
    version: str | None = arg(default=None, help="Version string on cover sheet")
    author: str | None = arg(
        default=None, help="Author name on cover sheet (default: from profile)"
    )
    classification: str | None = arg(
        default=None,
        help=f"Classification label: {_classification_choices()} (default: from profile)",
    )
    chapter: str | None = arg(default=None, help="Chapter label on cover sheet")
    no_cover: bool = arg(default=False, help="Suppress cover sheet")
    no_version: bool = arg(default=False, help="Hide version from cover sheet")
    no_author: bool = arg(default=False, help="Hide author from cover sheet")
    force: bool = arg(default=False, help="Force re-conversion")
    currency: str = arg(default="$", help="Currency symbol (default: $)")
    currency_format: str = arg(
        default="#,##0.00",
        help="Excel currency number format string (default: #,##0.00)",
    )
    number_format: str = arg(
        default="#,##0",
        help="Excel integer number format string (default: #,##0)",
    )
    date_format: str = arg(
        default="YYYY-MM-DD",
        help="Excel date format string (default: YYYY-MM-DD)",
    )
    locale: str = arg(
        default="en_US",
        help="Locale identifier (informational, default: en_US)",
    )
    auto_width: bool = arg(default=True, help="Auto-fit column widths from content")
    max_col_width: float = arg(default=60.0, help="Maximum column width in characters")
    freeze_header: bool = arg(
        default=True, help="Freeze the column header row on each data sheet"
    )
    wrap_text: bool = arg(default=True, help="Wrap text in data cells")
    no_sheet_title: bool = arg(
        default=False, help="Suppress the H2 heading as a title row on each sheet"
    )

    @override
    async def run(self) -> None:
        settings = _load_settings()
        profile_mgr = ProfileManager(settings.profile_path)
        revision_mgr = RevisionManager(settings.cache_path)

        classification = (
            DocumentClassification(self.classification.upper())
            if self.classification
            else None
        )
        resolved_version = self.version or (
            _read_version_from_md(self.source) if self.source.is_file() else None
        )
        cover = profile_mgr.build_cover_config(
            self.profile,
            enabled=not self.no_cover,
            version_str=resolved_version,
            show_version=not self.no_version,
            classification=classification,
            author="" if self.no_author else self.author,
            chapter=self.chapter,
        )

        fmt = XlsxFormatConfig(
            currency_symbol=self.currency,
            currency_format=self.currency_format,
            number_format=self.number_format,
            date_format=self.date_format,
            locale=self.locale,
            auto_width=self.auto_width,
            max_col_width=self.max_col_width,
            freeze_header=self.freeze_header,
            wrap_text=self.wrap_text,
        )

        if self.source.is_dir():
            files = (
                list(self.source.rglob(self.pattern))
                if self.recursive
                else list(self.source.glob(self.pattern))
            )
            out_dir = self.target or (self.source.parent / f"{self.source.name}-xlsx")
            out_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"[cyan]Converting {len(files)} file(s) to XLSX...[/cyan]")
            ok = 0
            for f in sorted(files):
                rel = f.relative_to(self.source)
                out_file = out_dir / rel.with_suffix(".xlsx")
                out_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    file_hash = get_file_hash(f)
                    rev = (
                        revision_mgr.bump(f, file_hash).revision
                        if self.force
                        else revision_mgr.record(f, file_hash).revision
                    )
                    file_version = self.version or _read_version_from_md(f)
                    file_cover = (
                        cover.model_copy(update={"version_str": file_version})
                        if file_version != resolved_version
                        else cover
                    )
                    convert_md_to_xlsx(
                        f,
                        out_file,
                        cover=file_cover,
                        revision=rev,
                        fmt=fmt,
                        sheet_title=not self.no_sheet_title,
                    )
                    ok += 1
                    console.print(f"  [green]✓[/green] {rel}")
                except Exception as exc:
                    console.print(f"  [red]✗[/red] {rel}: {exc}")
            console.print(f"[green]Done: {ok}/{len(files)}[/green]")
        else:
            out_file = self.target or (
                settings.output_dir / self.source.with_suffix(".xlsx").name
            )
            out_file.parent.mkdir(parents=True, exist_ok=True)
            file_hash = get_file_hash(self.source)
            rev = (
                revision_mgr.bump(self.source, file_hash).revision
                if self.force
                else revision_mgr.record(self.source, file_hash).revision
            )
            convert_md_to_xlsx(
                self.source,
                out_file,
                cover=cover,
                revision=rev,
                fmt=fmt,
                sheet_title=not self.no_sheet_title,
            )
            console.print(f"[green]✓[/green] {self.source.name} → {out_file} (r{rev})")


class ToMd(Command):
    """Convert DOCX / PDF / XLSX / PPTX to Markdown.

    For PDF files the conversion provider is selected by --provider:

    auto (default)  Check for a valid AWS MFA session.  If one exists, use
                    AWS Textract (superior OCR, handles scanned documents and
                    complex tables).  Fall back to pdfplumber automatically
                    when no session is available.

    textract        Force AWS Textract.  Fails immediately if no valid MFA
                    session is found — run ``eval $(qmfa)`` first.

    pdfplumber      Force pdfplumber (local, no AWS required).  Best for
                    text-native PDFs where OCR is not needed.
    """

    source: Path = arg(help="Source file (.docx, .pdf, .xlsx, .pptx)")
    target: Path | None = arg(default=None, help="Output .md file")
    provider: str = arg(
        default="auto",
        help="PDF conversion provider: auto | textract | pdfplumber (ignored for non-PDF formats)",
    )
    textract_region: str = arg(
        default="eu-west-1",
        help="AWS region for Textract (only used when provider is 'textract' or 'auto' with a valid MFA session)",
    )
    no_ai_format: bool = arg(
        default=False,
        help="Skip AI post-processing — write raw Textract output without LLM cleanup (faster, no Bedrock cost)",
    )
    ai_model_id: str | None = arg(
        default=None,
        help="Bedrock model ID for AI formatting (default: from AgentSettings / QAGENTS_BEDROCK_MODEL_ID)",
    )
    extract_figures: bool = arg(
        default=False,
        help="Detect and save figure regions as PNG files in a figures/ sub-directory and insert image references in the Markdown output (Textract provider only)",
    )
    no_cache: bool = arg(
        default=False,
        help="Skip content-addressed cache for Textract and AI calls; also skips conversion history recording",
    )
    pages: str | None = arg(
        default=None,
        help="Pages to convert, e.g. '1-3,7,10-12' (1-based, comma-separated ranges). Default: all pages. Textract and pdfplumber providers both support this.",
    )

    @override
    async def run(self) -> None:
        ext = self.source.suffix.lower()

        if ext == ".pdf":
            import shutil

            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
                TimeElapsedColumn,
            )

            valid_providers = {"auto", "textract", "pdfplumber"}
            chosen = self.provider.lower()
            if chosen not in valid_providers:
                console.print(
                    f"[red]Unknown PDF provider '{self.provider}'. "
                    f"Choose: {', '.join(sorted(valid_providers))}[/red]"
                )
                sys.exit(1)

            # ── Output folder structure ────────────────────────────────────
            if self.target is not None:
                out = self.target
                _figures_dir: Path | None = None  # converter uses default
                _out_dir: Path | None = None
                _exports_base: Path | None = None
                _version = 0
            else:
                _exports_base = Path.cwd() / "exports"
                _bootstrap_exports_dir(_exports_base)
                _base = _exports_base / self.source.stem
                _out_dir, _version = _next_versioned_dir(_base)
                out = _out_dir / self.source.with_suffix(".md").name
                _figures_dir = _out_dir / "figures" if self.extract_figures else None

            # ── MFA preflight ──────────────────────────────────────────────
            if chosen in ("auto", "textract"):
                from qdocs.converters import check_textract_mfa_session

                ok, mfa_msg = check_textract_mfa_session()
                if ok:
                    console.print(f"[dim]  MFA session: {mfa_msg}[/dim]")
                elif chosen == "textract":
                    console.print(f"[red]✗[/red] {mfa_msg}")
                    sys.exit(1)
                else:
                    console.print(
                        f"[yellow]  No MFA session — using pdfplumber.[/yellow]\n"
                        f"[dim]  ({mfa_msg})[/dim]"
                    )

            # Suppress boto3/botocore credential-discovery INFO logs that
            # leak through Python's stdlib logging and break the progress bar.
            import logging

            logging.getLogger("botocore").setLevel(logging.WARNING)
            logging.getLogger("boto3").setLevel(logging.WARNING)

            # ── Convert with rich progress ─────────────────────────────────
            _state: dict[str, int] = {"pages": 0, "figures": 0}
            _cache = None
            _history_db = None
            if not self.no_cache:
                from qdocs.cache import ContentCache, QdocsHistoryDB

                _cache = ContentCache()
                _history_db = QdocsHistoryDB()
            _desc_w = 34  # fixed visible width for the description column
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=None),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=False,
            ) as progress:
                textract_task = progress.add_task(
                    f"[cyan]{'Textract':<{_desc_w}}[/cyan]", total=None
                )
                ai_task = progress.add_task(
                    f"[dim]{'AI format':<{_desc_w}}[/dim]", total=1, visible=False
                )

                def on_progress(stage: str, current: int, total: int) -> None:
                    _tw = len(str(total)) if total else 1
                    if stage == "done":
                        _state["pages"] = current
                        _state["figures"] = total
                    elif stage == "page":
                        lbl = f"Textract  pg {current:>{_tw}}/{total}"
                        progress.update(
                            textract_task,
                            total=total,
                            completed=current,
                            description=f"[cyan]{lbl:<{_desc_w}}[/cyan]",
                        )
                    elif stage == "ai_start":
                        lbl_t = f"Textract  {total or current} pg  \u2713"
                        progress.update(
                            textract_task,
                            completed=total or current,
                            description=f"[green]{lbl_t:<{_desc_w}}[/green]",
                        )
                        progress.update(
                            ai_task,
                            visible=True,
                            total=total or 1,
                            completed=0,
                            description=f"[yellow]{'AI format  pg   0/??':<{_desc_w}}[/yellow]",
                        )
                    elif stage == "ai_chunk":
                        lbl = f"AI format  pg {current:>{_tw}}/{total}"
                        progress.update(
                            ai_task,
                            completed=current,
                            total=total,
                            description=f"[yellow]{lbl:<{_desc_w}}[/yellow]",
                        )
                    elif stage == "ai_done":
                        lbl = f"AI format  {total or current} pg  \u2713"
                        progress.update(
                            ai_task,
                            completed=progress.tasks[ai_task].total or 1,
                            description=f"[green]{lbl:<{_desc_w}}[/green]",
                        )

                convert_pdf_to_md(
                    self.source,
                    out,
                    provider=chosen,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                    textract_region=self.textract_region,
                    ai_format=not self.no_ai_format,
                    ai_model_id=self.ai_model_id,
                    extract_figures=self.extract_figures,
                    figures_dir=_figures_dir,
                    on_progress=on_progress,
                    cache=_cache,
                    pages=self.pages,
                )

            # ── Copy source PDF to src/ ────────────────────────────────────
            if self.target is None and _out_dir is not None:
                src_dir = _out_dir / "src"
                src_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.source, src_dir / self.source.name)

            if _history_db is not None and self.target is None and _out_dir is not None:
                try:
                    _src_hash = _history_db.source_hash(self.source)
                    _history_db.record(
                        source=self.source,
                        source_hash=_src_hash,
                        out_dir=_out_dir,
                        version=_version,
                        provider=chosen,
                        ai_model=self.ai_model_id,
                        pages=_state.get("pages", 0),
                        figures=_state.get("figures", 0),
                        ai_formatted=not self.no_ai_format,
                    )
                except Exception as _hist_exc:
                    logger.debug("history record failed: {}", _hist_exc)

            ai_label = "raw" if self.no_ai_format else "ai-formatted"
            fig_label = "  +figures" if self.extract_figures else ""
            if self.target is None and _out_dir is not None:
                try:
                    out_rel = str(out.relative_to(Path.cwd()))
                except ValueError:
                    out_rel = str(out)
            else:
                out_rel = out.name
            _fig_count = _state.get("figures", 0)
            fig_count_label = f"  {_fig_count} figure(s)" if _fig_count else ""
            console.print(
                f"[green]✓[/green] [bold]{self.source.name}[/bold]  →  "
                f"[cyan]{out_rel}[/cyan]  "
                f"[dim]({chosen}, {ai_label}{fig_label}{fig_count_label})[/dim]"
            )
            return

        # ── Non-PDF formats ────────────────────────────────────────────────
        out = self.target or self.source.with_suffix(".md")
        dispatch = {
            ".docx": convert_docx_to_md,
            ".xlsx": convert_xlsx_to_md,
            ".pptx": convert_pptx_to_md,
        }
        fn = dispatch.get(ext)
        if fn is None:
            console.print(f"[red]Unsupported format: {ext}[/red]")
            sys.exit(1)
        fn(self.source, out)
        console.print(f"[green]✓[/green] {self.source.name} → {out.name}")


class Convert(Command):
    """Convert documents between formats."""

    subcommand: ToPdf | ToDocx | ToXlsx | ToMd = arg(help="Target format")

    @override
    async def run(self) -> None:
        pass


# ===========================================================================
# qdocs diagrams
# ===========================================================================


class DiagramsGenerate(Command):
    """Render all Mermaid blocks in markdown file(s)."""

    source: Path = arg(help="Source .md file or directory")
    pattern: str = arg(default="*.md", help="Glob pattern")
    recursive: bool = arg(default=True, help="Recurse into subdirectories")
    fmt: str = arg(default="svg", help="Output format: svg | png")
    width: int = arg(default=1920, help="Render width in pixels")
    background: str = arg(default="white", help="Background colour")
    force: bool = arg(default=False, help="Re-render even if cached")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        files = _collect_md_files(self.source, self.pattern, self.recursive)
        for f in files:
            diagrams_dir = f.parent / "__diagrams__"
            generated = generate_diagrams_cached(
                f,
                diagrams_dir,
                settings.cache_path,
                output_format=self.fmt,
                width=self.width,
                background=self.background,
                force=self.force,
            )
            if generated:
                console.print(f"[green]✓[/green] {f.name}: {len(generated)} diagram(s)")
            else:
                console.print(f"[dim]  {f.name}: no diagrams[/dim]")


class DiagramsCheck(Command):
    """Verify mmdc (mermaid-cli) is installed and reachable."""

    @override
    async def run(self) -> None:
        try:
            check_mmdc()
            console.print("[green]✓[/green] mmdc found in PATH")
        except MmcdNotFoundError as exc:
            console.print(f"[red]✗[/red] {exc}")
            sys.exit(1)


class DiagramsExport(Command):
    """Export raw .mmd source files from markdown."""

    source: Path = arg(help="Source .md file or directory")
    output: Path | None = arg(default=None, help="Output directory")
    pattern: str = arg(default="*.md", help="Glob pattern")
    recursive: bool = arg(default=True, help="Recurse")

    @override
    async def run(self) -> None:
        files = _collect_md_files(self.source, self.pattern, self.recursive)
        for f in files:
            out_dir = self.output or f.parent / "__diagrams__"
            exported = export_mermaid_blocks(f, out_dir)
            if exported:
                console.print(
                    f"[green]✓[/green] {f.name}: {len(exported)} .mmd file(s) → {out_dir}"
                )


class DiagramsList(Command):
    """List Mermaid diagram blocks with line numbers."""

    source: Path = arg(help="Source .md file")
    limit: int = arg(default=50, help="Maximum diagrams to list")

    @override
    async def run(self) -> None:
        import re

        content = self.source.read_text(encoding="utf-8")
        pattern = r"```mermaid\s*\n(.*?)\n```"
        matches = list(re.finditer(pattern, content, re.DOTALL))
        if not matches:
            console.print(
                f"[yellow]No Mermaid diagrams found in {self.source.name}[/yellow]"
            )
            return
        for idx, match in enumerate(matches[: self.limit], 1):
            line_num = content[: match.start()].count("\n") + 1
            first_lines = match.group(1).strip().split("\n")[:3]
            console.print(f"[cyan]Diagram {idx}[/cyan] at line {line_num}:")
            for line in first_lines:
                console.print(f"  {line[:80]}")
        if len(matches) > self.limit:
            console.print(
                f"[dim]… {len(matches) - self.limit} more diagrams not shown[/dim]"
            )


class Diagrams(Command):
    """Mermaid diagram management."""

    subcommand: DiagramsGenerate | DiagramsCheck | DiagramsExport | DiagramsList = arg(
        help="Diagrams command"
    )

    @override
    async def run(self) -> None:
        pass


# ===========================================================================
# qdocs export
# ===========================================================================


class Export(Command):
    """Full export pipeline: diagrams → PDF and/or DOCX."""

    source_dir: Path = arg(default=Path(), help="Source directory containing .md files")
    output_dir: Path | None = arg(
        default=None, help="Output directory (default: ~/.intriq/exports/qdocs)"
    )
    fmt: str = arg(default="pdf", help="Output format: pdf | docx | md | all")
    pattern: str = arg(default="*.md", help="File pattern")
    recursive: bool = arg(default=True, help="Recurse into subdirectories")
    profile: str = arg(default="default", help="Profile name")
    version: str | None = arg(default=None, help="Version string for cover page")
    author: str | None = arg(default=None, help="Author name for cover page")
    classification: str = arg(
        default="CONFIDENTIAL", help=f"Classification: {_classification_choices()}"
    )
    theme: str | None = arg(
        default=None,
        help="Pygments theme (github-dark, monokai, one-dark, friendly). Overrides profile.",
    )
    no_cover: bool = arg(default=False, help="Suppress cover page")
    no_version: bool = arg(default=False, help="Hide version from cover")
    no_date: bool = arg(default=False, help="Hide date from cover")
    no_revision: bool = arg(default=False, help="Hide revision from cover")
    force: bool = arg(default=False, help="Force re-export ignoring cache")
    landscape: bool = arg(default=False, help="Landscape orientation")
    regenerate_diagrams: bool = arg(default=False, help="Force re-render all diagrams")
    verbose: bool = arg(default=False, help="Show cached files too")
    book: bool = arg(default=False, help="Combine all files into a single book output")
    book_title: str | None = arg(
        default=None, help="Title for book cover page (default: folder name)"
    )
    output_name: str | None = arg(
        default=None,
        help="Output filename stem (without extension). In book mode overrides the default "
        "'<dir>-book' name; in single-file mode renames the exported file.",
    )
    no_page_numbers: bool = arg(
        default=False, help="Disable page numbers in footer (default: enabled)"
    )
    page_breaks: bool = arg(
        default=False, help="Insert page breaks between sections in DOCX (default: off)"
    )
    no_toc: bool = arg(
        default=False, help="Suppress Table of Contents page in book mode"
    )
    chapters: bool = arg(
        default=False, help="Export each subdirectory as a separate chapter book file"
    )
    chapter: str | None = arg(default=None, help="Chapter label shown on cover page")
    exclude: list[str] = arg(
        default=[],
        help="Glob patterns for files to exclude (repeatable: --exclude '00-*.md')",
    )

    @override
    async def run(self) -> None:
        from qdocs.models.document import DocumentFormat

        settings = _load_settings()
        profile_mgr = ProfileManager(settings.profile_path)
        classification = DocumentClassification(self.classification.upper())

        cover = profile_mgr.build_cover_config(
            self.profile,
            enabled=not self.no_cover,
            version_str=self.version,
            show_version=not self.no_version,
            show_date=not self.no_date,
            show_revision=not self.no_revision,
            classification=classification,
            author=self.author,
            chapter=self.chapter,
            code_theme=self.theme,
        )

        try:
            doc_fmt = DocumentFormat(self.fmt)
        except ValueError:
            console.print(f"[red]Unknown format: {self.fmt}[/red]")
            sys.exit(1)

        out_dir = (self.output_dir or settings.output_dir).expanduser()
        svc = ExportService(settings)

        if self.chapters:
            console.print(
                f"[cyan]Chapter export: {self.source_dir} → {out_dir} [{self.fmt}][/cyan]"
            )
            results = await svc.export_chapters(
                source_dir=self.source_dir,
                output_dir=out_dir,
                fmt=doc_fmt,
                cover=cover,
                pattern=self.pattern,
                page_numbers=not self.no_page_numbers,
                page_breaks=self.page_breaks,
                landscape=self.landscape,
                force=self.force,
                toc=not self.no_toc,
                exclude=self.exclude,
            )
            ok = sum(1 for r in results if r.success)
            failed = sum(1 for r in results if not r.success)
            for r in results:
                status = "[green]✓[/green]" if r.success else "[red]✗[/red]"
                console.print(f"  {status} {r.output.name}")
                if not r.success:
                    console.print(f"    [red]{r.error}[/red]")
            console.print(f"[green]Chapters: {ok} exported, {failed} failed[/green]")
            return

        if self.book:
            console.print(
                f"[cyan]Book export: {self.source_dir} → {out_dir} [{self.fmt}][/cyan]"
            )
            result = await svc.export_book(
                source_dir=self.source_dir,
                output_dir=out_dir,
                fmt=doc_fmt,
                cover=cover,
                pattern=self.pattern,
                recursive=self.recursive,
                book_title=self.book_title,
                output_name=self.output_name,
                page_numbers=not self.no_page_numbers,
                page_breaks=self.page_breaks,
                landscape=self.landscape,
                force=self.force,
                toc=not self.no_toc,
                exclude=self.exclude,
            )
            status = "[green]✓[/green]" if result.success else "[red]✗[/red]"
            console.print(f"  {status} {result.output.name}")
            if not result.success:
                console.print(f"  [red]{result.error}[/red]")
            return

        console.print(
            f"[cyan]Exporting from {self.source_dir} → {out_dir} [{self.fmt}][/cyan]"
        )

        results = await svc.export_directory(
            source_dir=self.source_dir,
            output_dir=out_dir,
            fmt=doc_fmt,
            cover=cover,
            pattern=self.pattern,
            recursive=self.recursive,
            force=self.force,
            landscape=self.landscape,
            page_numbers=not self.no_page_numbers,
            page_breaks=self.page_breaks,
            exclude=self.exclude,
        )

        ok = sum(1 for r in results if r.success and not r.cached)
        cached = sum(1 for r in results if r.cached)
        failed = sum(1 for r in results if not r.success)

        for r in results:
            if r.success and r.cached and not self.verbose:
                continue
            status = (
                "[dim](cached)[/dim]"
                if r.cached
                else "[green]✓[/green]"
                if r.success
                else "[red]✗[/red]"
            )
            rev_label = f" r{r.revision}" if r.revision else ""
            console.print(f"  {status} {r.source.name}{rev_label} → {r.output.name}")

        # --output-name rename: only applies when exactly one successful file was exported
        if self.output_name:
            successful = [r for r in results if r.success and not r.cached]
            all_successful = [r for r in results if r.success]
            target_results = successful or all_successful
            if len(target_results) == 1:
                r = target_results[0]
                new_path = r.output.parent / f"{self.output_name}{r.output.suffix}"
                if r.output != new_path:
                    r.output.rename(new_path)
                    console.print(f"  [dim]→ renamed to {new_path.name}[/dim]")
            elif len(target_results) > 1:
                console.print(
                    "[yellow]--output-name ignored: matched multiple files "
                    "(use --pattern to narrow to one)[/yellow]"
                )

        summary_parts = []
        if ok:
            summary_parts.append(f"[green]{ok} exported[/green]")
        if cached:
            summary_parts.append(f"[dim]{cached} cached[/dim]")
        if failed:
            summary_parts.append(f"[red]{failed} failed[/red]")
        console.print("  ".join(summary_parts))


# ===========================================================================
# qdocs revisions
# ===========================================================================


class RevisionsShow(Command):
    """Show revision history for a document."""

    source: Path = arg(help="Source .md file")
    limit: int = arg(default=20, help="Maximum revisions to display")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        mgr = RevisionManager(settings.cache_path)
        history = mgr.get_history(self.source)

        if not history.revisions:
            console.print(f"[dim]No revision history for {self.source.name}[/dim]")
            return

        tbl = Table(title=f"Revisions: {self.source.name}", show_lines=True)
        tbl.add_column("Rev", style="bold cyan", width=6)
        tbl.add_column("Date", width=18)
        tbl.add_column("Author", width=16)
        tbl.add_column("Hash", width=12)
        tbl.add_column("Note")

        for rev in history.revisions[: self.limit]:
            tbl.add_row(
                f"r{rev.revision}",
                rev.changed_at.strftime("%Y-%m-%d %H:%M"),
                rev.author or "—",
                rev.content_hash[:10],
                rev.note or "",
            )
        console.print(tbl)


class RevisionsBump(Command):
    """Manually create a new revision entry."""

    source: Path = arg(help="Source .md file")
    note: str | None = arg(default=None, help="Optional revision note")
    author: str | None = arg(default=None, help="Author name")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        mgr = RevisionManager(settings.cache_path)
        rev = mgr.bump(self.source, note=self.note, author=self.author)
        console.print(f"[green]✓[/green] {self.source.name} bumped to r{rev.revision}")


class RevisionsClear(Command):
    """Delete all revision history for a document."""

    source: Path = arg(help="Source .md file")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        mgr = RevisionManager(settings.cache_path)
        count = mgr.clear(self.source)
        console.print(
            f"[green]✓[/green] Cleared {count} revision(s) for {self.source.name}"
        )


class Revisions(Command):
    """Document revision history management."""

    subcommand: RevisionsShow | RevisionsBump | RevisionsClear = arg(
        help="Revisions command"
    )

    @override
    async def run(self) -> None:
        pass


# ===========================================================================
# qdocs profile
# ===========================================================================


class ProfileShow(Command):
    """Show active profile settings."""

    name: str = arg(default="default", help="Profile name")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        mgr = ProfileManager(settings.profile_path)
        try:
            profile = mgr.load(self.name)
        except ProfileError as exc:
            console.print(f"[red]✗[/red] {exc}")
            sys.exit(1)

        tbl = Table(title=f"Profile: {self.name}", show_header=False, box=None)
        tbl.add_column("Key", style="bold")
        tbl.add_column("Value")
        tbl.add_row("company_name", profile.company_name)
        tbl.add_row(
            "logo",
            str(profile.logo_path)
            if profile.logo_path
            else f"[red]{profile.logo} (missing)[/red]",
        )
        tbl.add_row("copyright", profile.copyright)
        tbl.add_row("address", profile.address)
        tbl.add_row("website", profile.website)
        console.print(tbl)


class ProfileInit(Command):
    """Initialise (or re-scaffold) a profile directory."""

    name: str = arg(default="default", help="Profile name")
    logo: Path | None = arg(default=None, help="Logo file to copy into profile")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        mgr = ProfileManager(settings.profile_path)
        profile_dir = mgr.init(self.name, logo_source=self.logo)
        console.print(f"[green]✓[/green] Profile initialised: {profile_dir}")


class ProfileList(Command):
    """List installed profiles."""

    @override
    async def run(self) -> None:
        settings = _load_settings()
        mgr = ProfileManager(settings.profile_path)
        names = mgr.list_profiles()
        if not names:
            console.print("[dim]No profiles found.[/dim] Run: qdocs profile init")
            return
        for name in names:
            console.print(f"  • {name}")


class ProfileSetLogo(Command):
    """Copy a logo file into a profile."""

    logo_path: Path = arg(help="Path to logo image (.png recommended)")
    name: str = arg(default="default", help="Profile name")

    @override
    async def run(self) -> None:
        settings = _load_settings()
        mgr = ProfileManager(settings.profile_path)
        dest = mgr.set_logo(self.logo_path, self.name)
        console.print(f"[green]✓[/green] Logo set: {dest}")


class Profile(Command):
    """Profile management (branding, logo, cover page defaults)."""

    subcommand: ProfileShow | ProfileInit | ProfileList | ProfileSetLogo = arg(
        help="Profile command"
    )

    @override
    async def run(self) -> None:
        pass


# ===========================================================================
# qdocs cache
# ===========================================================================


class CacheStatus(Command):
    """Show cache statistics."""

    @override
    async def run(self) -> None:
        from qdocs.cache import QDocsDB

        settings = _load_settings()
        try:
            db = QDocsDB(settings.cache_path)
            stats = db.cache_stats()
            db_path = db.path

            tbl = Table(title="Cache Status", show_header=False, box=None)
            tbl.add_column("Key", style="bold")
            tbl.add_column("Value", justify="right")
            tbl.add_row("Database", str(db_path))
            tbl.add_row(
                "Size",
                f"{db_path.stat().st_size / (1024 * 1024):.2f} MB"
                if db_path.exists()
                else "0.00 MB",
            )
            tbl.add_row("Documents (entries)", str(stats["documents_entries"]))
            tbl.add_row("Documents (sources)", str(stats["documents_sources"]))
            tbl.add_row("Diagrams (entries)", str(stats["diagrams_entries"]))
            tbl.add_row("Revisions (total)", str(stats["revisions_total"]))
            tbl.add_row("Revisions (sources)", str(stats["revisions_sources"]))
            console.print(tbl)
        except Exception as exc:
            console.print(f"[red]Cache error: {exc}[/red]")


class CacheClear(Command):
    """Clear cache entries."""

    docs: bool = arg(default=False, help="Clear document cache entries")
    diagrams: bool = arg(default=False, help="Clear diagram cache entries")
    revisions: bool = arg(default=False, help="Clear revision history")
    all: bool = arg(default=False, help="Clear everything")

    @override
    async def run(self) -> None:
        from qdocs.cache import QDocsDB

        settings = _load_settings()

        clear_docs = self.all or self.docs
        clear_diags = self.all or self.diagrams
        clear_revs = self.all or self.revisions

        if not any([clear_docs, clear_diags, clear_revs]):
            console.print(
                "[yellow]Specify --docs, --diagrams, --revisions, or --all[/yellow]"
            )
            return

        db = QDocsDB(settings.cache_path)
        if clear_docs:
            db.clear_documents()
            console.print("[green]✓[/green] Cleared document cache")
        if clear_diags:
            db.clear_diagrams()
            console.print("[green]✓[/green] Cleared diagram cache")
        if clear_revs:
            db.clear_revisions()
            console.print("[green]✓[/green] Cleared revision history")


class Cache(Command):
    """Cache inspection and management."""

    subcommand: CacheStatus | CacheClear = arg(help="Cache command")

    @override
    async def run(self) -> None:
        pass


# ===========================================================================
# qdocs isms-export
# ===========================================================================


def _isms_to_md(source: Path, target: Path) -> None:
    """Convert DOCX/XLSX/PDF/PPTX to Markdown for ISMS pipeline."""
    ext = source.suffix.lower()
    if ext == ".docx":
        convert_docx_to_md(source, target)
    elif ext == ".xlsx":
        convert_xlsx_to_md(source, target)
    elif ext == ".pdf":
        convert_pdf_to_md(source, target)
    elif ext == ".pptx":
        convert_pptx_to_md(source, target)
    else:
        msg = f"Unsupported format for ISMS conversion: {ext}"
        raise ConversionError(msg)


class IsmsExport(Command):
    """Export ISMS documentation to PDF and/or DOCX with versioned revision tracking.

    Reads from ~/.intriq/source/<version>/ by default. Converts TOML risk files
    in-pipeline, bumps per-folder REVISION counter, and prunes stale revision files.
    """

    version: str = arg(
        default="v1", help="ISMS version directory under source_dir (default: v1)"
    )
    output: Path | None = arg(
        default=None, help="Output root (default: ~/.intriq/exports/<version>/)"
    )
    pattern: str = arg(default="*.md", help="Markdown file glob (default: *.md)")
    recursive: bool = arg(default=True, help="Recurse into sub-directories")
    fmt: str | None = arg(
        default=None, help="Export format: pdf | docx | both (default: pdf from config)"
    )
    rm_old_revs: bool = arg(
        default=True, help="Prune stale revision files (default: True)"
    )
    landscape: bool = arg(default=False, help="Landscape PDF orientation")
    no_coverpage_revision: bool = arg(
        default=False, help="Hide revision number on cover page"
    )
    regenerate_diagrams: bool = arg(
        default=False, help="Force re-render Mermaid diagrams"
    )
    verbose: bool = arg(default=False, help="Show per-file progress")

    @override
    async def run(self) -> None:
        # Lazy imports — ISMS features require qcli to be installed
        try:
            from qcli.compliance.isms.adapters import (
                isms_convert_md_to_pdf as _isms_md_to_pdf,
            )
            from qcli.compliance.isms.exceptions import (
                ConversionError as IsmsConversionError,
            )
            from qcli.compliance.isms.settings.qisms import QismsSettings
            from qcli.sprinto.converters.toml_to_md import convert_toml_to_md
        except ImportError as exc:
            console.print(
                f"[red]ISMS export requires qcli to be installed: {exc}[/red]"
            )
            sys.exit(1)

        logger.remove()
        logger.add(
            sys.stderr,
            format="<level>{message}</level>",
            level="WARNING",
            colorize=True,
        )

        settings = QismsSettings.load()
        source = settings.source_dir / self.version

        if not source.exists():
            msg = f"Version directory not found: {source}"
            console.print(f"[red]✗ {msg}[/red]")
            raise SourceNotFoundError(msg)

        # Resolve formats
        if self.fmt is None:
            formats = [getattr(settings, "format", "pdf")]
        elif self.fmt == "both":
            formats = ["pdf", "docx"]
        elif self.fmt in ("pdf", "docx"):
            formats = [self.fmt]
        else:
            console.print(
                f"[red]✗ Invalid format: {self.fmt}. Use pdf, docx, or both[/red]"
            )
            raise ConversionError(f"Invalid format: {self.fmt}")

        # ── TOML → MD pre-processing (Risk Register) ────────────────────────
        temp_dir: Path | None = None
        toml_files = list(
            source.rglob("*.toml") if self.recursive else source.glob("*.toml")
        )
        if self.pattern != "*.md" and toml_files:
            pattern_base = self.pattern.replace("*.md", "*").replace(".md", "")
            toml_files = [
                f
                for f in toml_files
                if pattern_base == "*"
                or pattern_base in f.stem
                or f.name.startswith(pattern_base.replace("*", ""))
            ]

        if toml_files:
            temp_dir = Path(tempfile.mkdtemp(prefix="qisms_toml_"))
            if self.verbose:
                console.print(
                    f"[cyan]Converting {len(toml_files)} TOML file(s) to Markdown...[/cyan]"
                )
            for tf in toml_files:
                rel_path = tf.relative_to(source)
                md_out = temp_dir / rel_path.with_suffix(".md")
                md_out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    convert_toml_to_md(tf, md_out)
                except (ConversionError, IsmsConversionError) as e:
                    if self.verbose:
                        console.print(f"  ✗ {tf.name}: {e}", style="yellow")

        # ── Collect markdown files ───────────────────────────────────────────
        md_files: list[Path] = []
        glob_fn = (
            (lambda d, p: list(d.rglob(p)))
            if self.recursive
            else (lambda d, p: list(d.glob(p)))
        )
        md_files.extend(glob_fn(source, self.pattern))
        if temp_dir:
            md_files.extend(glob_fn(temp_dir, self.pattern))
        md_files = [f for f in md_files if f.suffix.lower() in (".md", ".markdown")]

        # Existing PDFs / DOCX to copy/convert (with pattern filter)
        all_pdfs = list(
            source.rglob("*.pdf") if self.recursive else source.glob("*.pdf")
        )
        all_docx = list(
            source.rglob("*.docx") if self.recursive else source.glob("*.docx")
        )
        pattern_stem = self.pattern.replace("*.md", "*").replace(".md", "")
        existing_pdfs = [f for f in all_pdfs if fnmatch(f.stem, pattern_stem)]
        existing_docx = [f for f in all_docx if fnmatch(f.stem, pattern_stem)]

        if not md_files and not existing_pdfs and not existing_docx:
            console.print(f"[yellow]No files found to export in {source}[/yellow]")
            if temp_dir:
                shutil.rmtree(temp_dir)
            return

        # ── Diagram generation (step 1) ──────────────────────────────────────
        if self.regenerate_diagrams:
            console.print("[cyan]Regenerating diagrams from Mermaid blocks...[/cyan]")
        else:
            console.print(
                "[cyan]Checking diagrams (using cache when available)...[/cyan]"
            )

        n_gen = n_cached = 0
        for md_file in sorted(md_files):
            if not md_file.exists() or md_file.name == "README.md":
                continue
            if temp_dir and temp_dir in md_file.parents:
                continue  # skip temp TOML-converted files
            diagrams_dir = md_file.parent / "__diagrams__"
            try:
                diagrams = generate_diagrams(
                    source=md_file,
                    output_dir=diagrams_dir,
                    output_format="svg",
                    width=1920,
                    background="white",
                    skip_if_cached=not self.regenerate_diagrams,
                )
                if diagrams:
                    if self.regenerate_diagrams:
                        n_gen += len(diagrams)
                    else:
                        n_cached += len(diagrams)
                    if self.verbose:
                        label = "generated" if self.regenerate_diagrams else "cached"
                        console.print(
                            f"  ✓ {md_file.name}: {len(diagrams)} diagram(s) ({label})"
                        )
            except (ConversionError, MmcdNotFoundError) as e:
                if self.verbose:
                    console.print(f"  ⚠ {md_file.name}: {e}", style="yellow")

        parts = []
        if n_gen:
            parts.append(f"{n_gen} generated")
        if n_cached:
            parts.append(f"{n_cached} cached")
        console.print(
            f"[green]✓ Diagrams: {', '.join(parts)}[/green]\n"
            if parts
            else "[dim]No diagrams found[/dim]\n"
        )

        # ── PDF export (step 2) ──────────────────────────────────────────────
        if self.output is None:
            pdf_out = settings.output_dir / self.version / "pdf"
        else:
            pdf_out = self.output / "pdf"
        pdf_out.mkdir(parents=True, exist_ok=True)

        revision_file = pdf_out / "REVISION"
        try:
            next_rev = (
                int(revision_file.read_text().strip()) + 1
                if revision_file.exists()
                else 1
            )
        except (ValueError, OSError):
            next_rev = 1
        revision_file.write_text(str(next_rev))

        version_num = self.version.lstrip("v")
        pdf_success = 0
        generated_pdfs: list[tuple[Path, Path]] = []

        for md_file in sorted(md_files):
            rel = md_file.relative_to(
                temp_dir if (temp_dir and temp_dir in md_file.parents) else source
            )
            # Copy __diagrams__ alongside output
            diagrams_dir = md_file.parent / "__diagrams__"
            if diagrams_dir.exists():
                tgt_diag = pdf_out / rel.parent / "__diagrams__"
                tgt_diag.mkdir(parents=True, exist_ok=True)
                for df in [*diagrams_dir.glob("*.svg"), *diagrams_dir.glob("*.png")]:
                    shutil.copy2(df, tgt_diag / df.name)

            pdf_name = rel.stem + f"-{self.version}-{next_rev}.pdf"
            out_file = pdf_out / rel.parent / pdf_name
            out_file.parent.mkdir(parents=True, exist_ok=True)
            base_path = source if (temp_dir and temp_dir in md_file.parents) else None
            try:
                _isms_md_to_pdf(
                    md_file,
                    out_file,
                    cover_page=True,
                    version=version_num,
                    revision=next_rev if not self.no_coverpage_revision else None,
                    skip_header=True,
                    base_path=base_path,
                    landscape_mode=self.landscape,
                )
                pdf_success += 1
                generated_pdfs.append((rel, out_file))
            except (ConversionError, IsmsConversionError) as e:
                console.print(f"  ✗ {rel}: {e}", style="red")
                if temp_dir:
                    shutil.rmtree(temp_dir)
                raise

        for pdf_file in sorted(existing_pdfs):
            rel = pdf_file.relative_to(source)
            out_file = (
                pdf_out / rel.parent / (rel.stem + f"-{self.version}-{next_rev}.pdf")
            )
            out_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(pdf_file, out_file)
                pdf_success += 1
                generated_pdfs.append((rel, out_file))
            except Exception as e:
                if temp_dir:
                    shutil.rmtree(temp_dir)
                raise ConversionError(f"Failed to copy PDF: {e}") from e

        for docx_file in sorted(existing_docx):
            rel = docx_file.relative_to(source)
            out_file = (
                pdf_out / rel.parent / (rel.stem + f"-{self.version}-{next_rev}.pdf")
            )
            out_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                convert_docx_to_pdf(docx_file, out_file)
                pdf_success += 1
                generated_pdfs.append((rel.with_suffix(".pdf"), out_file))
            except (ConversionError, IsmsConversionError):
                if temp_dir:
                    shutil.rmtree(temp_dir)
                raise

        # Prune stale PDF revisions
        if self.rm_old_revs:
            for _rel, gen_file in generated_pdfs:
                base = _rel.stem
                for old in gen_file.parent.glob(f"{base}-*.pdf"):
                    if old != gen_file and old.stem.rsplit("-", 2)[0] == base:
                        try:
                            old.unlink()
                            if self.verbose:
                                console.print(
                                    f"  ✗ Removed old revision: {old.name}", style="dim"
                                )
                        except OSError:
                            pass

        total = len(md_files) + len(existing_pdfs) + len(existing_docx)
        console.print(f"[green]✓ Exported {pdf_success}/{total} files to PDF[/green]")
        console.print(f"[cyan]Version-Revision: {self.version}-{next_rev}[/cyan]")
        console.print(f"[dim]Output: {pdf_out}[/dim]")

        # ── DOCX export (step 3) ─────────────────────────────────────────────
        if "docx" not in formats:
            if temp_dir:
                shutil.rmtree(temp_dir)
            return

        if self.output is None:
            docx_out = settings.output_dir / self.version / "docx"
        else:
            docx_out = self.output / "docx"
        docx_out.mkdir(parents=True, exist_ok=True)

        console.print()
        console.print("[cyan]Converting PDFs to DOCX...[/cyan]")
        docx_success = 0

        for _rel, pdf_file in generated_pdfs:
            pdf_rel = pdf_file.relative_to(pdf_out)
            docx_file = docx_out / pdf_rel.parent / (pdf_rel.stem + ".docx")
            docx_file.parent.mkdir(parents=True, exist_ok=True)

            # Copy diagrams alongside DOCX
            pdf_diag = pdf_file.parent / "__diagrams__"
            if pdf_diag.exists():
                docx_diag = docx_file.parent / "__diagrams__"
                docx_diag.mkdir(parents=True, exist_ok=True)
                for df in [*pdf_diag.glob("*.png"), *pdf_diag.glob("*.svg")]:
                    shutil.copy2(df, docx_diag / df.name)

            try:
                convert_pdf_to_docx(pdf_file, docx_file, landscape_mode=self.landscape)
                docx_success += 1
            except (ConversionError, IsmsConversionError) as e:
                console.print(f"  ✗ {pdf_rel}: {e}", style="red")
                if temp_dir:
                    shutil.rmtree(temp_dir)
                raise

        # Prune stale DOCX revisions
        if self.rm_old_revs:
            for _rel, pdf_file in generated_pdfs:
                pdf_rel = pdf_file.relative_to(pdf_out)
                base = pdf_rel.stem.rsplit("-", 2)[0]
                parent = docx_out / pdf_rel.parent
                if not parent.exists():
                    continue
                current_stem = pdf_rel.stem
                for old in parent.glob(f"{base}-*.docx"):
                    if old.stem != current_stem and old.stem.rsplit("-", 2)[0] == base:
                        try:
                            old.unlink()
                            if self.verbose:
                                console.print(
                                    f"  ✗ Removed old revision: {old.name}", style="dim"
                                )
                        except OSError:
                            pass

        console.print(
            f"[green]✓ Converted {docx_success}/{len(generated_pdfs)} PDFs to DOCX[/green]"
        )
        console.print(f"[dim]Output: {docx_out}[/dim]")

        if temp_dir:
            shutil.rmtree(temp_dir)
            if self.verbose:
                console.print("[dim]Cleaned up temporary files[/dim]")


# ===========================================================================
# qdocs preview
# ===========================================================================


class Preview(Command):
    """Render paginated PNG previews of any document (PDF, DOCX, XLSX, PPTX, images).

    Office formats (DOCX/XLSX/PPTX/ODS/ODT etc.) are converted to PDF via
    LibreOffice headless, then rasterised page-by-page.  PDFs and images are
    rendered directly.  All pages are cached by content-hash so repeat runs
    are instant.

    Examples
    --------
      qdocs preview report.pdf
      qdocs preview presentation.pptx --quality fhd --pages 1-5
      qdocs preview data.xlsx --out /tmp/preview --no-cache
      qdocs preview --status
    """

    source: list[Path] = arg(
        default_factory=list,
        short="-s",
        help="File(s) to preview (repeatable)",
    )
    quality: str = arg(
        default="hd",
        help="DPI preset: sd (96) | hd (150, default) | fhd (220) | uhd (300)",
    )
    pages: str | None = arg(
        default=None,
        help="Page range: '1-5' | '2,4,6' | 'all' (default: all)",
    )
    out: Path | None = arg(
        default=None,
        help="Output directory (default: ~/.intriq/exports/previews/<stem>/)",
    )
    no_cache: bool = arg(
        default=False,
        help="Force re-render — bypass the preview cache",
    )
    status: bool = arg(
        default=False,
        help="Show preview cache statistics",
    )

    @override
    async def run(self) -> None:
        import asyncio

        from rich.table import Table as _Table

        try:
            from qcli.demo._preview import (
                PreviewQuality,
                _PreviewCache,
                can_preview,
                render_preview,
            )
        except ImportError as exc:
            console.print(
                f"[red]Preview requires qcli to be installed: {exc}[/red]"
            )
            sys.exit(1)

        if self.status:
            cache = _PreviewCache()
            stats = cache.stats()
            if stats["available"]:
                console.print(
                    f"Preview cache: {stats['pages']} pages  {stats['size_mb']:.1f}MB"
                )
            else:
                console.print("[yellow]Preview cache unavailable[/yellow]")
            return

        if not self.source:
            console.print(
                "[yellow]No files specified. Use --source/-s to provide file(s).[/yellow]"
            )
            return

        quality_map = {
            "sd": PreviewQuality.SD,
            "hd": PreviewQuality.HD,
            "fhd": PreviewQuality.FHD,
            "uhd": PreviewQuality.UHD,
        }
        quality = quality_map.get(self.quality.lower(), PreviewQuality.HD)

        page_range: tuple[int, int] | None = None
        if self.pages and self.pages.lower() not in ("all", ""):
            try:
                if "-" in self.pages:
                    a, b = self.pages.split("-", 1)
                    page_range = (int(a) - 1, int(b))
                elif "," in self.pages:
                    nums = [int(x.strip()) for x in self.pages.split(",")]
                    page_range = (min(nums) - 1, max(nums))
            except ValueError:
                console.print(
                    f"[yellow]Invalid page range {self.pages!r} — rendering all[/yellow]"
                )

        loop = asyncio.get_event_loop()
        for src in self.source:
            if not src.exists():
                console.print(f"[red]✗ not found: {src}[/red]")
                continue
            ok, reason = can_preview(src)
            if not ok:
                console.print(f"[yellow]⚠ {src.name}: {reason}[/yellow]")
                continue

            out_dir = self.out or (
                Path.home() / ".intriq" / "exports" / "previews" / src.stem
            )
            console.print(f"[dim]→ rendering {src.name} @ {int(quality)}dpi…[/dim]")

            _out_dir = out_dir  # bind loop variable for lambda capture
            pages = await loop.run_in_executor(
                None,
                lambda p=src, d=_out_dir: render_preview(
                    p,
                    quality=quality,
                    page_range=page_range,
                    no_cache=self.no_cache,
                    out_dir=d,
                ),
            )
            if not pages:
                console.print(f"[yellow]⚠ {src.name}: no pages rendered[/yellow]")
                continue

            cache_hits = sum(1 for p in pages if p.cache_hit)
            total_kb = sum(p.size_kb for p in pages)
            cache_note = f" ({cache_hits} cached)" if cache_hits else ""

            t = _Table(
                show_header=True,
                header_style="dim",
                show_lines=False,
                box=None,
                padding=(0, 1),
            )
            t.add_column("#", justify="right", style="dim", min_width=3)
            t.add_column("File", style="dim", max_width=50)
            t.add_column("Size", justify="right")
            t.add_column("W×H", style="dim")
            t.add_column("Cache", justify="center")
            for pg in pages:
                fname = out_dir / f"{src.stem}_p{pg.page_no:03d}_dpi{int(quality)}.png"
                t.add_row(
                    str(pg.page_no),
                    fname.name,
                    f"{pg.size_kb:.0f}KB",
                    f"{pg.width_px}×{pg.height_px}",
                    "[green]✓[/green]" if pg.cache_hit else "",
                )
            console.print(t)
            console.print(
                f"[green]✓[/green]  {src.name}  {len(pages)}pp @ {int(quality)}dpi  "
                f"{total_kb:.0f}KB{cache_note}  → [dim]{out_dir}/[/dim]"
            )


# ===========================================================================
# Root command
# ===========================================================================


class Qdocs(Command):
    """qdocs — document conversion, export, and revision management."""

    subcommand: (
        Convert | Diagrams | Export | IsmsExport | Preview | Revisions | Profile | Cache
    ) = arg(help="Command")

    @override
    async def run(self) -> None:
        pass


# ===========================================================================
# Utilities
# ===========================================================================


def _gen_diagrams(
    source: Path, settings: QdocsSettings, background: str | None = None
) -> None:
    """Generate Mermaid diagrams for source, swallowing mmdc-not-found gracefully."""
    resolved_bg = background if background is not None else settings.diagram_background
    force = background is not None and background != settings.diagram_background
    try:
        generate_diagrams_cached(
            source,
            source.parent / "__diagrams__",
            settings.cache_path,
            output_format=settings.diagram_format,
            width=settings.diagram_width,
            background=resolved_bg,
            force=force,
        )
    except MmcdNotFoundError as exc:
        console.print(f"[yellow]Warning:[/yellow] {exc}")
    except ConversionError as exc:
        logger.warning(f"Diagram generation skipped for {source.name}: {exc}")


def _collect_md_files(
    source: Path, pattern: str, recursive: bool, exclude: list[str] | None = None
) -> list[Path]:
    from fnmatch import fnmatch

    if source.is_file():
        return [source]
    files = sorted(source.rglob(pattern) if recursive else source.glob(pattern))
    if exclude:
        files = [f for f in files if not any(fnmatch(f.name, ex) for ex in exclude)]
    return files


# ===========================================================================
# Entry points
# ===========================================================================


def _normalize_convert_positional_args() -> None:
    """Allow `qdocs convert to-pdf /path/file.md` (bare path without --source).

    When ``convert to-pdf`` or ``convert to-docx`` is invoked with a bare path
    argument (not preceded by ``--source``), inject ``--source`` before it so
    clypi can parse it correctly.
    """
    if len(sys.argv) < 4:
        return
    if sys.argv[1] != "convert" or sys.argv[2] not in ("to-pdf", "to-docx", "to-md"):
        return
    # If --source is already present, nothing to do
    if "--source" in sys.argv:
        return
    # Find the first non-flag token after the subcommand name (sys.argv[3:])
    for i, token in enumerate(sys.argv[3:], start=3):
        if not token.startswith("-"):
            sys.argv.insert(i, "--source")
            return


def _configure_logging() -> None:
    """Minimal logging setup — loguru to stderr, warnings only."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<level>{level:<8}</level> | {message}",
        level="WARNING",
        colorize=True,
    )


def main() -> int:
    """qdocs CLI entry point."""
    _configure_logging()
    _normalize_convert_positional_args()
    try:
        Qdocs.parse().start()
        return 0
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
    except ProfileError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
    except SourceNotFoundError as exc:
        console.print(f"[red]Not found:[/red] {exc}")
    except MmcdNotFoundError as exc:
        console.print(f"[red]mmdc missing:[/red] {exc}")
    except ConversionError as exc:
        console.print(f"[red]Conversion failed:[/red] {exc}")
    except RevisionError as exc:
        console.print(f"[red]Revision error:[/red] {exc}")
    except CacheError as exc:
        console.print(f"[red]Cache error:[/red] {exc}")
    except QdocsError as exc:
        console.print(f"[red]Error:[/red] {exc}")
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    return 1
