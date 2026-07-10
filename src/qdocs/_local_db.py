"""DuckDB base class for qdocs local (persistent) stores.

Self-contained — free of any qcli dependency so this module loads cleanly
in any environment where qdocs is installed.

All qdocs persistent local stores extend :class:`LocalDB` and get:

- **Ephemeral per-call connections**: the file lock is held for the minimum time.
- **In-process thread safety**: a per-instance ``threading.Lock`` serialises callers.
- **Cross-process retry**: backs off up to ``wait_s`` seconds on file-lock contention.
- **Schema init at construction**: ``_SCHEMA_SQL`` executed once on first open.
- **Additive migrations**: ``_MIGRATION_SQL`` executed after schema (safe on existing DBs).
- **Stale artefact cleanup**: ``force=True`` removes ``.wal/.lock/.tmp`` before first attempt.
- **Auto-purge**: rows older than ``_PURGE_POLICY`` thresholds are deleted in a daemon thread.
- **Purge**: ``db.purge()`` deletes the DB file (used by cache-clear commands).
- **Health check**: ``db.health_check()`` verifies the file and expected tables.

Usage::

    class MyDB(LocalDB):
        _DEFAULT_PATH    = Path.home() / ".intriq" / "cache" / "my.duckdb"
        _SCHEMA_SQL      = "CREATE TABLE IF NOT EXISTS ..."
        _EXPECTED_TABLES = frozenset({"my_table"})

        def query_something(self) -> list[dict]:
            with self.session() as conn:
                rows = conn.execute("SELECT ...").fetchall()
            return rows

    db = MyDB()
    rows = db.query_something()
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, ClassVar

from loguru import logger

_RETRY_INTERVAL_S: float = 0.5


def _remove_wal_artefacts(path: Path) -> None:
    """Remove stale DuckDB WAL / lock files left by a crashed process."""
    for suffix in (".wal", ".lock", ".tmp"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            logger.warning("Removing stale DuckDB artefact: {}", candidate)
            with contextlib.suppress(Exception):
                candidate.unlink(missing_ok=True)


class LocalDB:
    """DuckDB connection manager for qdocs local persistent stores.

    Subclass and set ``_DEFAULT_PATH``.  Override ``_SCHEMA_SQL`` and
    ``_MIGRATION_SQL`` to define the schema.  Set ``_EXPECTED_TABLES`` for
    :meth:`health_check` to verify table presence.  Add domain methods that call
    ``self.session()`` — callers never touch ``conn`` directly.
    """

    _SCHEMA_SQL: ClassVar[str] = ""
    _MIGRATION_SQL: ClassVar[str] = ""
    _DEFAULT_PATH: ClassVar[Path | None] = None
    _EXPECTED_TABLES: ClassVar[frozenset[str]] = frozenset()
    # Each entry: (table_name, timestamp_column, max_age_days).
    _PURGE_POLICY: ClassVar[list[tuple[str, str, int]]] = []

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        wait_s: int = 30,
        force: bool = False,
    ) -> None:
        import os

        resolved = (
            db_path
            or os.environ.get(self._path_env_var() or "", None)
            or self._DEFAULT_PATH
        )
        if resolved is None:
            raise ValueError(
                f"{type(self).__name__}: db_path required (no _DEFAULT_PATH set)"
            )
        self._path = Path(resolved)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._wait_s = wait_s
        self._force = force
        self._lock = threading.Lock()

        with self.session() as conn:
            if self._SCHEMA_SQL:
                conn.execute(self._SCHEMA_SQL)
            if self._MIGRATION_SQL:
                try:
                    conn.execute(self._MIGRATION_SQL)
                except Exception as _mig_exc:
                    logger.debug(
                        "LocalDB [{}]: migration skipped: {}",
                        type(self).__name__,
                        _mig_exc,
                    )

        logger.debug("LocalDB [{}]: {}", type(self).__name__, self._path)

        # Background purge — non-blocking, best-effort.
        if self._PURGE_POLICY:
            _t = threading.Thread(
                target=self.purge_expired,
                daemon=True,
                name=f"{type(self).__name__}.purge",
            )
            _t.start()

    def _path_env_var(self) -> str | None:
        """Return the env-var name that overrides the DB path, or None."""
        return None

    @property
    def path(self) -> Path:
        """Resolved path to the DuckDB file."""
        return self._path

    def purge(self) -> Path:
        """Delete the DB file. Returns the path whether or not it existed."""
        if self._path.exists():
            self._path.unlink()
        return self._path

    def purge_expired(self) -> int:
        """Delete rows older than the per-table thresholds in ``_PURGE_POLICY``.

        Returns the total number of rows deleted.
        """
        if not self._PURGE_POLICY:
            return 0
        total = 0
        try:
            with self.session() as conn:
                for table, column, days in self._PURGE_POLICY:
                    try:
                        row = conn.execute(
                            f"DELETE FROM {table}"
                            f" WHERE {column} < NOW() - INTERVAL '{days} days'"
                        ).fetchone()
                        deleted = row[0] if row else 0
                        if deleted:
                            logger.debug(
                                "{}: purged {} expired rows from {} (>{} days)",
                                type(self).__name__,
                                deleted,
                                table,
                                days,
                            )
                        total += deleted
                    except Exception as exc:
                        logger.debug(
                            "{}: purge_expired [{}.{}] failed: {}",
                            type(self).__name__,
                            table,
                            column,
                            exc,
                        )
        except Exception as exc:
            logger.debug(
                "{}: purge_expired outer failed: {}", type(self).__name__, exc
            )
        return total

    def is_empty(self) -> bool:
        """Return True if all tables in ``_EXPECTED_TABLES`` have zero rows."""
        if not self._EXPECTED_TABLES:
            return True
        try:
            with self.session() as conn:
                for table in self._EXPECTED_TABLES:
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    if count:
                        return False
            return True
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Session / connection management
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def session(self) -> Generator[Any]:
        """Acquire a thread-safe DuckDB connection with retry on lock contention.

        Usage::

            with db.session() as conn:
                conn.execute("SELECT ...")
        """
        import duckdb

        with self._lock:
            start = time.monotonic()

            if self._force:
                _remove_wal_artefacts(self._path)

            while True:
                try:
                    conn = duckdb.connect(str(self._path))
                    try:
                        yield conn
                    finally:
                        conn.close()
                    return
                except duckdb.IOException as exc:
                    elapsed = time.monotonic() - start
                    if elapsed > self._wait_s:
                        raise TimeoutError(
                            f"DuckDB lock timeout after {self._wait_s}s: "
                            f"{self._path}"
                        ) from exc
                    logger.debug(
                        "DuckDB lock contention [{:.1f}s]: {}",
                        elapsed,
                        self._path,
                    )
                    time.sleep(_RETRY_INTERVAL_S)
                except Exception:
                    raise

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """Verify the DB file exists and expected tables are present."""
        result: dict[str, Any] = {
            "path": str(self._path),
            "exists": self._path.exists(),
            "size_bytes": self._path.stat().st_size if self._path.exists() else 0,
            "tables": {},
            "healthy": False,
        }
        if not self._path.exists():
            return result
        try:
            with self.session() as conn:
                for table in self._EXPECTED_TABLES:
                    try:
                        row = conn.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()
                        result["tables"][table] = row[0] if row else 0
                    except Exception:
                        result["tables"][table] = "error"
            result["healthy"] = all(
                isinstance(v, int) for v in result["tables"].values()
            )
        except Exception:
            pass
        return result

    def row_counts(self) -> dict[str, int]:
        """Return ``{table_name: row_count}`` for all expected tables."""
        counts: dict[str, int] = {}
        try:
            with self.session() as conn:
                for table in self._EXPECTED_TABLES:
                    try:
                        row = conn.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()
                        counts[table] = row[0] if row else 0
                    except Exception:
                        counts[table] = -1
        except Exception:
            pass
        return counts
