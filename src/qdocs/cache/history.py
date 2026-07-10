"""Conversion history store for qdocs to-md operations.

Each PDF conversion is recorded with the source content hash, version number,
output directory, provider, model, page count, and figure count.  Version
numbers are filesystem-derived (next available -vN directory), making the
sequence predictable and self-consistent with the on-disk layout.

Default path: ~/.intriq/cache/qdocs/history.duckdb
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar

from qdocs._local_db import LocalDB


class QdocsHistoryDB(LocalDB):
    """DuckDB-backed conversion history for qdocs to-md."""

    _DEFAULT_PATH: ClassVar[Path] = (
        Path.home() / ".intriq" / "cache" / "qdocs" / "history.duckdb"
    )
    _EXPECTED_TABLES: ClassVar[frozenset[str]] = frozenset({"conversions"})
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE SEQUENCE IF NOT EXISTS conversions_id_seq START 1;
        CREATE TABLE IF NOT EXISTS conversions (
            id           INTEGER   PRIMARY KEY DEFAULT nextval('conversions_id_seq'),
            source_path  TEXT      NOT NULL,
            source_hash  TEXT      NOT NULL,
            version      INTEGER   NOT NULL,
            out_dir      TEXT      NOT NULL,
            provider     TEXT,
            ai_model     TEXT,
            pages        INTEGER,
            figures      INTEGER   NOT NULL DEFAULT 0,
            ai_formatted BOOLEAN   NOT NULL DEFAULT FALSE,
            created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS conversions_hash
            ON conversions(source_hash, version DESC);
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def source_hash(source: Path) -> str:
        """Return the SHA-256 hex digest of a source file's bytes."""
        return hashlib.sha256(source.read_bytes()).hexdigest()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        source: Path,
        source_hash: str,
        out_dir: Path,
        version: int,
        provider: str,
        ai_model: str | None,
        pages: int,
        figures: int,
        ai_formatted: bool,
    ) -> None:
        """Insert a conversion record."""
        with self.session() as conn:
            conn.execute(
                "INSERT INTO conversions"
                " (source_path, source_hash, version, out_dir,"
                "  provider, ai_model, pages, figures, ai_formatted)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(source),
                    source_hash,
                    version,
                    str(out_dir),
                    provider,
                    ai_model,
                    pages,
                    figures,
                    ai_formatted,
                ],
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_versions(self, source: Path) -> list[dict]:
        """Return all conversions for *source* (newest version first)."""
        src_hash = self.source_hash(source)
        with self.session() as conn:
            rows = conn.execute(
                "SELECT id, version, out_dir, provider, ai_model, pages,"
                "        figures, ai_formatted, created_at"
                " FROM conversions WHERE source_hash = ?"
                " ORDER BY version DESC",
                [src_hash],
            ).fetchall()
        cols = [
            "id",
            "version",
            "out_dir",
            "provider",
            "ai_model",
            "pages",
            "figures",
            "ai_formatted",
            "created_at",
        ]
        return [dict(zip(cols, row, strict=False)) for row in rows]
