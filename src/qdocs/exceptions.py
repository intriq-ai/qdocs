"""Typed exception hierarchy for qdocs."""


class QdocsError(Exception):
    """Base exception for all qdocs errors."""


class ConfigError(QdocsError):
    """Invalid or missing configuration."""


class ProfileError(QdocsError):
    """Profile not found or invalid."""


class ConversionError(QdocsError):
    """Document conversion failed."""


class SourceNotFoundError(QdocsError):
    """Source file or directory does not exist."""


class CacheError(QdocsError):
    """Cache read/write error."""


class RevisionError(QdocsError):
    """Revision management error."""


class MmcdNotFoundError(QdocsError):
    """mermaid-cli (mmdc) not found in PATH.

    Install via: npm install -g @mermaid-js/mermaid-cli
    """

    def __str__(self) -> str:
        return (
            "mermaid-cli (mmdc) not found in PATH.\n"
            "Install with: npm install -g @mermaid-js/mermaid-cli\n"
            "Then verify with: mmdc --version"
        )
