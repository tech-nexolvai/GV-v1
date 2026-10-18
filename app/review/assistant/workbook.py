"""A plain workbook writer: every cell text, a bold frozen header and a filter on each sheet.

Shared by the job tracker's export (#1591) and the full check's Not-compared list (#1619). Every
cell is text (`@`), so a `1/8` is never turned into a date or a decimal. openpyxl is loaded by name
(`ModuleNotFoundError` when it is not installed: the caller then writes CSV instead).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from importlib import import_module

__all__ = ["Sheet", "write_xlsx"]

#: One sheet: its title, its header and its rows (each cell written as text).
type Sheet = tuple[str, Sequence[str], Sequence[Sequence[object]]]

_WIDEST: int = 60


def write_xlsx(sheets: Sequence[Sheet]) -> bytes:
    """The workbook of `sheets`, in order, as bytes."""
    openpyxl = import_module("openpyxl")
    styles = import_module("openpyxl.styles")
    utils = import_module("openpyxl.utils")
    book = openpyxl.Workbook()
    for at, (title, header, rows) in enumerate(sheets):
        sheet = book.active if at == 0 else book.create_sheet()
        sheet.title = title
        sheet.append(list(header))
        for row in rows:
            sheet.append(["" if value is None else str(value) for value in row])
        for cell in sheet[1]:
            cell.font = styles.Font(bold=True)
        widths = [len(str(h)) for h in header]
        for line in sheet.iter_rows(min_row=1):
            for column, cell in enumerate(line):
                cell.number_format = "@"
                cell.alignment = styles.Alignment(vertical="top", wrap_text=False)
                widths[column] = max(widths[column], min(_WIDEST, len(str(cell.value or ""))))
        for column, width in enumerate(widths, start=1):
            sheet.column_dimensions[utils.get_column_letter(column)].width = width + 2
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{utils.get_column_letter(len(header))}{max(1, len(rows) + 1)}"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
