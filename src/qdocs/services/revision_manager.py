"""Revision manager — per-document revision tracking via DuckDB.

A new revision is recorded automatically whenever a document's content hash
changes (i.e. the file was actually modified).  Revisions can also be bumped
manually with an optional note/author.
"""

from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from qdocs.cache import QDocsDB
from qdocs.exceptions import RevisionError
from qdocs.models.revision import Revision, RevisionHistory


def _row_to_revision(row: tuple) -> Revision:
    """Map a DB row tuple → Revision model."""
    rid, source_path, revision, content_hash, changed_at, author, note = row
    if isinstance(changed_at, str):
        changed_at = datetime.fromisoformat(changed_at)
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=UTC)
    return Revision(
        id=rid,
        source_path=source_path,
        revision=revision,
        content_hash=content_hash,
        changed_at=changed_at,
        author=author,
        note=note,
    )


class RevisionManager:
    """Manages document revision history in the qdocs DuckDB cache."""

    def __init__(self, cache_dir: Path) -> None:
        self._db = QDocsDB(cache_dir)

    # ------------------------------------------------------------------
    # Core: auto-record on hash change
    # ------------------------------------------------------------------

    def record(
        self,
        source_path: Path,
        content_hash: str,
        author: str | None = None,
        note: str | None = None,
    ) -> Revision:
        """Record a revision if the content hash changed since the last revision.

        If the hash is unchanged, the existing latest revision is returned without
        creating a new entry (idempotent).

        Returns:
            The current (possibly new) Revision.
        """
        key = str(source_path.resolve())
        try:
            row = self._db.upsert_revision(key, content_hash, author=author, note=note)
            if row is None:
                msg = f"upsert_revision returned None for {source_path}"
                raise RevisionError(msg)
            rev = _row_to_revision(row)
            logger.debug(f"Revision r{rev.revision} for {source_path.name}")
            return rev
        except RevisionError:
            raise
        except Exception as exc:
            msg = f"Failed to record revision for {source_path}: {exc}"
            raise RevisionError(msg) from exc

    # ------------------------------------------------------------------
    # Manual operations
    # ------------------------------------------------------------------

    def bump(
        self,
        source_path: Path,
        content_hash: str | None = None,
        note: str | None = None,
        author: str | None = None,
    ) -> Revision:
        """Force a new revision regardless of whether the content changed.

        If content_hash is not provided, the hash from the last revision is reused.
        """
        from qdocs.converters.utils import get_file_hash

        key = str(source_path.resolve())
        resolved_hash = content_hash or (
            get_file_hash(source_path) if source_path.exists() else "manual"
        )

        try:
            # Force a new revision by first bumping the hash to something unique,
            # then inserting with the resolved hash via direct session call.
            with self._db.session() as conn:
                latest = conn.execute(
                    "SELECT revision FROM revisions WHERE source_path = ? ORDER BY revision DESC LIMIT 1",
                    [key],
                ).fetchone()
                next_rev = (latest[0] + 1) if latest else 1
                from datetime import UTC
                from datetime import datetime as _dt

                conn.execute(
                    "INSERT INTO revisions (source_path, revision, content_hash, changed_at, author, note)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [key, next_rev, resolved_hash, _dt.now(UTC), author, note],
                )
                row = conn.execute(
                    "SELECT id, source_path, revision, content_hash, changed_at, author, note"
                    " FROM revisions WHERE source_path = ? AND revision = ? ORDER BY id DESC LIMIT 1",
                    [key, next_rev],
                ).fetchone()
            rev = _row_to_revision(row)
            logger.info(f"Bumped to r{rev.revision} for {source_path.name}")
            return rev
        except Exception as exc:
            msg = f"Failed to bump revision for {source_path}: {exc}"
            raise RevisionError(msg) from exc

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_latest(self, source_path: Path) -> Revision | None:
        """Return the latest revision for a document, or None."""
        key = str(source_path.resolve())
        try:
            row = self._db.get_latest_revision(key)
            return _row_to_revision(row) if row else None
        except Exception as exc:
            msg = f"Failed to fetch latest revision for {source_path}: {exc}"
            raise RevisionError(msg) from exc

    def get_history(self, source_path: Path) -> RevisionHistory:
        """Return the full revision history for a document (newest first)."""
        key = str(source_path.resolve())
        try:
            rows = self._db.get_all_revisions(key)
            revisions = tuple(_row_to_revision(r) for r in rows)
            return RevisionHistory(source_path=key, revisions=revisions)
        except Exception as exc:
            msg = f"Failed to fetch revision history for {source_path}: {exc}"
            raise RevisionError(msg) from exc

    def clear(self, source_path: Path) -> int:
        """Delete all revision records for a document. Returns number deleted."""
        key = str(source_path.resolve())
        try:
            count = self._db.delete_revisions(key)
            logger.info(f"Cleared {count} revision(s) for {source_path.name}")
            return count
        except Exception as exc:
            msg = f"Failed to clear revisions for {source_path}: {exc}"
            raise RevisionError(msg) from exc
