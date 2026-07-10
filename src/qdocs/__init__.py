"""qdocs — General-purpose document conversion, export, and revision management."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("qdocs")
except PackageNotFoundError:
    __version__ = "0.0.0"
__all__ = ["__version__"]
