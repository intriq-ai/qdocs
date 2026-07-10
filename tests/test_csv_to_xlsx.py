from pathlib import Path

from qdocs.converters.csv_to_xlsx import convert_csv_to_xlsx


def test_convert_csv_to_xlsx_creates_workbook(tmp_path: Path) -> None:
    src = tmp_path / "input.csv"
    dst = tmp_path / "output.xlsx"
    src.write_text("name,amount,date\nalpha,12,2026-01-01\n", encoding="utf-8")

    convert_csv_to_xlsx(src, dst)

    assert dst.exists()
    assert dst.stat().st_size > 0
