from pathlib import Path

import openpyxl

from qdocs.converters.md_to_xlsx import XlsxFormatConfig, convert_md_to_xlsx


def test_md_to_xlsx_applies_advanced_features(tmp_path: Path) -> None:
    src = tmp_path / "advanced.md"
    dst = tmp_path / "advanced.xlsx"
    src.write_text(
        "# Report\n\n## Data\n| Name | Amount |\n| --- | --- |\n| A | 10 |\n| B | -5 |\n",
        encoding="utf-8",
    )

    fmt = XlsxFormatConfig(
        merged_ranges=["A6:B6"],
        conditional_rules=[
            {
                "type": "cell_is",
                "range": "B3:B100",
                "operator": "lessThan",
                "formula": ["0"],
                "fill": "FCE4D6",
            }
        ],
        data_validations=[
            {
                "type": "whole",
                "range": "B3:B100",
                "operator": "greaterThanOrEqual",
                "formula1": "-1000000",
                "allow_blank": True,
            }
        ],
    )

    convert_md_to_xlsx(src, dst, fmt=fmt, sheet_title=True)

    wb = openpyxl.load_workbook(dst)
    ws = wb["Data"]
    merged = [str(rng) for rng in ws.merged_cells.ranges]
    assert "A6:B6" in merged
    assert len(ws.data_validations.dataValidation) >= 1
    wb.close()
