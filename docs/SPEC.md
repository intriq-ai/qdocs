# QDocs — Technical Specification

**Version:** 0.1.0 | **Date:** 2026-07-10

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Presentation Layer                        │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────────┐ │
│  │  CLI     │  │  Public API  │  │  Wrapper (qcli.qdocs)      │ │
│  │  cli.py  │  │  __init__.py │  │  re-exports from qdocs     │ │
│  └────┬─────┘  └──────┬───────┘  └────────────┬───────────────┘ │
└───────┼────────────────┼───────────────────────┼─────────────────┘
        │                │                       │
        ▼                ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Application Layer                          │
│  ┌────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│  │  ExportService │  │  ProfileManager  │  │ RevisionManager  │ │
│  │  services/     │  │  services/       │  │  services/       │ │
│  └───────┬────────┘  └────────┬─────────┘  └────────┬─────────┘ │
└──────────┼────────────────────┼─────────────────────┼───────────┘
           │                    │                     │
           ▼                    ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Domain Layer                              │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Converters (12)                                            │ │
│  │  md_to_pdf  md_to_docx  md_to_xlsx  mermaid                 │ │
│  │  docx_to_md docx_to_pdf pdf_to_md   pdf_to_docx             │ │
│  │  pptx_to_md xlsx_to_md  pdf_to_md_textract  highlighter     │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  Models      │  │  Config      │  │  Exceptions            │ │
│  │  models/     │  │  config.py   │  │  exceptions.py         │ │
│  └──────────────┘  └──────────────┘  └────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Infrastructure Layer                         │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Cache (DuckDB)                                             │ │
│  │  QDocsDB  ContentCache  QdocsHistoryDB                      │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌──────────────┐                                               │
│  │  _local_db   │  Self-contained DuckDB base (zero deps)       │
│  └──────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘
```

## 2. Domain Model

### 2.1 Bounded Contexts

```
┌──────────────────────┐  ┌──────────────────────┐
│  Conversion Context   │  │  Document Context     │
│  ─────────────────── │  │  ─────────────────── │
│  md_to_pdf           │  │  DocumentMeta        │
│  pdf_to_md           │  │  ConversionResult    │
│  md_to_docx          │  │  DocumentFormat      │
│  md_to_xlsx          │  │  Revision            │
│  docx_to_md          │  │  RevisionHistory     │
│  pptx_to_md          │  │                      │
│  xlsx_to_md          │  │                      │
│  mermaid (diagrams)  │  │                      │
└──────────┬───────────┘  └──────────┬───────────┘
           │                         │
           └─────────┬───────────────┘
                     │
           ┌─────────▼───────────┐
           │  Profile Context     │
           │  ─────────────────── │
           │  ProfileConfig       │
           │  CoverPageConfig     │
           │  DocumentClassification│
           └──────────────────────┘
```

### 2.2 Core Entities

```python
# Document identity
class DocumentMeta(BaseModel, frozen=True):
    source_path: Path
    content_hash: str
    mtime: float
    title: str | None

# Conversion outcome  
class ConversionResult(BaseModel, frozen=True):
    source: Path
    output: Path
    fmt: DocumentFormat      # pdf | docx | md | all
    success: bool
    cached: bool
    revision: int | None
    error: str | None

# Cover page branding
class CoverPageConfig(BaseModel, frozen=True):
    enabled: bool
    show_version: bool
    version_str: str | None
    show_date: bool
    show_revision: bool
    classification: DocumentClassification
    author: str | None
    chapter: str | None
    profile: ProfileConfig

# Revision tracking
class Revision(BaseModel, frozen=True):
    id: int
    source_path: str
    revision: int
    content_hash: str
    changed_at: datetime
    author: str | None
    note: str | None
```

### 2.3 Value Objects

- `DocumentClassification` (StrEnum): PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED, TOP_SECRET
- `DocumentFormat` (StrEnum): PDF, DOCX, MD, ALL
- `XlsxFormatConfig`: currency, locale, column width, freeze, wrap settings

## 3. Cache Architecture

### 3.1 Content-Addressed Cache (`ContentCache`)

```
URN:  urn:intriq:qdocs:{kind}:{uuid5}
      uuid5 = uuid5(NAMESPACE, "{kind}:{sha256_hex}")

Storage:  ~/.intriq/cache/qdocs/objects/
          {kind}/{hex[:2]}/{hex[2:4]}/{hex}.bin

Index:    ~/.intriq/cache/qdocs/cache.duckdb
          ┌─ cache_entries ────────────────────────┐
          │ urn (PK)   kind   src_hash   size_bytes │
          │ hit_count  created_at  last_hit_at      │
          └────────────────────────────────────────┘
```

### 3.2 Document Cache (`QDocsDB`)

```
~/.intriq/cache/qdocs/qdocs.duckdb

documents:  (source_path, output_format) → content_hash, output_path
diagrams:   (source_path, block_index) → content_hash, output_path
revisions:  id, source_path, revision, content_hash, changed_at, author, note
```

### 3.3 History Store (`QdocsHistoryDB`)

```
~/.intriq/cache/qdocs/history.duckdb

conversions:  id, source_path, source_hash, version, out_dir,
              provider, ai_model, pages, figures, ai_formatted, created_at
```

## 4. Public API Surface

```python
# Top-level convenience imports (like qagents)
from qdocs import (
    # Conversion
    convert_md_to_pdf,
    convert_md_to_docx,
    convert_md_to_xlsx,
    convert_pdf_to_md,
    convert_docx_to_md,
    convert_xlsx_to_md,
    convert_pptx_to_md,
    convert_docx_to_pdf,
    convert_pdf_to_docx,
    
    # Diagrams
    generate_diagrams,
    check_mmdc,
    
    # Models
    CoverPageConfig,
    DocumentClassification,
    ProfileConfig,
    ConversionResult,
    DocumentMeta,
    DocumentFormat,
    Revision,
    RevisionHistory,
    
    # Services
    ExportService,
    ProfileManager,
    RevisionManager,
    
    # Config
    QdocsSettings,
)

# Deep imports for specific needs
from qdocs.converters.md_to_docx import convert_book_to_docx
from qdocs.converters.pdf_to_md_textract import convert_pdf_to_md_textract
from qdocs.cache import ContentCache, QDocsDB, QdocsHistoryDB
```

## 5. CLI Command Tree

```
qdocs
├── convert
│   ├── to-pdf      MD → PDF (batch, cover, revision, landscape)
│   ├── to-docx     MD → DOCX (batch, cover, revision, page-breaks)
│   ├── to-xlsx     MD → XLSX (multi-sheet, currency, locale, formatting)
│   └── to-md       DOCX/PDF/XLSX/PPTX → MD (Textract, pdfplumber, AI)
├── diagrams
│   ├── generate    Render Mermaid blocks in MD files
│   ├── check       Verify mmdc is installed
│   ├── export      Export raw .mmd source files
│   └── list        List diagram blocks with line numbers
├── export          Full pipeline: diagrams → PDF/DOCX
│                   (book mode, chapter export, TOC, exclude patterns)
├── isms-export     ISMS pipeline: TOML→MD, versioned PDF/DOCX, revision pruning
├── preview         Paginated PNG previews (via LibreOffice headless)
├── revisions
│   ├── show        View revision history
│   ├── bump        Manually create new revision
│   └── clear       Delete revision history
├── profile
│   ├── show        Display profile settings
│   ├── init        Scaffold a new profile
│   ├── list        List installed profiles
│   └── set-logo    Set profile logo
└── cache
    ├── status      Show cache statistics
    └── clear       Clear document/diagram/revision cache
```

## 6. Error Hierarchy

```
QdocsError (base)
├── ConfigError           Invalid/missing configuration
├── ProfileError          Profile not found or invalid
├── ConversionError       Document conversion failed
├── SourceNotFoundError   Source file/directory does not exist
├── CacheError            Cache read/write error
├── RevisionError         Revision management error
└── MmcdNotFoundError     mermaid-cli (mmdc) not found in PATH
```

## 7. Configuration

Loaded from (priority order):
1. CLI arguments (highest)
2. Environment variables (`QDOCS_` prefix)
3. `config/.qdocs.toml` (searched upward from CWD)
4. Default values

```toml
# config/.qdocs.toml
[paths]
source_dir = "docs/"
output_dir = "~/.intriq/exports/qdocs"

[defaults]
format = "pdf"
pattern = "*.md"
recursive = true

[conversion]
force = false

[diagrams]
output_format = "svg"
width = 1920
background = "#0d1117"
cache = true

[cache]
path = "~/.intriq/cache/qdocs"

[profile]
name = "default"
path = "~/.intriq/profiles"
```

## 8. Integration Points

### 8.1 qcli (backward compat)
All `from qcli.qdocs.X import Y` continue to work via thin re-export wrappers:
```python
# qcli/qdocs/converters/md_to_pdf.py
from qdocs.converters.md_to_pdf import *
```

### 8.2 qagents
```python
from qdocs.converters import convert_md_to_pdf
# Used for executive report generation
```

### 8.3 ISMS (qisms)
```python
# Lazy import in qdocs.cli.IsmsExport.run()
from qcli.compliance.isms.adapters import isms_convert_md_to_pdf
```

### 8.4 PostHog exports
```python
from qdocs.converters.md_to_docx import convert_md_to_docx
from qdocs.models.profile import CoverPageConfig, DocumentClassification
```

## 9. Testing Strategy

| Layer | Tool | Scope |
|-------|------|-------|
| Unit | pytest | Models, config, utils, individual converters |
| Integration | pytest + moto | DuckDB cache, AWS Textract (mocked) |
| CLI | pytest + typer testing | Full command tree |
| E2E | qdemo validate | Real document conversion pipeline |

```bash
uv run pytest tests/ -v
uv run pytest tests/ -v -m "not slow"
```
