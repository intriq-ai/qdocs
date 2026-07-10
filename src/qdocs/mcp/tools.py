"""Model Context Protocol (MCP) tool definitions for qdocs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qdocs.converters import (
    convert_csv_to_xlsx,
    convert_md_tables_to_xlsx,
    convert_md_to_xlsx,
    validate_csv,
    validate_md_tables,
    validate_xlsx,
)


def mcp_validate_file(source: str, format: str = "auto") -> dict[str, Any]:
    """Pre-flight syntax validation for CSV, Markdown tables, and XLSX.

    Args:
        source: Path to the file to validate.
        format: Validation format: auto | csv | md | xlsx.
    """
    path = Path(source)
    fmt = format.lower()
    if fmt == "auto":
        suffix = path.suffix.lower()
        if suffix == ".csv":
            fmt = "csv"
        elif suffix == ".md":
            fmt = "md"
        elif suffix == ".xlsx":
            fmt = "xlsx"
        else:
            return {"error": f"Unsupported format for auto-detect: {suffix}"}

    if fmt == "csv":
        res = validate_csv(path)
    elif fmt == "md":
        res = validate_md_tables(path)
    elif fmt == "xlsx":
        res = validate_xlsx(path)
    else:
        return {"error": f"Unknown format: {format}"}

    return {
        "valid": res.is_valid,
        "kind": res.kind,
        "errors": [{"line": i.line, "msg": i.message} for i in res.errors],
        "warnings": [{"line": i.line, "msg": i.message} for i in res.warnings],
    }


def mcp_convert_csv_to_xlsx(source: str, target: str | None = None) -> dict[str, Any]:
    """Convert a CSV file to an XLSX workbook.

    Args:
        source: Path to the source .csv file.
        target: Optional target path (defaults to same name with .xlsx).
    """
    src = Path(source)
    dst = Path(target) if target else src.with_suffix(".xlsx")
    try:
        convert_csv_to_xlsx(src, dst)
        return {"success": True, "target": str(dst)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def mcp_extract_md_tables(source: str, target: str | None = None) -> dict[str, Any]:
    """Extract markdown tables and convert them to an XLSX workbook.

    Args:
        source: Path to the source .md file.
        target: Optional target path (defaults to .tables.xlsx).
    """
    src = Path(source)
    dst = Path(target) if target else src.with_suffix(".tables.xlsx")
    try:
        convert_md_tables_to_xlsx(src, dst)
        return {"success": True, "target": str(dst)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def mcp_convert_md_to_xlsx(source: str, target: str | None = None) -> dict[str, Any]:
    """Convert a full Markdown document to a multi-sheet XLSX workbook.

    Each H2 heading creates a new sheet. Supports full document styling.

    Args:
        source: Path to the source .md file.
        target: Optional target path (defaults to .xlsx).
    """
    src = Path(source)
    dst = Path(target) if target else src.with_suffix(".xlsx")
    try:
        convert_md_to_xlsx(src, dst)
        return {"success": True, "target": str(dst)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
