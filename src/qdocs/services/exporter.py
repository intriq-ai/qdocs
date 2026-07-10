"""Export service — orchestrates the full document export pipeline.

Pipeline per file:
1. Compute content hash (SHA256)
2. Check document cache — skip if hash unchanged (unless force=True)
3. Record / get revision via RevisionManager
4. Generate / cache Mermaid diagrams (per-block hash)
5. Convert to target format(s)
6. Upsert document cache entry
"""

import asyncio
from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import Path

from loguru import logger

from qdocs.cache import QDocsDB
from qdocs.config import QdocsSettings
from qdocs.converters.md_to_docx import convert_md_to_docx
from qdocs.converters.md_to_pdf import _clean_title, convert_md_to_pdf
from qdocs.converters.mermaid import generate_diagrams_cached
from qdocs.converters.utils import get_file_hash
from qdocs.models.document import ConversionResult, DocumentFormat
from qdocs.models.profile import CoverPageConfig
from qdocs.services.profile_manager import ProfileManager
from qdocs.services.revision_manager import RevisionManager


class ExportService:
    """Orchestrates conversion, caching, and revision tracking."""

    def __init__(self, settings: QdocsSettings) -> None:
        self.settings = settings
        self.profile_mgr = ProfileManager(settings.profile_path)
        self.revision_mgr = RevisionManager(settings.cache_path)

    # ------------------------------------------------------------------
    # Single file
    # ------------------------------------------------------------------

    async def export_file(
        self,
        source: Path,
        output_dir: Path | None = None,
        fmt: DocumentFormat = DocumentFormat.PDF,
        cover: CoverPageConfig | None = None,
        force: bool = False,
        landscape: bool = False,
        page_numbers: bool = True,
        page_breaks: bool = False,
    ) -> list[ConversionResult]:
        """Export a single markdown file to one or more formats.

        Returns a list of ConversionResult (one per output format).
        """
        source = source.resolve()
        if not source.exists():
            from qdocs.exceptions import SourceNotFoundError

            msg = f"Source not found: {source}"
            raise SourceNotFoundError(msg)

        out_dir = (output_dir or self.settings.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        content_hash = get_file_hash(source)
        mtime = source.stat().st_mtime

        # Record revision (idempotent if unchanged)
        revision = self.revision_mgr.record(source, content_hash).revision

        # Inject revision into cover config
        if cover is None:
            cover = self.profile_mgr.build_cover_config(self.settings.profile_name)

        # Generate / cache Mermaid diagrams
        diagrams_dir = source.parent / "__diagrams__"
        await asyncio.to_thread(
            generate_diagrams_cached,
            source,
            diagrams_dir,
            self.settings.cache_path,
            output_format=self.settings.diagram_format,
            width=self.settings.diagram_width,
            background=self.settings.diagram_background,
            force=force,
        )

        formats = (
            [DocumentFormat.PDF, DocumentFormat.DOCX, DocumentFormat.MD]
            if fmt == DocumentFormat.ALL
            else [fmt]
        )

        results: list[ConversionResult] = []
        for target_fmt in formats:
            result = await self._convert_one(
                source=source,
                out_dir=out_dir,
                fmt=target_fmt,
                cover=cover,
                revision=revision,
                content_hash=content_hash,
                mtime=mtime,
                force=force,
                landscape=landscape,
                page_numbers=page_numbers,
                page_breaks=page_breaks,
            )
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Batch directory
    # ------------------------------------------------------------------

    async def export_directory(
        self,
        source_dir: Path,
        output_dir: Path | None = None,
        fmt: DocumentFormat = DocumentFormat.PDF,
        cover: CoverPageConfig | None = None,
        pattern: str = "*.md",
        recursive: bool = True,
        force: bool = False,
        landscape: bool = False,
        page_numbers: bool = True,
        page_breaks: bool = False,
        exclude: Sequence[str] = (),
    ) -> list[ConversionResult]:
        """Export all matching markdown files in source_dir."""
        source_dir = source_dir.resolve()
        out_dir = (output_dir or self.settings.output_dir).expanduser()

        if recursive:
            md_files = sorted(source_dir.rglob(pattern))
        else:
            md_files = sorted(source_dir.glob(pattern))

        if exclude:
            md_files = [
                f for f in md_files if not any(fnmatch(f.name, ex) for ex in exclude)
            ]

        all_results: list[ConversionResult] = []
        for md_file in md_files:
            # Preserve relative subfolder structure
            relative = md_file.relative_to(source_dir)
            file_out_dir = out_dir / relative.parent
            try:
                file_results = await self.export_file(
                    source=md_file,
                    output_dir=file_out_dir,
                    fmt=fmt,
                    cover=cover,
                    force=force,
                    landscape=landscape,
                    page_numbers=page_numbers,
                    page_breaks=page_breaks,
                )
                all_results.extend(file_results)
            except Exception as exc:
                logger.error(f"Failed to export {md_file.name}: {exc}")
                all_results.append(
                    ConversionResult(
                        source=md_file,
                        output=file_out_dir / md_file.stem,
                        fmt=fmt,
                        success=False,
                        error=str(exc),
                    )
                )

        return all_results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _convert_one(
        self,
        source: Path,
        out_dir: Path,
        fmt: DocumentFormat,
        cover: CoverPageConfig,
        revision: int,
        content_hash: str,
        mtime: float,
        force: bool,
        landscape: bool,
        page_numbers: bool = True,
        page_breaks: bool = False,
    ) -> ConversionResult:
        out_dir.mkdir(parents=True, exist_ok=True)

        # Output filename: {stem}-r{revision}.{ext}
        stem = source.stem
        out_file = out_dir / f"{stem}-r{revision}.{fmt.value}"

        source_key = str(source)

        # Check cache
        if not force:
            _db = QDocsDB(self.settings.cache_path)
            cached = _db.is_document_cached(source_key, fmt.value, content_hash)
            if cached and out_file.exists():
                logger.debug(f"Cache hit: {out_file.name}")
                return ConversionResult(
                    source=source,
                    output=out_file,
                    fmt=fmt,
                    success=True,
                    cached=True,
                    revision=revision,
                )

        try:
            if fmt == DocumentFormat.PDF:
                await asyncio.to_thread(
                    convert_md_to_pdf,
                    source,
                    out_file,
                    cover=cover,
                    revision=revision,
                    landscape_mode=landscape,
                    page_numbers=page_numbers,
                )
            elif fmt == DocumentFormat.DOCX:
                await asyncio.to_thread(
                    convert_md_to_docx,
                    source,
                    out_file,
                    cover=cover,
                    revision=revision,
                    page_numbers=page_numbers,
                    page_breaks=page_breaks,
                )
            elif fmt == DocumentFormat.MD:
                # MD "export" is a straight copy normalised to output location
                import shutil

                await asyncio.to_thread(shutil.copy2, source, out_file)
            else:
                msg = f"Unsupported format: {fmt}"
                raise ValueError(msg)

            # Upsert cache
            QDocsDB(self.settings.cache_path).upsert_document(
                source_key, content_hash, mtime, fmt.value, str(out_file)
            )

            return ConversionResult(
                source=source,
                output=out_file,
                fmt=fmt,
                success=True,
                cached=False,
                revision=revision,
            )

        except Exception as exc:
            logger.error(f"Conversion failed [{fmt.value}]: {source.name}: {exc}")
            return ConversionResult(
                source=source,
                output=out_file,
                fmt=fmt,
                success=False,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Book mode
    # ------------------------------------------------------------------

    async def export_book(
        self,
        source_dir: Path,
        output_dir: Path | None = None,
        fmt: DocumentFormat = DocumentFormat.PDF,
        cover: CoverPageConfig | None = None,
        pattern: str = "*.md",
        recursive: bool = True,
        book_title: str | None = None,
        output_name: str | None = None,
        page_numbers: bool = True,
        page_breaks: bool = False,
        landscape: bool = False,
        force: bool = False,
        toc: bool = True,
        exclude: Sequence[str] = (),
    ) -> ConversionResult:
        """Export all matching markdown files as a single combined book output."""
        from qdocs.converters.md_to_docx import convert_book_to_docx
        from qdocs.converters.md_to_pdf import convert_book_to_pdf

        source_dir = source_dir.resolve()
        out_dir = (output_dir or self.settings.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        md_files = sorted(
            source_dir.rglob(pattern) if recursive else source_dir.glob(pattern)
        )

        if exclude:
            md_files = [
                f for f in md_files if not any(fnmatch(f.name, ex) for ex in exclude)
            ]

        if not md_files:
            return ConversionResult(
                source=source_dir,
                output=out_dir,
                fmt=fmt,
                success=False,
                error="No matching files found",
            )

        title = book_title or source_dir.name
        _default_stem = f"{source_dir.name}-book"
        out_file = out_dir / f"{output_name or _default_stem}.{fmt.value}"

        if cover is None:
            cover = self.profile_mgr.build_cover_config(self.settings.profile_name)

        # Pre-generate Mermaid diagrams for every source file before combining.
        # Single-file export does this in export_file(); book mode must do it here
        # or _replace_mermaid_with_images() finds no __diagrams__/ dir and leaves
        # raw mermaid code blocks in the output.
        for md_file in md_files:
            diagrams_dir = md_file.parent / "__diagrams__"
            try:
                await asyncio.to_thread(
                    generate_diagrams_cached,
                    md_file,
                    diagrams_dir,
                    self.settings.cache_path,
                    output_format=self.settings.diagram_format,
                    width=self.settings.diagram_width,
                    background=self.settings.diagram_background,
                    force=force,
                )
            except Exception as exc:
                logger.warning(f"Diagram generation skipped for {md_file.name}: {exc}")

        try:
            if fmt == DocumentFormat.PDF:
                await asyncio.to_thread(
                    convert_book_to_pdf,
                    md_files,
                    out_file,
                    cover=cover,
                    book_title=title,
                    page_numbers=page_numbers,
                    landscape_mode=landscape,
                    toc=toc,
                )
            elif fmt == DocumentFormat.DOCX:
                await asyncio.to_thread(
                    convert_book_to_docx,
                    md_files,
                    out_file,
                    cover=cover,
                    book_title=title,
                    page_numbers=page_numbers,
                    page_breaks=page_breaks,
                    toc=toc,
                )
            else:
                msg = f"Book mode does not support format: {fmt}"
                raise ValueError(msg)

            logger.info(f"Book: {len(md_files)} files → {out_file.name}")
            return ConversionResult(
                source=source_dir,
                output=out_file,
                fmt=fmt,
                success=True,
            )

        except Exception as exc:
            logger.error(f"Book export failed: {exc}")
            return ConversionResult(
                source=source_dir,
                output=out_file,
                fmt=fmt,
                success=False,
                error=str(exc),
            )

    async def export_chapters(
        self,
        source_dir: Path,
        output_dir: Path | None = None,
        fmt: DocumentFormat = DocumentFormat.PDF,
        cover: CoverPageConfig | None = None,
        pattern: str = "*.md",
        page_numbers: bool = True,
        page_breaks: bool = False,
        landscape: bool = False,
        force: bool = False,
        toc: bool = True,
        exclude: Sequence[str] = (),
    ) -> list[ConversionResult]:
        """Export each subdirectory of source_dir as its own separate book file.

        Each immediate subfolder becomes one output document (chapter book).
        Files directly in source_dir (no subfolder) are bundled as a root book.
        """
        source_dir = source_dir.resolve()
        out_dir = (output_dir or self.settings.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        if cover is None:
            cover = self.profile_mgr.build_cover_config(self.settings.profile_name)

        # Collect chapter directories (immediate subdirs with matching md files)
        chapter_dirs = sorted(
            d
            for d in source_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".") and list(d.glob(pattern))
        )

        # Also collect root-level md files (if any)
        root_files = sorted(source_dir.glob(pattern))
        if exclude:
            root_files = [
                f for f in root_files if not any(fnmatch(f.name, ex) for ex in exclude)
            ]

        def _chapter_files(ch_dir: Path) -> list[Path]:
            files = sorted(ch_dir.rglob(pattern))
            if exclude:
                files = [
                    f for f in files if not any(fnmatch(f.name, ex) for ex in exclude)
                ]
            return files

        targets: list[tuple[str, list[Path]]] = []
        if root_files:
            targets.append((source_dir.name, root_files))
        targets.extend((ch_dir.name, _chapter_files(ch_dir)) for ch_dir in chapter_dirs)

        results: list[ConversionResult] = []
        for chapter_name, md_files in targets:
            if not md_files:
                continue
            out_file = out_dir / f"{chapter_name}-book.{fmt.value}"
            chapter_cover = cover.model_copy(
                update={"chapter": None}
            )  # no chapter label on sub-books
            result = await self.export_book(
                source_dir=md_files[0].parent,
                output_dir=out_dir,
                fmt=fmt,
                cover=chapter_cover,
                pattern=pattern,
                recursive=False,
                book_title=_clean_title(chapter_name),
                page_numbers=page_numbers,
                page_breaks=page_breaks,
                landscape=landscape,
                force=force,
                toc=toc,
                exclude=exclude,
            )
            # Rename to expected filename if needed
            if result.success and result.output.exists() and result.output != out_file:
                result.output.rename(out_file)
            results.append(
                ConversionResult(
                    source=md_files[0].parent,
                    output=out_file,
                    fmt=fmt,
                    success=result.success,
                    error=result.error,
                )
            )

        return results
