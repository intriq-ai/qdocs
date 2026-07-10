from pathlib import Path

from qdocs.converters.csv_to_xlsx import convert_csv_to_xlsx
from qdocs.converters.validators import validate_csv, validate_md_tables, validate_xlsx


def test_validate_csv_ok(tmp_path: Path) -> None:
    src = tmp_path / "sample.csv"
    src.write_text("name,amount\nalpha,10\nbeta,20\n", encoding="utf-8")

    result = validate_csv(src)

    assert result.is_valid is True
    assert result.errors == []


def test_validate_md_missing_separator_invalid(tmp_path: Path) -> None:
    src = tmp_path / "table.md"
    src.write_text("| A | B |\n| 1 | 2 |\n", encoding="utf-8")

    result = validate_md_tables(src)

    assert result.is_valid is False
    assert any("missing separator" in issue.message.lower() for issue in result.errors)


def test_validate_xlsx_ok(tmp_path: Path) -> None:
    csv_src = tmp_path / "sample.csv"
    xlsx_src = tmp_path / "sample.xlsx"
    csv_src.write_text("name,amount\nalpha,100\n", encoding="utf-8")
    convert_csv_to_xlsx(csv_src, xlsx_src)

    result = validate_xlsx(xlsx_src)

    assert result.is_valid is True
    assert result.errors == []
