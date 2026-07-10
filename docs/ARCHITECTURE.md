# QDocs — Architecture Decision Records

## ADR-001: Standalone Package Extraction

**Date:** 2026-07-10 | **Status:** Accepted

### Context
QDocs was embedded in `qcli.qdocs` as part of the monolithic qcli package. This
created tight coupling to qcli internals (LocalDB, MFA service, logging) and
prevented reuse outside the CLI context.

### Decision
Extract qdocs into a standalone first-class package at `utils/qdocs/`, following
the same patterns as `utils/qagents/`:

- Self-contained DuckDB base (`_local_db.py`) — zero qcli dependency
- Rich public API surface in `__init__.py`
- Own `pyproject.toml`, `ruff.toml`, `ty.toml`
- Thin re-export wrappers in `qcli/qdocs/` for backward compatibility
- Lazy imports for qcli-dependent features (ISMS export, preview, MFA)

### Alternatives Considered
1. **Keep in qcli** — rejected: tight coupling, no standalone reuse
2. **Move to qagents** — rejected: qdocs is not agent-specific, different domain
3. **Micro-package per converter** — rejected: over-fragmentation, shared models/cache

### Consequences
- ✅ Standalone installable via `uv add qdocs`
- ✅ Clean public API
- ✅ qagents can use qdocs without qcli
- ❌ Two packages to maintain (qdocs + qcli shims)
- ❌ ISMS features require qcli at runtime (lazy import)

---

## ADR-002: Content-Addressed Cache (URN Scheme)

**Date:** 2026-07-10 | **Status:** Accepted

### Context
Textract and AI calls are expensive ($/call, latency). We need caching that:
- Survives file renames/moves
- Works across different machines (future S3 backend)
- Is immune to timestamp-based false negatives

### Decision
Use a content-addressed URN scheme:
```
urn:intriq:qdocs:{kind}:{uuid5}
uuid5 = uuid5(NAMESPACE, "{kind}:{sha256_hex}")
```

Storage is sharded by hex prefix for filesystem performance:
```
~/.intriq/cache/qdocs/objects/{kind}/{hex[:2]}/{hex[2:4]}/{hex}.bin
```

Metadata indexed in DuckDB for queryability.

### Alternatives Considered
1. **File-path-based keys** — rejected: breaks on file rename/move
2. **Pure SHA-256 filenames** — rejected: no kind metadata, hard to manage
3. **Redis/SQLite** — rejected: DuckDB is already a dependency, simpler

### Consequences
- ✅ Stable across renames, moves, machines
- ✅ DuckDB metadata enables hit count tracking, TTL
- ✅ Backend-swappable (local → S3)
- ❌ Slightly more complex than file-path keys

---

## ADR-003: Lazy qcli Imports for ISMS Features

**Date:** 2026-07-10 | **Status:** Accepted

### Context
The ISMS export pipeline (`qdocs isms-export`) requires qcli for:
- `QismsSettings` (ISMS configuration)
- `isms_convert_md_to_pdf` (ISMS-specific cover page branding)
- `convert_toml_to_md` (Risk Register TOML→MD)
- `MFAService` (AWS MFA session check for Textract)

### Decision
Import qcli modules lazily inside the `IsmsExport.run()` and `Preview.run()`
methods, wrapped in try/except with user-friendly error messages when qcli
is not installed.

### Alternatives Considered
1. **Hard dependency on qcli** — rejected: defeats standalone purpose
2. **Move ISMS code to qdocs** — rejected: ISMS is a separate domain concern
3. **Optional dependency group** — rejected: overcomplicates install UX

### Consequences
- ✅ qdocs installs and runs standalone
- ✅ ISMS features work when qcli is available
- ✅ User gets clear error message when qcli is missing
- ❌ Import errors surface at runtime, not install time
