# qdocs

General-purpose document conversion, export, and revision management.

```
Markdown / DOCX / PDF / XLSX / PPTX
          │
          ▼
   [convert]  ─── to-pdf | to-docx | to-md | to-xlsx
          │
          ▼
   PDF / DOCX / MD / XLSX  ─── branded cover page, revision tracking
```

---

## Features

- **Multi-format conversion**: MD→PDF, MD→DOCX, MD→XLSX, DOCX→MD, PDF→MD, XLSX→MD, PPTX→MD, DOCX→PDF, PDF→DOCX
- **Branded cover pages**: configurable profiles with logo, classification, version, author
- **Mermaid diagram rendering**: generate/check/export diagrams from markdown code blocks
- **Revision management**: per-document auto-incrementing revision history in DuckDB
- **Content-addressed cache**: skip re-conversion when source is unchanged
- **Book assembly**: combine multiple markdown files into a single PDF/DOCX with table of contents
- **Chapter export**: export each subdirectory as a separate chapter book
- **Textract PDF→MD**: AWS Textract OCR with optional AI formatting via Bedrock
- **Rich CLI**: clypi-powered command tree with progress bars and colour output

---

## Install

```bash
uv sync
```

## Usage

```bash
qdocs convert to-pdf --source doc.md
qdocs convert to-docx --source doc.md --classification CONFIDENTIAL
qdocs convert to-md --source scans/report.pdf --provider textract
qdocs export --source-dir ./docs --fmt pdf --book
qdocs diagrams generate --source doc.md
qdocs revisions show --source doc.md
qdocs profile init
```
