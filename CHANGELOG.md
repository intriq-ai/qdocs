# Changelog

All notable changes to qdocs are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
Versioning: [Semantic Versioning](https://semver.org/)

---

## [Unreleased]

### Added
- Pre-flight validation command: `qdocs validate --source <file> --format auto|csv|md|xlsx`
- CSV syntax validator with delimiter detection and row-shape consistency checks
- Markdown table syntax validator with separator-row and column consistency checks
- XLSX integrity validator (workbook openability, sheet-level checks)
- New converter command: `qdocs convert csv-to-xlsx`
- New converter command: `qdocs convert md-table-to-xlsx`
- `convert_csv_to_xlsx` converter with type inference (number/currency/date), header styling, freeze pane, and auto-width
- `convert_md_tables_to_xlsx` converter for extracting standalone markdown tables into one or many sheets
- Extended `XlsxFormatConfig` with advanced worksheet support:
	- merged ranges
	- conditional formatting rules
	- data validation rules

### Tests
- Added tests for CSV/Markdown validators
- Added tests for CSV->XLSX converter output
- Added tests for Markdown-table->XLSX converter output

## [0.1.0] — 2026-07-10

### Added — Initial Release

#### Package Infrastructure
- Standalone package at `utils/qdocs/` extracted from `qcli.qdocs`
- Self-contained `_local_db.py` — zero qcli dependency for DuckDB operations
- Rich public API surface in `__init__.py` (qagents-style convenience imports)
- `pyproject.toml` with hatchling build, uv dependency management
- `ruff.toml` (line-length=120, py314), `ty.toml`, `.pre-commit-config.yaml`
- Comprehensive docs: PRD, SPEC, ARCHITECTURE (ADR-001 through ADR-003)

#### Conversion (12 converters)
- **MD → PDF**: branded cover pages, revision tracking, landscape, page numbers
- **MD → DOCX**: cover pages, syntax highlighting, page breaks, metadata headers
- **MD → XLSX**: multi-sheet workbooks, currency/locale formatting, auto-fit columns
- **PDF → MD**: pdfplumber (local) or AWS Textract + Bedrock AI formatting
- **DOCX → MD, XLSX → MD, PPTX → MD**: office document extraction
- **DOCX → PDF, PDF → DOCX**: office format interop
- **Mermaid**: block extraction, SVG/PNG rendering, caching, .mmd export
- **Syntax highlighting**: Pygments-based code block rendering

#### Domain Models (Pydantic v2, frozen)
- `CoverPageConfig`, `ProfileConfig`, `DocumentClassification` (StrEnum)
- `DocumentMeta`, `ConversionResult`, `DocumentFormat` (StrEnum)
- `Revision`, `RevisionHistory`

#### Application Services
- `ExportService`: batch export, book assembly, chapter export, TOC generation
- `ProfileManager`: load, init, list, set-logo for branding profiles
- `RevisionManager`: auto-incrementing revision counter, content-hash detection

#### Cache Layer (DuckDB)
- `QDocsDB`: documents + diagrams + revisions store
- `ContentCache`: URN-based content-addressed cache (`urn:intriq:qdocs:{kind}:{uuid5}`)
- `QdocsHistoryDB`: to-md conversion history

#### CLI (clypi command tree)
- `qdocs convert to-pdf|to-docx|to-xlsx|to-md`
- `qdocs diagrams generate|check|export|list`
- `qdocs export` (batch, book, chapter, TOC, exclude patterns)
- `qdocs isms-export` (versioned PDF/DOCX, TOML→MD, revision pruning)
- `qdocs preview` (paginated PNG via LibreOffice headless)
- `qdocs revisions show|bump|clear`
- `qdocs profile show|init|list|set-logo`
- `qdocs cache status|clear`

#### Backward Compatibility
- Thin re-export wrappers in `qcli/qdocs/` — all existing imports continue to work
- qcli depends on qdocs via local path (`utils/qdocs/`)
- Lazy qcli imports for ISMS features (runs standalone without qcli installed)

#### Configuration
- `QdocsSettings`: Pydantic-settings with env var + config file + defaults
- `config/.qdocs.toml`: paths, defaults, conversion, diagrams, cache, profile
- Environment variable prefix: `QDOCS_`

#### Error Handling
- Typed exception hierarchy: `QdocsError` → `ConfigError`, `ProfileError`, `ConversionError`, `SourceNotFoundError`, `CacheError`, `RevisionError`, `MmcdNotFoundError`
