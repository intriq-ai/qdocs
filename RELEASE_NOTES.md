# Release Notes — qdocs v0.1.0

**Released**: 2026-07-10
**Type**: Initial — extracted from qcli qdocs module

---

## Overview

v0.1.0 establishes qdocs as a standalone first-class package, extracted from the
qcli monolith. All core conversion, export, diagram, revision, and profile
functionality is preserved. qcli now wraps qdocs as a thin compatibility shim.

---

## Migration from qcli.qdocs

qdocs was previously embedded in `qcli.qdocs.*`. It is now available as the
standalone `qdocs` package:

```python
# Before
from qcli.qdocs.converters import convert_md_to_pdf

# After
from qdocs.converters import convert_md_to_pdf
```

The `qcli.qdocs` namespace continues to work via re-exports from the `qdocs`
package — no immediate code changes required for qcli consumers.
