"""Mermaid diagram generation with per-block DuckDB cache.

Uses mmdc (mermaid-cli) to render .mmd blocks embedded in markdown files.
Caches each block by its content hash — only re-renders when the block changes.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from loguru import logger

from qdocs.converters.utils import get_text_hash
from qdocs.exceptions import ConversionError, MmcdNotFoundError


def check_mmdc() -> None:
    """Verify mmdc is available in PATH.

    Raises:
        MmcdNotFoundError: If not found.
    """
    if shutil.which("mmdc") is None:
        raise MmcdNotFoundError


def extract_mermaid_blocks(md_file: Path) -> list[tuple[str, int]]:
    """Extract all ```mermaid blocks from a markdown file.

    Returns:
        List of (mermaid_code, line_number) tuples, 1-indexed.
    """
    content = md_file.read_text(encoding="utf-8")
    blocks: list[tuple[str, int]] = []
    pattern = r"```mermaid\s*\n(.*?)\n```"
    for match in re.finditer(pattern, content, re.DOTALL):
        code = match.group(1).strip()
        line_num = content[: match.start()].count("\n") + 1
        blocks.append((code, line_num))
    return blocks


def _puppeteer_env() -> dict:
    """Build environment with Puppeteer Chrome path if not already set."""
    env = os.environ.copy()
    if "PUPPETEER_EXECUTABLE_PATH" in env:
        return env
    chrome_base = Path.home() / ".cache" / "puppeteer" / "chrome-headless-shell"
    if chrome_base.exists():
        version_dirs = sorted(
            (d for d in chrome_base.iterdir() if d.is_dir()), reverse=True
        )
        for vdir in version_dirs:
            binaries = list(vdir.rglob("chrome-headless-shell"))
            if binaries:
                env["PUPPETEER_EXECUTABLE_PATH"] = str(binaries[0])
                logger.debug(f"Puppeteer Chrome: {binaries[0]}")
                break
    return env


def _is_dark_background(background: str) -> bool:
    """Return True when background is a dark hex colour (luminance < 0.4)."""
    bg = background.strip().lstrip("#")
    if bg.lower() in ("transparent", "white"):
        return False
    if len(bg) == 6:
        r, g, b = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
        return (r * 0.299 + g * 0.587 + b * 0.114) < 100
    return False


def generate_diagram(
    mermaid_code: str,
    output_path: Path,
    output_format: str = "svg",
    width: int = 1920,
    background: str = "transparent",
) -> None:
    """Render a single Mermaid block to an SVG or PNG file.

    When *background* is a dark colour, a Mermaid config file is written and
    passed via ``-c`` so mmdc uses the ``dark`` theme — ensuring white/high-
    contrast text on dark backgrounds rather than the default gray labels.

    Raises:
        ConversionError: If mmdc exits non-zero.
    """
    check_mmdc()
    temp_input = output_path.parent / f".{output_path.stem}_tmp.mmd"
    temp_config: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_input.write_text(mermaid_code, encoding="utf-8")
        cmd = [
            "mmdc",
            "-i",
            str(temp_input),
            "-o",
            str(output_path),
            "-w",
            str(width),
            "-b",
            background,
        ]
        if _is_dark_background(background):
            temp_config = output_path.parent / f".{output_path.stem}_mmdc_cfg.json"
            temp_config.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "themeVariables": {
                            "edgeLabelBackground": "transparent",
                            "labelTextColor": "#ffffff",
                            # stateDiagram-v2: override ccc/e0dfdf defaults to white
                            "labelColor": "#ffffff",
                            "textColor": "#ffffff",
                            "stateLabelColor": "#ffffff",
                            "nodeTextColor": "#ffffff",
                        },
                    }
                ),
                encoding="utf-8",
            )
            cmd.extend(["-c", str(temp_config)])
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=_puppeteer_env()
        )
        if result.returncode != 0:
            err = result.stderr or result.stdout or "unknown error"
            msg = f"mmdc failed (exit {result.returncode}): {err}"
            raise ConversionError(msg)
        # Note: mmdc writes the -b value directly into the root <svg> style
        # attribute (as rgb() notation), so no post-processing is needed.
        logger.debug(f"Rendered diagram: {output_path.name}")
    finally:
        if temp_input.exists():
            temp_input.unlink()
        if temp_config is not None and temp_config.exists():
            temp_config.unlink()


def generate_diagrams(
    source: Path,
    output_dir: Path,
    output_format: str = "svg",
    width: int = 1920,
    background: str = "transparent",
    skip_if_cached: bool = False,
) -> list[Path]:
    """Render all Mermaid blocks in a markdown file.

    Uses count-based cache check when skip_if_cached=True (legacy compat).
    Prefer generate_diagrams_cached() for per-block hash cache.

    Returns:
        List of generated (or cached) output file paths.
    """
    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)

    blocks = extract_mermaid_blocks(source)
    if not blocks:
        logger.debug(f"No Mermaid blocks in {source.name}")
        return []

    if skip_if_cached and _count_cached(source, output_dir, output_format) == len(
        blocks
    ):
        cached = sorted(output_dir.glob(f"{source.stem}_diagram_*.{output_format}"))
        logger.debug(f"Using {len(cached)} cached diagrams for {source.name}")
        return cached

    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    for idx, (code, line_num) in enumerate(blocks, 1):
        out = output_dir / f"{source.stem}_diagram_{idx}.{output_format}"
        try:
            generate_diagram(
                code,
                out,
                output_format=output_format,
                width=width,
                background=background,
            )
            generated.append(out)
            logger.info(f"  [{idx}/{len(blocks)}] {out.name} (line {line_num})")
        except ConversionError as exc:
            logger.warning(f"  Skipped diagram {idx} at line {line_num}: {exc}")
    return generated


def generate_diagrams_cached(
    source: Path,
    output_dir: Path,
    cache_dir: Path,
    output_format: str = "svg",
    width: int = 1920,
    background: str = "transparent",
    force: bool = False,
    png_width: int = 3840,
) -> list[Path]:
    """Render Mermaid blocks with per-block DuckDB content-hash cache.

    Each block is only re-rendered when its text content changes.
    PNG sidecars are written to ``output_dir/png/`` at ``png_width`` resolution
    (default 3840 — 4 K) alongside every SVG, for archival / sharing purposes.
    They are not referenced by the PDF/DOCX pipeline.

    Returns:
        List of rendered output file paths (SVG/PNG as specified by output_format).
    """
    from qdocs.cache import QDocsDB

    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)

    blocks = extract_mermaid_blocks(source)
    if not blocks:
        return []

    source_key = str(source.resolve())
    output_dir.mkdir(parents=True, exist_ok=True)
    png_dir = output_dir / "png"
    results: list[Path] = []

    if force:
        # Delete any pre-existing rendered files so mmdc always writes fresh.
        for stale in output_dir.glob(f"{source.stem}_diagram_*.{output_format}"):
            stale.unlink(missing_ok=True)
        # Also clean PNG sidecars.
        if png_dir.exists():
            for stale_png in png_dir.glob(f"{source.stem}_diagram_*.png"):
                stale_png.unlink(missing_ok=True)

    db = QDocsDB(cache_dir)

    for idx, (code, line_num) in enumerate(blocks, 1):
        block_hash = get_text_hash(code)
        out = output_dir / f"{source.stem}_diagram_{idx}.{output_format}"
        png_out = png_dir / f"{source.stem}_diagram_{idx}.png"

        svg_cached = (
            not force
            and db.is_diagram_cached(source_key, idx, block_hash)
            and out.exists()
        )

        if svg_cached:
            logger.debug(f"Diagram cache hit: {out.name}")
            results.append(out)
        else:
            try:
                generate_diagram(
                    code,
                    out,
                    output_format=output_format,
                    width=width,
                    background=background,
                )
                db.upsert_diagram(source_key, idx, block_hash, str(out))
                results.append(out)
                logger.info(
                    f"  Rendered [{idx}/{len(blocks)}] {out.name} (line {line_num})"
                )
            except ConversionError as exc:
                logger.warning(f"  Skipped diagram {idx} at line {line_num}: {exc}")
                continue

        # PNG sidecar — render when missing or force.
        if not force and png_out.exists():
            logger.debug(f"PNG sidecar cache hit: {png_out.name}")
            continue
        try:
            png_dir.mkdir(parents=True, exist_ok=True)
            generate_diagram(
                code,
                png_out,
                output_format="png",
                width=png_width,
                background=background,
            )
            logger.debug(f"  PNG sidecar: {png_out.name} ({png_width}px)")
        except ConversionError as exc:
            logger.warning(f"  PNG sidecar skipped for diagram {idx}: {exc}")

    return results


def export_mermaid_blocks(source: Path, output_dir: Path) -> list[Path]:
    """Export all Mermaid blocks as raw .mmd text files."""
    if not source.exists():
        msg = f"Source not found: {source}"
        raise ConversionError(msg)

    blocks = extract_mermaid_blocks(source)
    if not blocks:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []
    for idx, (code, line_num) in enumerate(blocks, 1):
        out = output_dir / f"{source.stem}_diagram_{idx}.mmd"
        out.write_text(code, encoding="utf-8")
        exported.append(out)
        logger.info(f"  Exported: {out.name} (line {line_num})")
    return exported


def _count_cached(source: Path, output_dir: Path, fmt: str) -> int:
    if not output_dir.exists():
        return 0
    return len(list(output_dir.glob(f"{source.stem}_diagram_*.{fmt}")))
