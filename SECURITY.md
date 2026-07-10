# Security Audit — qdocs

**Audited:** 2026-07-10 | **Version:** 0.1.0

## Findings & Fixes

### No issues found

Initial release extracted from qcli. No security findings at this time.

---

## Dependency Notes

- `playwright` is used for PDF rendering (HTML→PDF via Chromium). Keep updated.
- `python-docx` processes DOCX files — no known CVEs in current version range.
- `reportlab` generates PDF cover pages — no known CVEs in current version range.
