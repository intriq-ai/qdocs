"""FastMCP server for qdocs tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from qdocs.mcp.tools import (
    mcp_convert_csv_to_xlsx,
    mcp_convert_md_to_xlsx,
    mcp_extract_md_tables,
    mcp_validate_file,
)

mcp = FastMCP("qdocs")


@mcp.tool()
async def validate_syntax(source: str, format: str = "auto") -> dict[str, Any]:
    """Pre-flight syntax validation for CSV, Markdown tables, and XLSX."""
    return mcp_validate_file(source, format)


@mcp.tool()
async def convert_csv_to_xlsx(source: str, target: str | None = None) -> dict[str, Any]:
    """Convert a CSV file to an XLSX workbook with type inference and header styling."""
    return mcp_convert_csv_to_xlsx(source, target)


@mcp.tool()
async def extract_md_tables(source: str, target: str | None = None) -> dict[str, Any]:
    """Extract all GFM markdown tables from a file into a multi-sheet XLSX workbook."""
    return mcp_extract_md_tables(source, target)


@mcp.tool()
async def convert_md_to_xlsx(source: str, target: str | None = None) -> dict[str, Any]:
    """Convert a full Markdown document to a multi-sheet XLSX workbook with full styling."""
    return mcp_convert_md_to_xlsx(source, target)


if __name__ == "__main__":
    mcp.run()
