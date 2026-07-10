"""qdocs — General-purpose document conversion, export, and revision management.

Public API surface — import the most commonly needed names so callers can do::

    from qdocs import convert_md_to_pdf, CoverPageConfig, ExportService

instead of knowing the full module path.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("qdocs")
except PackageNotFoundError:
    __version__ = "0.0.0"

# ---------------------------------------------------------------------------
# Public surface — top-level convenience imports
# ---------------------------------------------------------------------------

# Config
# Cache
from qdocs.cache.content_cache import ContentCache
from qdocs.cache.history import QdocsHistoryDB
from qdocs.cache.store import QDocsDB
from qdocs.config import QdocsSettings
from qdocs.converters.csv_to_xlsx import convert_csv_to_xlsx

# Converters — core conversion functions
from qdocs.converters.docx_to_md import convert_docx_to_md
from qdocs.converters.docx_to_pdf import convert_docx_to_pdf
from qdocs.converters.md_table_to_xlsx import convert_md_tables_to_xlsx
from qdocs.converters.md_to_docx import convert_book_to_docx, convert_md_to_docx
from qdocs.converters.md_to_pdf import convert_book_to_pdf, convert_md_to_pdf
from qdocs.converters.md_to_xlsx import XlsxFormatConfig, convert_md_to_xlsx
from qdocs.converters.mermaid import check_mmdc, generate_diagrams, generate_diagrams_cached
from qdocs.converters.pdf_to_docx import convert_pdf_to_docx
from qdocs.converters.pdf_to_md import convert_pdf_to_md
from qdocs.converters.pdf_to_md_textract import (
    check_mfa_session as check_textract_mfa_session,
)
from qdocs.converters.pdf_to_md_textract import (
    convert_pdf_to_md_textract,
)
from qdocs.converters.pptx_to_md import convert_pptx_to_md
from qdocs.converters.utils import find_image, get_file_hash, get_text_hash
from qdocs.converters.validators import validate_csv, validate_md_tables, validate_xlsx
from qdocs.converters.xlsx_to_md import convert_xlsx_to_md

# Exceptions
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

# Models
from qdocs.models.document import ConversionResult, DocumentFormat, DocumentMeta
from qdocs.models.profile import (
    CoverPageConfig,
    DocumentClassification,
    ProfileConfig,
)
from qdocs.models.revision import Revision, RevisionHistory

# Services
from qdocs.services.exporter import ExportService
from qdocs.services.profile_manager import ProfileManager
from qdocs.services.revision_manager import RevisionManager

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Exceptions
    "CacheError",
    "ConfigError",
    # Cache
    "ContentCache",
    "ConversionError",
    # Models — Document
    "ConversionResult",
    # Models — Profile
    "CoverPageConfig",
    "DocumentClassification",
    "DocumentFormat",
    "DocumentMeta",
    # Services
    "ExportService",
    "MmcdNotFoundError",
    "ProfileConfig",
    "ProfileError",
    "ProfileManager",
    "QDocsDB",
    "QdocsError",
    "QdocsHistoryDB",
    # Config
    "QdocsSettings",
    # Models — Revision
    "Revision",
    "RevisionError",
    "RevisionHistory",
    "RevisionManager",
    "SourceNotFoundError",
    "XlsxFormatConfig",
    # Version
    "__version__",
    "check_mmdc",
    # Converters — Textract
    "check_textract_mfa_session",
    "convert_book_to_docx",
    # Converters — book
    "convert_book_to_pdf",
    "convert_csv_to_xlsx",
    "convert_docx_to_md",
    # Converters — interop
    "convert_docx_to_pdf",
    "convert_md_tables_to_xlsx",
    "convert_md_to_docx",
    # Converters — MD → *
    "convert_md_to_pdf",
    "convert_md_to_xlsx",
    "convert_pdf_to_docx",
    # Converters — * → MD
    "convert_pdf_to_md",
    "convert_pdf_to_md_textract",
    "convert_pptx_to_md",
    "convert_xlsx_to_md",
    # Converters — utilities
    "find_image",
    # Converters — diagrams
    "generate_diagrams",
    "generate_diagrams_cached",
    "get_file_hash",
    "get_text_hash",
    "validate_csv",
    "validate_md_tables",
    "validate_xlsx",
]
