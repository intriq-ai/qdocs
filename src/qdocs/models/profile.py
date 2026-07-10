"""Profile models — cover page, company identity, document classification."""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DocumentClassification(StrEnum):
    """Document classification levels (shown on cover page)."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"
    TOP_SECRET = "TOP SECRET"  # pragma: allowlist secret

    @property
    def color_hex(self) -> str:
        """ReportLab-compatible color hex for the classification label."""
        return {
            DocumentClassification.PUBLIC: "#006400",  # dark green
            DocumentClassification.INTERNAL: "#1a1a8c",  # dark blue
            DocumentClassification.CONFIDENTIAL: "#cc0000",  # red
            DocumentClassification.RESTRICTED: "#8B4500",  # dark orange
            DocumentClassification.TOP_SECRET: "#4B0000",  # dark maroon
        }[self]


class ProfileConfig(BaseModel):
    """Company / branding profile loaded from ~/.intriq/profiles/{name}/profile.toml."""

    model_config = ConfigDict(frozen=True)

    name: str = "default"
    company_name: str = "Intriq AI"
    logo: str = "logo.png"
    copyright: str = "© 2026 Intriq AI Ltd. All rights reserved."
    address: str = "20 Wenlock Road, London, England, N1 7GU"
    website: str = "https://intriq.ai"
    code_theme: str = "github-dark"
    # Cover page defaults — can be overridden by CLI flags
    default_author: str | None = None
    default_author_email: str | None = None
    default_classification: DocumentClassification = DocumentClassification.INTERNAL
    # Resolved by ProfileManager — not stored in profile.toml
    logo_path: Path | None = None
    badge_paths: list[Path] = Field(default_factory=list)


class CoverPageConfig(BaseModel):
    """Configuration for document cover pages."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    show_version: bool = True
    version_str: str | None = None
    show_date: bool = True
    show_revision: bool = True
    classification: DocumentClassification = DocumentClassification.CONFIDENTIAL
    author: str | None = None
    author_email: str | None = None
    chapter: str | None = None
    profile: ProfileConfig = Field(default_factory=ProfileConfig)

    @classmethod
    def disabled(cls) -> CoverPageConfig:
        """Return a cover page config with cover disabled."""
        return cls(enabled=False)

    @classmethod
    def default(cls) -> CoverPageConfig:
        """Return a cover page config using all defaults."""
        return cls()
