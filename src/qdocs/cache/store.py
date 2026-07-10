"""DuckDB-backed local cache for qdocs.

Default path: ~/.intriq/cache/qdocs/qdocs.duckdb

Schema
------
documents    — (source_path, output_format) → content_hash + output_path; skip re-export when unchanged
diagrams     — (source_path, block_index) → per-block content_hash; skip re-render when unchanged
revisions    — full revision history per document (auto-incremented on hash change)
cache_meta   — generic key/value TTL store

Direct queryability
-------------------
    duckdb ~/.intriq/cache/qdocs/qdocs.duckdb
    SELECT * FROM revisions ORDER BY source_path, revision DESC;
    SELECT * FROM documents WHERE output_format = 'pdf';
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from qdocs._local_db import LocalDB

_DEFAULT_CACHE_ROOT = Path.home() / ".intriq" / "cache"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    source_path   TEXT         NOT NULL,
    content_hash  TEXT         NOT NULL,
    mtime         DOUBLE       NOT NULL,
    output_format TEXT         NOT NULL,
    output_path   TEXT         NOT NULL,
    built_at      TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (source_path, output_format)
);
CREATE TABLE IF NOT EXISTS diagrams (
    source_path   TEXT         NOT NULL,
    block_index   INTEGER      NOT NULL,
    content_hash  TEXT         NOT NULL,
    output_path   TEXT         NOT NULL,
    rendered_at   TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (source_path, block_index)
);
CREATE SEQUENCE IF NOT EXISTS revisions_id_seq START 1;
CREATE TABLE IF NOT EXISTS revisions (
    id            INTEGER      PRIMARY KEY DEFAULT nextval('revisions_id_seq'),
    source_path   TEXT         NOT NULL,
    revision      INTEGER      NOT NULL,
    content_hash  TEXT         NOT NULL,
    changed_at    TIMESTAMPTZ  NOT NULL,
    author        TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS revisions_source ON revisions(source_path, revision DESC);
CREATE TABLE IF NOT EXISTS cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class QDocsDB(LocalDB):
    """DuckDB-backed local cache for qdocs documents, diagrams, and revisions."""

    _SCHEMA_SQL = _SCHEMA
    _DEFAULT_PATH = _DEFAULT_CACHE_ROOT / "qdocs" / "qdocs.duckdb"

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        wait_s: int = 30,
        force: bool = False,
    ) -> None:
        path = (
            Path(cache_dir) / "qdocs" / "qdocs.duckdb"
            if cache_dir is not None
            else None
        )
        super().__init__(path, wait_s=wait_s, force=force)

    # ── Document cache ────────────────────────────────────────────────────────

    def is_document_cached(
        self, source_path: str, output_format: str, current_hash: str
    ) -> bool:
        """Return True when the stored hash matches *current_hash*."""
        with self.session() as conn:
            row = conn.execute(
                "SELECT content_hash FROM documents WHERE source_path = ? AND output_format = ?",
                [source_path, output_format],
            ).fetchone()
        return row is not None and row[0] == current_hash

    def upsert_document(
        self,
        source_path: str,
        content_hash: str,
        mtime: float,
        output_format: str,
        output_path: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO documents (source_path, content_hash, mtime, output_format, output_path, built_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_path, output_format) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    mtime        = excluded.mtime,
                    output_path  = excluded.output_path,
                    built_at     = excluded.built_at
                """,
                [source_path, content_hash, mtime, output_format, output_path, now],
            )

    # ── Diagram cache ─────────────────────────────────────────────────────────

    def is_diagram_cached(
        self, source_path: str, block_index: int, content_hash: str
    ) -> bool:
        """Return True when the stored block hash matches — diagram unchanged."""
        with self.session() as conn:
            row = conn.execute(
                "SELECT content_hash FROM diagrams WHERE source_path = ? AND block_index = ?",
                [source_path, block_index],
            ).fetchone()
        return row is not None and row[0] == content_hash

    def upsert_diagram(
        self,
        source_path: str,
        block_index: int,
        content_hash: str,
        output_path: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO diagrams (source_path, block_index, content_hash, output_path, rendered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (source_path, block_index) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    output_path  = excluded.output_path,
                    rendered_at  = excluded.rendered_at
                """,
                [source_path, block_index, content_hash, output_path, now],
            )

    # ── Revision history ──────────────────────────────────────────────────────

    def get_latest_revision(self, source_path: str) -> tuple | None:
        """Return the latest revision row, or ``None`` when no revision exists."""
        with self.session() as conn:
            return conn.execute(
                """
                SELECT id, source_path, revision, content_hash, changed_at, author, note
                FROM revisions WHERE source_path = ?
                ORDER BY revision DESC LIMIT 1
                """,
                [source_path],
            ).fetchone()

    def upsert_revision(
        self,
        source_path: str,
        content_hash: str,
        author: str | None = None,
        note: str | None = None,
    ) -> tuple | None:
        """Insert a new revision if *content_hash* differs from the latest, else return the existing row.

        Returns the revision row (id, source_path, revision, content_hash, changed_at, author, note).
        """
        now = datetime.now(UTC)
        with self.session() as conn:
            latest = conn.execute(
                "SELECT id, source_path, revision, content_hash, changed_at, author, note "
                "FROM revisions WHERE source_path = ? ORDER BY revision DESC LIMIT 1",
                [source_path],
            ).fetchone()
            if latest and latest[3] == content_hash:
                return latest
            next_rev = (latest[2] + 1) if latest else 1
            conn.execute(
                "INSERT INTO revisions (source_path, revision, content_hash, changed_at, author, note)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [source_path, next_rev, content_hash, now, author, note],
            )
            return conn.execute(
                "SELECT id, source_path, revision, content_hash, changed_at, author, note "
                "FROM revisions WHERE source_path = ? AND revision = ? ORDER BY id DESC LIMIT 1",
                [source_path, next_rev],
            ).fetchone()

    def get_all_revisions(self, source_path: str) -> list[tuple]:
        """Return all revisions for a document, newest first."""
        with self.session() as conn:
            return conn.execute(
                """
                SELECT id, source_path, revision, content_hash, changed_at, author, note
                FROM revisions WHERE source_path = ?
                ORDER BY revision DESC
                """,
                [source_path],
            ).fetchall()

    def delete_revisions(self, source_path: str) -> int:
        """Delete all revisions for a document. Returns deleted count."""
        with self.session() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM revisions WHERE source_path = ?", [source_path]
            ).fetchone()[0]
            conn.execute("DELETE FROM revisions WHERE source_path = ?", [source_path])
        return count

    # ── Stats ─────────────────────────────────────────────────────────────────

    def cache_stats(self) -> dict:
        """Return summary stats for the cache UI."""
        with self.session() as conn:
            docs = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT source_path) FROM documents"
            ).fetchone()
            diags = conn.execute("SELECT COUNT(*) FROM diagrams").fetchone()
            revs = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT source_path) FROM revisions"
            ).fetchone()
        return {
            "documents_entries": docs[0],
            "documents_sources": docs[1],
            "diagrams_entries": diags[0],
            "revisions_total": revs[0],
            "revisions_sources": revs[1],
        }

    def clear_documents(self) -> None:
        with self.session() as conn:
            conn.execute("DELETE FROM documents")

    def clear_diagrams(self) -> None:
        with self.session() as conn:
            conn.execute("DELETE FROM diagrams")

    def clear_revisions(self) -> None:
        with self.session() as conn:
            conn.execute("DELETE FROM revisions")
