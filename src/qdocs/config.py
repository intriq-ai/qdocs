"""qdocs configuration management.

Settings are loaded from (in priority order):
1. CLI arguments
2. Environment variables (QDOCS_ prefix)
3. config/.qdocs.toml (searched upward from CWD)
4. Default values
"""

from pathlib import Path
from typing import Literal

import toml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class QdocsSettings(BaseSettings):
    """qdocs runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="QDOCS_",
        case_sensitive=False,
        extra="ignore",
    )

    # Paths
    source_dir: Path = Field(default=Path(), description="Default source directory")
    output_dir: Path = Field(
        default=Path.home() / ".intriq" / "exports" / "qdocs",
        description="Default output directory",
    )

    # Defaults
    format: Literal["pdf", "docx", "md", "all"] = Field(
        default="pdf", description="Default format"
    )
    pattern: str = Field(default="*.md", description="Glob pattern for batch mode")
    recursive: bool = Field(default=True, description="Recurse into subdirectories")

    # Conversion
    force: bool = Field(default=False, description="Force re-conversion ignoring cache")

    # Diagrams
    diagram_format: Literal["png", "svg"] = Field(
        default="svg", description="Mermaid output format"
    )
    diagram_width: int = Field(default=1920, description="Mermaid render width in px")
    diagram_background: str = Field(
        default="#0d1117", description="Mermaid background color"
    )
    diagram_cache: bool = Field(default=True, description="Cache rendered diagrams")

    # Cache
    cache_path: Path = Field(
        default=Path.home() / ".intriq" / "cache" / "qdocs",
        description="DuckDB cache directory",
    )

    # Profile
    profile_name: str = Field(default="default", description="Active profile name")
    profile_path: Path = Field(
        default=Path.home() / ".intriq" / "profiles",
        description="Profiles base directory",
    )

    @classmethod
    def find_config(cls, start_dir: Path | None = None) -> Path | None:
        """Walk up from start_dir searching for config/.qdocs.toml."""
        current = (start_dir or Path.cwd()).resolve()
        while current != current.parent:
            candidate = current / "config" / ".qdocs.toml"
            if candidate.exists():
                return candidate
            if current == Path.home():
                break
            current = current.parent
        # Fallback: package-adjacent config/
        pkg_config = Path(__file__).parent.parent / "config" / ".qdocs.toml"
        return pkg_config if pkg_config.exists() else None

    @classmethod
    def load(cls, config_path: Path | None = None) -> QdocsSettings:
        """Load settings from config file, falling back to defaults."""
        from qdocs.exceptions import ConfigError

        if config_path is None:
            config_path = cls.find_config()
        if config_path is None:
            return cls()

        try:
            raw = toml.load(config_path)
        except Exception as exc:
            msg = f"Failed to parse config: {config_path}: {exc}"
            raise ConfigError(msg) from exc

        base_dir = config_path.parent.parent
        overrides: dict = {}

        if paths := raw.get("paths"):
            for key in ("source_dir", "output_dir"):
                if val := paths.get(key):
                    p = Path(val).expanduser()
                    overrides[key] = p if p.is_absolute() else (base_dir / p).resolve()

        if defaults := raw.get("defaults"):
            for key in ("format", "pattern", "recursive"):
                if (val := defaults.get(key)) is not None:
                    overrides[key] = val

        if (conv := raw.get("conversion")) and (val := conv.get("force")) is not None:
            overrides["force"] = val

        if diag := raw.get("diagrams"):
            mapping = {
                "output_format": "diagram_format",
                "width": "diagram_width",
                "background": "diagram_background",
                "cache": "diagram_cache",
            }
            for toml_key, field in mapping.items():
                if (val := diag.get(toml_key)) is not None:
                    overrides[field] = val

        if (cache_cfg := raw.get("cache")) and (val := cache_cfg.get("path")):
            overrides["cache_path"] = Path(val).expanduser()

        if profile := raw.get("profile"):
            if val := profile.get("name"):
                overrides["profile_name"] = val
            if val := profile.get("path"):
                overrides["profile_path"] = Path(val).expanduser()

        return cls(**overrides)
