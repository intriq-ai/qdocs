"""Content-addressable local cache for qdocs AI and Textract responses.

Cache entries are addressed by a UUID5 URN derived from SHA-256 of the input
bytes, making keys stable across runs and portable to future S3 storage.

URN format:  urn:intriq:qdocs:{kind}:{uuid5}
             where uuid5 = uuid5(NAMESPACE, "{kind}:{sha256_hex}")
Storage:     ~/.intriq/cache/qdocs/objects/{kind}/{hex[:2]}/{hex[2:4]}/{hex}.bin
Index:       ~/.intriq/cache/qdocs/cache.duckdb
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, ClassVar

from qdocs._local_db import LocalDB

# Stable UUID5 namespace for qdocs content addressing.
# Derived deterministically from a well-known URL — never changes.
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://intriq.ai/qdocs/cache/v1")


class _CacheIndexDB(LocalDB):
    _DEFAULT_PATH: ClassVar[Path] = (
        Path.home() / ".intriq" / "cache" / "qdocs" / "cache.duckdb"
    )
    _EXPECTED_TABLES: ClassVar[frozenset[str]] = frozenset({"cache_entries"})
    _SCHEMA_SQL: ClassVar[str] = """
        CREATE TABLE IF NOT EXISTS cache_entries (
            urn         TEXT      PRIMARY KEY,
            kind        TEXT      NOT NULL,
            src_hash    TEXT      NOT NULL,
            size_bytes  INTEGER   NOT NULL,
            hit_count   INTEGER   NOT NULL DEFAULT 0,
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_hit_at TIMESTAMP
        );
    """


class ContentCache:
    """Content-addressable local cache with a DuckDB metadata index.

    Content bytes are stored in a sharded directory tree; the DuckDB index
    tracks URNs, hit counts, and sizes.  The storage interface is designed to
    be backend-swappable (local → S3) without changing call sites.

    Args:
        objects_root:  Override the directory for stored objects.
        db_path:       Override the DuckDB index path.

    Example::

        cache = ContentCache()
        urn = ContentCache.urn_for("textract", png_bytes)
        blocks_json = cache.get_json(urn)
        if blocks_json is None:
            blocks = run_textract(png_bytes)
            cache.put_json(urn, [b.model_dump(mode="json") for b in blocks])
    """

    def __init__(
        self,
        objects_root: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        self._db = _CacheIndexDB(db_path)
        self._objects = objects_root or (self._db.path.parent / "objects")

    # ------------------------------------------------------------------
    # URN helpers
    # ------------------------------------------------------------------

    @staticmethod
    def urn_for(kind: str, content: bytes) -> str:
        """Return ``urn:intriq:qdocs:{kind}:{uuid5}`` for *content*.

        The URN is derived deterministically from SHA-256(*content*) and *kind*,
        so the same bytes always produce the same URN regardless of origin.
        """
        h = hashlib.sha256(content).hexdigest()
        uid = uuid.uuid5(_NAMESPACE, f"{kind}:{h}")
        return f"urn:intriq:qdocs:{kind}:{uid}"

    # ------------------------------------------------------------------
    # Storage path
    # ------------------------------------------------------------------

    def _path_for(self, urn: str) -> Path:
        uid_hex = urn.split(":")[-1].replace("-", "")
        kind = urn.split(":")[3]
        return self._objects / kind / uid_hex[:2] / uid_hex[2:4] / f"{uid_hex}.bin"

    # ------------------------------------------------------------------
    # Core get / put
    # ------------------------------------------------------------------

    def get(self, urn: str) -> bytes | None:
        """Return cached bytes for *urn*, or ``None`` on a miss."""
        p = self._path_for(urn)
        if not p.exists():
            return None
        data = p.read_bytes()
        try:
            with self._db.session() as conn:
                conn.execute(
                    "UPDATE cache_entries"
                    " SET hit_count = hit_count + 1, last_hit_at = CURRENT_TIMESTAMP"
                    " WHERE urn = ?",
                    [urn],
                )
        except Exception:
            pass
        return data

    def put(self, urn: str, content: bytes) -> None:
        """Store *content* at *urn*.  No-op if the entry already exists."""
        p = self._path_for(urn)
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        kind = urn.split(":")[3]
        src_hash = hashlib.sha256(content).hexdigest()
        try:
            with self._db.session() as conn:
                conn.execute(
                    "INSERT INTO cache_entries (urn, kind, src_hash, size_bytes)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (urn) DO NOTHING",
                    [urn, kind, src_hash, len(content)],
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Typed helpers
    # ------------------------------------------------------------------

    def get_text(self, urn: str) -> str | None:
        """Return cached UTF-8 text, or ``None`` on a miss."""
        data = self.get(urn)
        return data.decode() if data is not None else None

    def put_text(self, urn: str, text: str) -> None:
        """Store *text* as UTF-8 bytes at *urn*."""
        self.put(urn, text.encode())

    def get_json(self, urn: str) -> Any | None:
        """Return cached JSON-decoded object, or ``None`` on a miss."""
        data = self.get(urn)
        return json.loads(data) if data is not None else None

    def put_json(self, urn: str, obj: Any) -> None:
        """JSON-encode *obj* and store at *urn*."""
        self.put(urn, json.dumps(obj, ensure_ascii=False).encode())

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Return ``{entries, total_bytes, total_hits}`` summary."""
        try:
            with self._db.session() as conn:
                row = conn.execute(
                    "SELECT COUNT(*),"
                    "       COALESCE(SUM(size_bytes), 0),"
                    "       COALESCE(SUM(hit_count), 0)"
                    " FROM cache_entries"
                ).fetchone()
            return {"entries": row[0], "total_bytes": row[1], "total_hits": row[2]}
        except Exception:
            return {"entries": 0, "total_bytes": 0, "total_hits": 0}
