"""Profile manager — load, validate, and initialise qdocs profiles.

Profiles live at ~/.intriq/profiles/{name}/ and contain:
  profile.toml  — company identity and cover page defaults
  logo.png      — branding logo referenced in profile.toml
"""

import shutil
from pathlib import Path

import toml
from loguru import logger

from qdocs.exceptions import ProfileError
from qdocs.models.profile import (
    CoverPageConfig,
    DocumentClassification,
    ProfileConfig,
)

_DEFAULT_PROFILE_TOML = """\
[company]
name = "Intriq AI"
logo = "logo.png"
copyright = "© 2026 Intriq AI Ltd. All rights reserved."
address = "20 Wenlock Road, London, England, N1 7GU"
website = "https://intriq.ai"

[cover]
author = "Christopher Ward"
classification = "INTERNAL"
show_version = true
show_date = true
show_revision = true
"""

# Well-known locations for the Intriq logo within the qplatform monorepo tree
_ISMS_LOGO_CANDIDATES = [
    Path(__file__).parents[5] / "compliance" / "isms" / "public" / "intriq-logo.png",
    Path(__file__).parents[4] / "compliance" / "isms" / "public" / "intriq-logo.png",
]


class ProfileManager:
    """Manages qdocs profiles stored under ~/.intriq/profiles/."""

    def __init__(self, profiles_base: Path) -> None:
        self.profiles_base = profiles_base.expanduser().resolve()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, name: str = "default") -> ProfileConfig:
        """Load and return a ProfileConfig for the named profile.

        If the profile directory does not exist yet, initialise it first.

        Raises:
            ProfileError: If profile.toml is malformed.
        """
        profile_dir = self.profiles_base / name
        if not profile_dir.exists():
            logger.warning(f"Profile '{name}' not found — initialising defaults.")
            self.init(name)

        toml_path = profile_dir / "profile.toml"
        if not toml_path.exists():
            msg = f"profile.toml missing in profile '{name}' at {toml_path}"
            raise ProfileError(msg)

        try:
            raw = toml.load(toml_path)
        except Exception as exc:
            msg = f"Failed to parse profile.toml for '{name}': {exc}"
            raise ProfileError(msg) from exc

        company = raw.get("company", {})
        logo_filename: str = company.get("logo", "logo.png")
        logo_path = profile_dir / logo_filename
        resolved_logo = logo_path if logo_path.exists() else None

        if resolved_logo is None:
            logger.warning(f"Logo '{logo_filename}' not found in profile '{name}'.")

        # Resolve badge paths — listed under [badges] filenames = [...]
        badges_cfg = raw.get("badges", {})
        badge_filenames: list[str] = badges_cfg.get("filenames", [])
        badges_base_str: str = badges_cfg.get("base_dir", "")
        badges_base = Path(badges_base_str).expanduser() if badges_base_str else profile_dir
        resolved_badges: list[Path] = []
        for fname in badge_filenames:
            bp = badges_base / fname
            if bp.exists():
                resolved_badges.append(bp)
            else:
                logger.warning(f"Badge not found, skipping: {bp}")

        cover_cfg = raw.get("cover", {})
        raw_cls = cover_cfg.get("classification", "INTERNAL").upper()
        try:
            default_cls = DocumentClassification(raw_cls)
        except ValueError:
            default_cls = DocumentClassification.INTERNAL

        return ProfileConfig(
            name=name,
            company_name=company.get("name", "Intriq AI"),
            logo=logo_filename,
            copyright=company.get("copyright", ""),
            address=company.get("address", ""),
            website=company.get("website", ""),
            code_theme=company.get("code_theme", "github-dark"),
            default_author=cover_cfg.get("author"),
            default_author_email=cover_cfg.get("author_email"),
            default_classification=default_cls,
            logo_path=resolved_logo,
            badge_paths=resolved_badges,
        )

    def build_cover_config(
        self,
        profile_name: str = "default",
        *,
        enabled: bool = True,
        version_str: str | None = None,
        show_version: bool = True,
        show_date: bool = True,
        show_revision: bool = True,
        classification: DocumentClassification | None = None,
        author: str | None = None,
        chapter: str | None = None,
        code_theme: str | None = None,
    ) -> CoverPageConfig:
        """Load profile and construct a CoverPageConfig from CLI / settings values.

        ``classification`` and ``author`` fall back to the profile's defaults
        when not provided by the caller (i.e. not supplied via CLI flags).
        """
        profile = self.load(profile_name)
        # CLI --theme overrides the profile's code_theme
        resolved_profile = (
            profile
            if code_theme is None
            else profile.model_copy(update={"code_theme": code_theme})
        )
        resolved_classification = (
            classification
            if classification is not None
            else profile.default_classification
        )
        resolved_author = author if author is not None else profile.default_author
        resolved_author_email = profile.default_author_email
        return CoverPageConfig(
            enabled=enabled,
            version_str=version_str,
            show_version=show_version and version_str is not None,
            show_date=show_date,
            show_revision=show_revision,
            classification=resolved_classification,
            author=resolved_author,
            author_email=resolved_author_email,
            chapter=chapter,
            profile=resolved_profile,
        )

    def init(self, name: str = "default", logo_source: Path | None = None) -> Path:
        """Scaffold a profile directory and return its path.

        Auto-discovers the Intriq logo from the qplatform monorepo if no
        explicit logo_source is provided.

        Args:
            name: Profile name.
            logo_source: Optional path to a logo file to copy in.

        Returns:
            Path to the created profile directory.
        """
        profile_dir = self.profiles_base / name
        profile_dir.mkdir(parents=True, exist_ok=True)

        toml_path = profile_dir / "profile.toml"
        if not toml_path.exists():
            toml_path.write_text(_DEFAULT_PROFILE_TOML, encoding="utf-8")
            logger.info(f"Created profile.toml: {toml_path}")

        # Resolve logo
        logo_dest = profile_dir / "logo.png"
        if not logo_dest.exists():
            logo_src = logo_source or self._find_intriq_logo()
            if logo_src and logo_src.exists():
                shutil.copy2(logo_src, logo_dest)
                logger.info(f"Copied logo: {logo_src} → {logo_dest}")
            else:
                logger.warning(
                    f"No logo found for profile '{name}'. "
                    "Run: qdocs profile set-logo <path> to add one."
                )

        return profile_dir

    def set_logo(self, logo_source: Path, name: str = "default") -> Path:
        """Copy a logo file into the named profile directory.

        Returns:
            Destination path.
        """
        profile_dir = self.profiles_base / name
        profile_dir.mkdir(parents=True, exist_ok=True)
        dest = profile_dir / "logo.png"
        shutil.copy2(logo_source, dest)
        logger.info(f"Set logo for profile '{name}': {dest}")
        return dest

    def list_profiles(self) -> list[str]:
        """Return names of all installed profiles."""
        if not self.profiles_base.exists():
            return []
        return sorted(
            d.name
            for d in self.profiles_base.iterdir()
            if d.is_dir() and (d / "profile.toml").exists()
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_intriq_logo(self) -> Path | None:
        """Search well-known paths for the bundled Intriq logo."""
        for candidate in _ISMS_LOGO_CANDIDATES:
            if candidate.exists():
                return candidate
        return None
