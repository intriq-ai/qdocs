"""Document models — format, metadata, conversion results."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class DocumentFormat(StrEnum):
    """Supported document output formats."""

    PDF = "pdf"
    DOCX = "docx"
    MD = "md"
    ALL = "all"


class DocumentMeta(BaseModel):
    """Lightweight document identity record."""

    model_config = ConfigDict(frozen=True)

    source_path: Path
    content_hash: str
    mtime: float
    title: str | None = None


class ConversionResult(BaseModel):
    """Result of a single file conversion."""

    model_config = ConfigDict(frozen=True)

    source: Path
    output: Path
    fmt: DocumentFormat
    success: bool
    cached: bool = False
    revision: int | None = None
    error: str | None = None

    def __str__(self) -> str:
        status = "cached" if self.cached else ("✓" if self.success else "✗")
        return f"[{status}] {self.source.name} → {self.output.name}"
