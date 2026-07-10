from pathlib import Path

from qdocs.converters.md_table_to_xlsx import convert_md_tables_to_xlsx


def test_convert_md_tables_to_xlsx(tmp_path: Path) -> None:
    src = tmp_path / "tables.md"
    dst = tmp_path / "tables.xlsx"
    src.write_text(
        "# Title\n\n## Revenue\n| Month | Value |\n| --- | --- |\n| Jan | 100 |\n",
        encoding="utf-8",
    )

    convert_md_tables_to_xlsx(src, dst)

    assert dst.exists()
    assert dst.stat().st_size > 0


def test_convert_md_tables_single_sheet(tmp_path: Path) -> None:
    src = tmp_path / "tables-single.md"
    dst = tmp_path / "tables-single.xlsx"
    src.write_text(
        "## One\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n## Two\n| C | D |\n| --- | --- |\n| 3 | 4 |\n",
        encoding="utf-8",
    )

    convert_md_tables_to_xlsx(src, dst, one_table_per_sheet=False)

    assert dst.exists()
    assert dst.stat().st_size > 0
