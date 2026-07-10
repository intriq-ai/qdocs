"""Revision models — per-document revision history."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Revision(BaseModel):
    """A single revision record for a document."""

    model_config = ConfigDict(frozen=True)

    id: int
    source_path: str
    revision: int
    content_hash: str
    changed_at: datetime
    author: str | None = None
    note: str | None = None

    def __str__(self) -> str:
        note_part = f" — {self.note}" if self.note else ""
        author_part = f" ({self.author})" if self.author else ""
        return f"r{self.revision}{author_part} @ {self.changed_at.strftime('%Y-%m-%d %H:%M')}{note_part}"


class RevisionHistory(BaseModel):
    """Ordered revision history for a single document (newest first)."""

    model_config = ConfigDict(frozen=True)

    source_path: str
    revisions: tuple[Revision, ...]

    @property
    def latest(self) -> Revision | None:
        """Most recent revision, or None if no history."""
        return self.revisions[0] if self.revisions else None

    @property
    def current_revision(self) -> int:
        """Current revision number (0 if no history)."""
        return self.latest.revision if self.latest else 0

    @classmethod
    def empty(cls, source_path: Path) -> RevisionHistory:
        """Empty history for a new document."""
        return cls(source_path=str(source_path), revisions=())
