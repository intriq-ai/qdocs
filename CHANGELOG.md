# Changelog

All notable changes to qdocs are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
Versioning: [Semantic Versioning](https://semver.org/)

---

## [0.1.0] — 2026-07-10

### Added

- Initial release extracted from qcli qdocs module
- Multi-format document conversion (MD↔PDF, MD↔DOCX, MD→XLSX, PDF↔MD, DOCX↔MD, XLSX↔MD, PPTX↔MD)
- Branded cover pages with configurable profiles
- Mermaid diagram rendering, export, and checking
- Revision management with DuckDB persistence
- Content-addressed cache for Textract and AI responses
- Book assembly with table of contents
- Chapter export (per-subdirectory book files)
- AWS Textract PDF→MD with AI formatting via Bedrock
- Rich CLI via clypi command tree
