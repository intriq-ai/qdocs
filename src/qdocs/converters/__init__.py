"""qdocs converters package."""

from qdocs.converters.csv_to_xlsx import convert_csv_to_xlsx
from qdocs.converters.docx_to_md import convert_docx_to_md
from qdocs.converters.docx_to_pdf import convert_docx_to_pdf
from qdocs.converters.md_table_to_xlsx import convert_md_tables_to_xlsx
from qdocs.converters.md_to_docx import convert_book_to_docx, convert_md_to_docx
from qdocs.converters.md_to_pdf import convert_book_to_pdf, convert_md_to_pdf
from qdocs.converters.md_to_xlsx import XlsxFormatConfig, convert_md_to_xlsx
from qdocs.converters.mermaid import (
    check_mmdc,
    export_mermaid_blocks,
    extract_mermaid_blocks,
    generate_diagrams,
    generate_diagrams_cached,
)
from qdocs.converters.pdf_to_docx import convert_pdf_to_docx
from qdocs.converters.pdf_to_md import convert_pdf_to_md
from qdocs.converters.pdf_to_md_textract import (
    check_mfa_session as check_textract_mfa_session,
)
from qdocs.converters.pdf_to_md_textract import (
    convert_pdf_to_md_textract,
)
from qdocs.converters.pptx_to_md import convert_pptx_to_md
from qdocs.converters.utils import (
    find_image,
    get_file_hash,
    get_text_hash,
    should_convert,
)
from qdocs.converters.validators import (
    ValidationIssue,
    ValidationResult,
    validate_csv,
    validate_md_tables,
    validate_xlsx,
)
from qdocs.converters.xlsx_to_md import convert_xlsx_to_md
from qdocs.mcp.tools import (
    mcp_convert_csv_to_xlsx,
    mcp_extract_md_tables,
    mcp_validate_file,
)

__all__ = [
    "ValidationIssue",
    "ValidationResult",
    "XlsxFormatConfig",
    "check_mmdc",
    "check_textract_mfa_session",
    "convert_book_to_docx",
    "convert_book_to_pdf",
    "convert_csv_to_xlsx",
    "convert_docx_to_md",
    "convert_docx_to_pdf",
    "convert_md_tables_to_xlsx",
    "convert_md_to_docx",
    "convert_md_to_pdf",
    "convert_md_to_xlsx",
    "convert_pdf_to_docx",
    "convert_pdf_to_md",
    "convert_pdf_to_md_textract",
    "convert_pptx_to_md",
    "convert_xlsx_to_md",
    "export_mermaid_blocks",
    "extract_mermaid_blocks",
    "find_image",
    "generate_diagrams",
    "generate_diagrams_cached",
    "get_file_hash",
    "get_text_hash",
    "mcp_convert_csv_to_xlsx",
    "mcp_extract_md_tables",
    "mcp_validate_file",
    "should_convert",
    "validate_csv",
    "validate_md_tables",
    "validate_xlsx",
]
