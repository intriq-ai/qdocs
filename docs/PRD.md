# QDocs — Product Requirements Document

**Version:** 0.1.0 | **Date:** 2026-07-10 | **Status:** Initial Release

---

## 1. Executive Summary

QDocs is a general-purpose document conversion, export, and revision management
library with a rich CLI. It transforms documents between formats (Markdown, PDF,
DOCX, XLSX, PPTX), renders Mermaid diagrams, manages branded cover pages, and
tracks document revisions in DuckDB.

Extracted from `qcli.qdocs` as a standalone first-class package at
`utils/qdocs/`, following the same patterns as `utils/qagents/`.

## 2. Problem Statement

- **Fragmentation**: Document conversion logic was embedded in the monolithic
  `qcli` package, preventing reuse outside the CLI context
- **Coupling**: Converters, cache stores, and profile management were tightly
  coupled to qcli's internal infrastructure (LocalDB, logging, MFA)
- **Discoverability**: No clear public API surface — consumers had to know
  internal module paths
- **Testability**: No standalone test suite; tests lived in qcli's monolith

## 3. Goals & Non-Goals

### Goals
- [x] Standalone installable package (`pip install qdocs` / `uv add qdocs`)
- [x] Rich public API surface — `from qdocs import convert_md_to_pdf`
- [x] Backward-compatible qcli wrapper — zero changes for existing consumers
- [x] Self-contained DuckDB layer — no qcli dependency
- [x] CLI parity — all commands preserved
- [x] Documented architecture, PRD, spec

### Non-Goals
- [ ] S3-backed cache (future: swap `ContentCache` backend)
- [ ] gRPC/HTTP API server (future: `qdocs serve`)
- [ ] Web UI
- [ ] Collaborative editing / real-time sync

## 4. User Personas

| Persona | Need | Primary Interface |
|---------|------|-------------------|
| **Platform Operator** | Export ISMS docs to branded PDF/DOCX | `qdocs export --book` |
| **Data Analyst** | Convert Excel→MD, PDF→MD for analysis | `qdocs convert to-md` |
| **Developer** | Embed conversion in Python scripts | `from qdocs import convert_md_to_pdf` |
| **Compliance Officer** | Track document revisions, classifications | `qdocs revisions show` |
| **AI Agent** | Programmatic document processing | Python API |

## 5. Functional Requirements

### FR1: Multi-Format Conversion
- MD → PDF, DOCX, XLSX
- PDF → MD (pdfplumber + Textract/AI)
- DOCX → MD, PDF
- XLSX → MD
- PPTX → MD

### FR2: Branded Cover Pages
- Configurable profiles (company, logo, classification, version, author)
- Auto-read version from document metadata (`**Version:** X.Y.Z`)
- Classification labels with color coding (PUBLIC→TOP SECRET)

### FR3: Mermaid Diagram Rendering
- Extract Mermaid blocks from Markdown
- Render to SVG/PNG via mmdc (mermaid-cli)
- Content-addressed caching (skip re-render when unchanged)
- Export raw .mmd source files

### FR4: Revision Management
- Auto-incrementing revision counter per document
- Content-hash change detection
- DuckDB-persisted history with author, notes, timestamps
- Prune stale revision files (ISMS export)

### FR5: Book Assembly
- Combine multiple MD files into single PDF/DOCX
- Auto-generated Table of Contents
- Chapter export (per-subdirectory book files)
- Page numbers, headers, footers

### FR6: Content Cache
- Content-addressed URN scheme (`urn:intriq:qdocs:{kind}:{uuid5}`)
- Sharded object storage (`~/.intriq/cache/qdocs/objects/`)
- DuckDB metadata index with hit counts
- TTL-based expiry

### FR7: ISMS Export Pipeline
- TOML→MD pre-processing (Risk Register)
- Versioned output directories
- Auto-incrementing REVISION counter
- Stale revision pruning
- Diagram regeneration with caching

## 6. Non-Functional Requirements

| NFR | Target |
|-----|--------|
| Python version | ≥3.14 |
| Cold start time | <500ms (import `qdocs`) |
| PDF conversion (50pg MD) | <10s |
| Cache hit (diagram) | <100ms |
| DuckDB lock timeout | 30s default, configurable |
| Test coverage target | >60% |
| Ruff lint | E, F, I, UP, RUF clean |
| Type checking | ty.toml baseline (warn on high-value rules) |

## 7. Dependencies

### Core (always required)
- `pydantic>=2.7`, `pydantic-settings>=2.3` — configuration & models
- `duckdb>=1.5` — cache & revision store
- `loguru>=0.7` — structured logging
- `rich>=13.0` — terminal output
- `clypi>=1.9.0` — CLI framework
- `pygments>=2.20` — syntax highlighting
- `markdown>=3.10` — MD parsing

### Conversion (format-specific)
- `python-docx>=1.2` — DOCX read/write
- `pdfplumber>=0.11` — PDF text extraction
- `reportlab>=5.0` — PDF generation (cover pages)
- `openpyxl>=3.1` — XLSX read/write
- `python-pptx>=1.0` — PPTX reading
- `playwright>=1.60` — HTML→PDF rendering
- `Pillow>=12.2` — image processing
- `svglib>=2.0` — SVG→ReportLab conversion

### Optional (lazy imports)
- `qcli` — ISMS export pipeline, preview, MFA checks
- `boto3` — AWS Textract PDF→MD
- `google-genai` — Vertex AI formatting
