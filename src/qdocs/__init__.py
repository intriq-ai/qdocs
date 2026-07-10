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
from qdocs.config import QdocsSettings

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

# Converters — core conversion functions
from qdocs.converters.docx_to_md import convert_docx_to_md
from qdocs.converters.docx_to_pdf import convert_docx_to_pdf
from qdocs.converters.md_to_docx import convert_book_to_docx, convert_md_to_docx
from qdocs.converters.md_to_pdf import convert_book_to_pdf, convert_md_to_pdf
from qdocs.converters.md_to_xlsx import XlsxFormatConfig, convert_md_to_xlsx
from qdocs.converters.mermaid import check_mmdc, generate_diagrams, generate_diagrams_cached
from qdocs.converters.pdf_to_docx import convert_pdf_to_docx
from qdocs.converters.pdf_to_md import convert_pdf_to_md
from qdocs.converters.pdf_to_md_textract import (
    check_mfa_session as check_textract_mfa_session,
    convert_pdf_to_md_textract,
)
from qdocs.converters.pptx_to_md import convert_pptx_to_md
from qdocs.converters.utils import find_image, get_file_hash, get_text_hash
from qdocs.converters.xlsx_to_md import convert_xlsx_to_md

# Services
from qdocs.services.exporter import ExportService
from qdocs.services.profile_manager import ProfileManager
from qdocs.services.revision_manager import RevisionManager

# Cache
from qdocs.cache.content_cache import ContentCache
from qdocs.cache.history import QdocsHistoryDB
from qdocs.cache.store import QDocsDB

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Version
    "__version__",
    # Config
    "QdocsSettings",
    # Exceptions
    "CacheError",
    "ConfigError",
    "ConversionError",
    "MmcdNotFoundError",
    "ProfileError",
    "QdocsError",
    "RevisionError",
    "SourceNotFoundError",
    # Models — Document
    "ConversionResult",
    "DocumentFormat",
    "DocumentMeta",
    # Models — Profile
    "CoverPageConfig",
    "DocumentClassification",
    "ProfileConfig",
    # Models — Revision
    "Revision",
    "RevisionHistory",
    # Converters — MD → *
    "convert_md_to_pdf",
    "convert_md_to_docx",
    "convert_md_to_xlsx",
    "XlsxFormatConfig",
    # Converters — * → MD
    "convert_pdf_to_md",
    "convert_pdf_to_md_textract",
    "convert_docx_to_md",
    "convert_xlsx_to_md",
    "convert_pptx_to_md",
    # Converters — interop
    "convert_docx_to_pdf",
    "convert_pdf_to_docx",
    # Converters — book
    "convert_book_to_pdf",
    "convert_book_to_docx",
    # Converters — diagrams
    "generate_diagrams",
    "generate_diagrams_cached",
    "check_mmdc",
    # Converters — Textract
    "check_textract_mfa_session",
    # Converters — utilities
    "find_image",
    "get_file_hash",
    "get_text_hash",
    # Services
    "ExportService",
    "ProfileManager",
    "RevisionManager",
    # Cache
    "ContentCache",
    "QDocsDB",
    "QdocsHistoryDB",
]
