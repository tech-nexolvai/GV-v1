"""Values entered for the job, and GV defaults used, in the signed outputs (#1181).

Verification for the "entered" and "GV defaults used" sections of the findings PDF
(`reports/findings_pdf.py`), the workbook (`reports/spreadsheet.py`) and the redline
(`reports/redline.py`). The internal outputs say what, where from, who and when, in plain words; the
redline goes to the vendor, so it says only the setting's plain name and value — never who typed it
or a note. Every value is synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

from openpyxl import load_workbook

from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
from reports.redline import ReportMode, render_redline
from reports.spreadsheet import PROJECT_VALUES_SHEET, write_stored_workbook
from storage.local import LocalStore
from tests.reports.test_countertop_summary_reports import _page_texts, _stored
from tests.reports.test_redline import _finding, _package, _reference, _text
from workflow.changed_values import ChangedValues, UsedValue

WHEN = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
SINK = UsedValue(
    name="sink_interior_width",
    text='30"',
    where="this job",
    source="G.C / Client: Sink Synthetic: private reviewer note",
    set_by="Synthetic Reviewer",
    set_at=WHEN,
)
DEFAULT = UsedValue(
    name="cabinet_depth",
    text='22"',
    where="GV default",
    source="Company standard",
    set_by="Synthetic Admin",
    set_at=WHEN,
)
CHANGED = ChangedValues("available", None, (), (), (SINK,), (DEFAULT,))


def test_a_used_value_reads_in_plain_words_with_who_and_when() -> None:
    assert SINK.line() == (
        'Sink inside width: 30" (this job; G.C / Client: Sink Synthetic: private reviewer note; '
        "set by Synthetic Reviewer on 12 Oct 2026)"
    )
    assert SINK.plain() == 'Sink inside width: 30"'
    assert "sink_interior_width" not in SINK.line()


def test_the_findings_pdf_lists_both_sections() -> None:
    text = " ".join(
        _page_texts(
            write_findings_pdf(
                FindingsPdfInput(
                    package_revision_id=UUID(int=14),
                    revision_number=1,
                    vendor=None,
                    findings=(_stored(),),
                    changed_values=CHANGED,
                )
            )
        )
    )

    assert "VALUES ENTERED FOR THIS JOB" in text
    assert 'Sink inside width: 30"' in text
    assert "GV DEFAULTS USED" in text
    assert 'Cabinet depth: 22" (GV default' in text


def test_the_workbook_lists_both_sections() -> None:
    book = load_workbook(BytesIO(write_stored_workbook((_stored(),), changed_values=CHANGED)))
    rows = [tuple(row) for row in book[PROJECT_VALUES_SHEET].iter_rows(values_only=True)]

    assert ("Value entered for this job", SINK.line()) in rows
    assert ("GV default used", DEFAULT.line()) in rows


def test_the_vendors_redline_has_the_value_and_never_the_person_or_the_note(
    tmp_path: Path,
) -> None:
    artifact = render_redline(
        _package(),
        [_finding(refs=(_reference(),))],
        ReportMode.INTERNAL,
        LocalStore(tmp_path),
        changed_values=CHANGED,
    )

    text = _text(tmp_path, artifact.key)
    assert 'Sink inside width: 30"' in text
    assert 'Cabinet depth: 22"' in text
    assert "Synthetic Reviewer" not in text
    assert "private reviewer note" not in text
    assert "Synthetic Admin" not in text
