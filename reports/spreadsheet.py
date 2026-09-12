"""The findings as a workbook, with `1/8` still reading `1/8`.

The client's own workflow is spreadsheet-shaped — the checklist that started this project is an
`.xlsx` — so this is the format in which the output gets used rather than re-keyed. That is the
entire reason it exists, and it sets the one hard constraint.

**Every measurement is a text cell.** A spreadsheet is the most eager type-coercer in the toolchain:
give Excel `1/8` in a numeric cell and it stores `0.125`, then shows whatever the column's format
says. The distinction between an eighth of an inch and a decimal approximation of one is what the
whole units layer exists to preserve (ADR-0001), and losing it in the final step would make the
export the least trustworthy artifact the system produces — while looking the most familiar.

So values are written as strings, each cell's number format is pinned to text, and
`tests/test_spreadsheet.py` reads the file back and asserts the stored type. Writing a string is not
enough on its own: openpyxl will happily give a string cell a numeric format, and a consumer that
re-saves the file can then have it converted for them.

**Abstentions are rows.** A `NOT_FOUND` check has no numbers to put in the value columns, and the
tempting thing is to leave the row out. That reproduces exactly the failure `NO_APPLICABLE_RULE` was
invented to stop: a reader scanning for problems sees nothing and reads it as nothing wrong. Every
finding gets a row, and the value columns say what is missing and why.

An abstention is not always empty, and the value columns follow **the calculation, not the outcome**.
A `REVIEW_REQUIRED` raised partway through arithmetic carries a trace, and its comparison and
operands are written out: an abstention raised because two readings disagreed is one whose readings
are the most useful thing in the file.

**Two sheets, because a finding and an operand are different rows.** One row per finding answers
"what did this check conclude?"; one row per traced operand answers "which numbers did it use, and
where did each come from?". Flattening both into one sheet means either repeating the finding on
every operand or dropping operands after the first, and the second is the kind of quiet truncation
that makes a report wrong without looking wrong.

**No expected/observed columns.** They would have to be invented. A `CalculationTrace` records its
operands by name and its `comparison` as text; nothing in it labels one side expected and the other
observed, and guessing from operand order would mislabel every rule whose operands are declared the
other way round. The comparison string is written verbatim instead, and the operand sheet carries
each named value — so a reader can see what was compared without this module asserting which was
which. There is likewise no `item` column: nothing in a finding identifies an item yet, and item
identifiers are B7.3 (#166).

Source: `AGENTS.md` §2.8; ADR-0001 · Design: `docs/DESIGN_PRODUCT.md` §3.3 ·
Verification: ``tests/reports/test_spreadsheet.py``
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from typing import Final

# openpyxl ships no type stubs, the same situation as reportlab in `reports/redline.py`. Ignored
# per-import rather than repo-wide, so a genuinely untyped import somewhere else still surfaces.
from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.cell.cell import Cell  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

from units.measurement import Measurement
from verdict.finding import Finding
from verdict.outcomes import Outcome, is_abstention

__all__ = [
    "FINDINGS_SHEET",
    "FINDING_COLUMNS",
    "NOT_RECORDED",
    "OPERANDS_SHEET",
    "OPERAND_COLUMNS",
    "SUMMARY_SHEET",
    "TEXT_FORMAT",
    "UNREADABLE_REFERENCE",
    "StoredFinding",
    "WorkbookSignoff",
    "decode_reference",
    "exact_text",
    "write_stored_workbook",
    "write_value",
    "write_workbook",
]

#: openpyxl's number format for "leave this alone". Pinned on every value cell.
TEXT_FORMAT: Final = "@"

FINDINGS_SHEET: Final = "Findings"
OPERANDS_SHEET: Final = "Operands"
SUMMARY_SHEET: Final = "Review Summary"

_BLACK: Final = "000000"
_WHITE: Final = "FFFFFF"
_LIGHT_GRAY: Final = "F4F4F4"

#: Frozen. Downstream consumers index by position, so inserting a column in the middle silently
#: shifts every one after it — a reader would get tolerances under the severity heading and have no
#: reason to doubt them. Add new columns at the end.
FINDING_COLUMNS: Final = (
    "check",
    "outcome",
    "severity",
    "comparison",
    "difference",
    "tolerance",
    "arithmetic_unit",
    "variant",
    "rule_snapshot",
    "engine_version",
    "evidence_pages",
    "reason",
    "notes",
    "reviewer_summary",
)

#: One row per operand the calculation used. Frozen for the same reason.
OPERAND_COLUMNS: Final = (
    "check",
    "operand",
    "value",
    "source",
    "evidence_page",
    "evidence_uri",
)

#: What a value column says when a check abstained before producing that value.
#:
#: A word rather than a blank. An empty cell is ambiguous between "nothing to report", "the export
#: dropped it" and "the check was never run", and those want different responses from a reader.
NOT_APPLICABLE: Final = "n/a — check abstained"

#: What a value column says when a decision was reached but the rule declares no such value.
NONE_DECLARED: Final = "none declared"

#: What a value column says when a reference could not be read as a complete citation.
UNREADABLE_REFERENCE: Final = "unreadable reference"

#: What a value column says when the database never kept this field.
#:
#: `findings` stores the outcome, the severity, the trace and the parameter versions. It does **not**
#: store the finding's prose reason, its delta, its applicability variant or its notes — those live on
#: `verdict.finding.Finding`, which is the engine's value type, and `record_finding` does not persist
#: them. A workbook built from stored rows therefore cannot fill those columns, and says so.
#:
#: A word rather than a blank, for the same reason as `NOT_APPLICABLE`: an empty cell reads as "there
#: was nothing to say", and here there was something to say and nobody wrote it down. Distinguishing
#: the two is the difference between a reader trusting the column and a reader checking it.
NOT_RECORDED: Final = "not recorded in the database"


def exact_text(value: object) -> str:
    """Render a value as text that has lost nothing.

    `Fraction` renders as `1/8`, never `0.125`. `Measurement` renders as its exact value with the
    unit it was authored in, because a number without its unit is the other half of the same
    mistake — and it keeps the source token when there is one, so a reader can see what the drawing
    actually said as well as what it was read as.
    """
    if isinstance(value, Measurement):
        rendered = f"{value.exact} {value.unit.value}"
        return f"{rendered} (as written: {value.raw_text})" if value.raw_text else rendered
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, tuple):
        return "; ".join(exact_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def write_value(cell: Cell, value: object) -> None:
    """Write one value as text, and pin the cell's format so it stays text.

    Both halves matter. A spreadsheet that turns `1/8` into `0.125` has discarded the distinction
    the entire units layer exists to preserve — and a string cell left on the `General` format is
    one re-save away from a consumer's tooling doing the conversion on their behalf.
    """
    cell.value = exact_text(value)
    cell.number_format = TEXT_FORMAT


def decode_reference(reference: str) -> tuple[str, str] | None:
    """(page, document version) for one evidence reference, or `None` if it is not a citation.

    **Both fields or neither.** A reference carrying `page` and no `document_version_id` decodes
    happily and yields "page 1" — of which drawing, nobody can say. Reporting that as provenance
    would put an unfollowable citation in the column a reader uses to go and check, which is worse
    than admitting the reference could not be read: they would look, fail to find it, and doubt the
    finding rather than the export.

    One decoder for both callers, deliberately. Two copies of "what counts as a readable reference"
    is how the operand sheet and the findings sheet come to disagree about the same reference — and
    a reader comparing them would have no way to tell which was right.

    `evidence/gate.py` writes `document_version_id`, `page`, `polygon` and `space`. Only the two
    fields this module reports are required here; a reference with no polygon is still a page in a
    document, and refusing it would drop a usable citation to enforce a field nothing here reads.
    """
    try:
        decoded = json.loads(reference)
        page = decoded["page"]
        document = decoded["document_version_id"]
    except (ValueError, TypeError, KeyError):
        return None
    # Rejected here, because both would otherwise survive a shape check and produce a citation that
    # looks followable: "page 1" of document "", or page 0 of a set whose first sheet is 1. The
    # column exists so somebody can go and look, and a reader who looks and fails to find it doubts
    # the finding rather than the export.
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        return None
    if not isinstance(document, str) or not document.strip():
        return None
    return (str(page + 1), document)


def _evidence_pages(finding: Finding) -> str:
    """The pages this finding's evidence sits on, for someone holding the drawing set.

    An unreadable reference is reported as such rather than skipped: a reference the export could
    not decode is a fact about the export, and dropping it would leave the row looking like a
    finding with no evidence.
    """
    decoded = [decode_reference(reference) for reference in finding.evidence_refs]
    return ", ".join(UNREADABLE_REFERENCE if parts is None else parts[0] for parts in decoded)


def _evidence_parts(reference: str | None) -> tuple[str, str]:
    """(page, document reference) for one operand's evidence, as text.

    Returns the raw reference when it cannot be decoded, rather than an empty pair. The point of
    this column is that somebody can go and look; handing them nothing because the JSON changed
    shape defeats it.
    """
    if not reference:
        return ("", "")
    decoded = decode_reference(reference)
    return decoded if decoded is not None else (UNREADABLE_REFERENCE, reference)


def _finding_row(finding: Finding) -> tuple[object, ...]:
    """One finding as a row, in `FINDING_COLUMNS` order.

    Keyed on **whether a calculation happened**, not on whether the outcome was a decision. The two
    are not the same: `Finding` requires a trace for a decision but *permits* one on an abstention,
    which is the ordinary shape of a `REVIEW_REQUIRED` raised partway through arithmetic — a unit it
    could not reconcile, say. When there is a trace, its comparison and tolerance are written,
    because the calculation genuinely ran and hiding it would leave a reviewer with an abstention and
    no idea how far it got.
    """
    trace = finding.trace
    abstained = is_abstention(finding.outcome)

    if trace is None:
        comparison: str = NOT_APPLICABLE if abstained else NONE_DECLARED
        tolerance: object = comparison
        unit: str = comparison
    else:
        comparison = trace.comparison
        tolerance = trace.tolerance if trace.tolerance is not None else NONE_DECLARED
        unit = trace.arithmetic_unit.value if trace.arithmetic_unit is not None else NONE_DECLARED

    return (
        finding.rule_id,
        finding.outcome.value,
        finding.severity.value,
        comparison,
        finding.delta if finding.delta is not None else (NOT_APPLICABLE if abstained else ""),
        tolerance,
        unit,
        finding.variant or "",
        finding.snapshot_id,
        finding.engine_version,
        _evidence_pages(finding),
        finding.reason,
        " | ".join(finding.notes),
        finding.reason,
    )


def _operand_rows(finding: Finding) -> list[tuple[object, ...]]:
    """One row per operand the calculation used, in `OPERAND_COLUMNS` order.

    A finding with no trace contributes no rows — nothing was read, and a row of blanks would
    suggest an operand was found and had no value. It still appears on the findings sheet, which is
    where a reader learns the check did not decide.

    An abstention that *does* carry a trace contributes its operands, for the same reason
    `_finding_row` writes its comparison: those operands were read. An abstention raised because two
    operands disagreed is one whose operands are the most useful thing in the file.
    """
    if finding.trace is None:
        return []
    rows: list[tuple[object, ...]] = []
    for operand in finding.trace.operands:
        page, document = _evidence_parts(operand.evidence_ref)
        rows.append((finding.rule_id, operand.name, operand.value, operand.source, page, document))
    return rows


def _write_summary(
    workbook: Workbook,
    findings: Sequence[StoredFinding] | Sequence[Finding],
    *,
    signoff: WorkbookSignoff | None,
) -> None:
    """Write the human handoff sheet from stored outcome strings, never parsed display values."""
    sheet = workbook.create_sheet(SUMMARY_SHEET)
    sheet.sheet_view.showGridLines = False
    sheet.merge_cells("A1:F1")
    sheet.merge_cells("A2:F2")
    title = sheet["A1"]
    title.value = "GRANITI VICENTIA × NEXOLV"
    title.font = Font(name="Courier New", bold=True, size=16, color=_WHITE)
    title.fill = PatternFill("solid", fgColor=_BLACK)
    title.alignment = Alignment(horizontal="left", vertical="center")
    title.number_format = TEXT_FORMAT
    subtitle = sheet["A2"]
    subtitle.value = "SHOP DRAWING REVIEW — HUMAN-OPERATED V1"
    subtitle.font = Font(name="Courier New", bold=True, size=11, color=_BLACK)
    subtitle.alignment = Alignment(horizontal="left")
    subtitle.number_format = TEXT_FORMAT
    sheet.row_dimensions[1].height = 30
    sheet.row_dimensions[2].height = 22

    outcome_counts: dict[str, int] = {}
    for finding in findings:
        outcome_counts[finding.outcome] = outcome_counts.get(finding.outcome, 0) + 1
    summary_rows = (
        ("REVIEW SUMMARY", ""),
        ("FINDINGS", str(len(findings))),
        ("PASS", str(outcome_counts.get("PASS", 0))),
        ("REVIEW REQUIRED", str(outcome_counts.get("REVIEW_REQUIRED", 0))),
        ("NOT FOUND", str(outcome_counts.get("NOT_FOUND", 0))),
        ("FAIL", str(outcome_counts.get("FAIL", 0))),
        ("REVIEWER SIGN-OFF", ""),
        (
            "STATUS",
            (
                "SIGNED OFF — report released after approval"
                if signoff is not None
                else "AWAITING REVIEWER SIGN-OFF — download remains blocked"
            ),
        ),
        ("APPROVED BY", signoff.approved_by if signoff is not None else "not yet recorded"),
        ("APPROVED AT", signoff.approved_at if signoff is not None else "not yet recorded"),
        ("WORKBOOK CONTENTS", "Findings and exact stored operands"),
    )
    for row_index, (label, value) in enumerate(summary_rows, start=4):
        label_cell = sheet.cell(row=row_index, column=1, value=label)
        label_cell.font = Font(name="Courier New", bold=True, color=_WHITE if not value else _BLACK)
        label_cell.fill = PatternFill("solid", fgColor=_BLACK if not value else _LIGHT_GRAY)
        label_cell.number_format = TEXT_FORMAT
        value_cell = sheet.cell(row=row_index, column=2, value=value)
        value_cell.font = Font(name="Courier New", color=_BLACK)
        value_cell.number_format = TEXT_FORMAT
        value_cell.alignment = Alignment(vertical="top", wrap_text=True)

    for column, width in {"A": 26, "B": 55, "C": 18, "D": 18, "E": 18, "F": 18}.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A4"


def _write_sheet(
    workbook: Workbook, title: str, columns: Sequence[str], rows: Sequence[Sequence[object]]
) -> None:
    sheet = workbook.create_sheet(title)
    for index, name in enumerate(columns, start=1):
        heading = sheet.cell(row=1, column=index, value=name)
        heading.font = Font(name="Courier New", bold=True, color=_WHITE)
        heading.fill = PatternFill("solid", fgColor=_BLACK)
        heading.number_format = TEXT_FORMAT
        # Wide enough to read without the reader resizing thirteen columns first. A guess, but a
        # cosmetic one — nothing here depends on it.
        sheet.column_dimensions[get_column_letter(index)].width = 22

    for offset, row in enumerate(rows, start=2):
        for index, value in enumerate(row, start=1):
            cell = sheet.cell(row=offset, column=index)
            write_value(cell, value)
            cell.font = Font(name="Courier New")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if offset % 2 == 0:
                cell.fill = PatternFill("solid", fgColor=_LIGHT_GRAY)

    # Headings stay visible while scrolling, and the header row cannot be sorted into the data.
    sheet.freeze_panes = "A2"


@dataclass(frozen=True, slots=True)
class StoredFinding:
    """One finding exactly as the database holds it, for the export a worker produces.

    **Why this exists rather than rebuilding a `Finding`.** The engine's own value type is the
    natural input, and it cannot be recovered from storage: `app/verdicts/trace.py:render_value`
    writes operand values as display text — a tuple of measurements becomes the single string
    `24, 24, 30` — so reconstructing one would mean re-parsing presentation output back into exact
    arithmetic. That is precisely how `984 mm` once became 984 inches, and it is not a mistake worth
    making twice for the sake of type symmetry.

    So the workbook a worker writes is built from what was stored. The columns are the same ones
    `write_workbook` uses, so the two files are directly comparable; the fields storage never kept
    are marked `NOT_RECORDED` rather than left blank.
    """

    rule_id: str
    outcome: str
    severity: str
    snapshot_id: str
    engine_version: str
    trace: Mapping[str, object]
    """`findings.trace` verbatim — a calculation trace, or an abstention's cause and reason."""

    # Four columns `findings` gained in #521. Each is `None` for a finding written before that
    # migration, and the workbook says `NOT_RECORDED` rather than leaving the cell empty: an old
    # finding genuinely has no recorded reason, and a blank would read as "there was nothing to say".
    reason: str | None = None
    delta: str | None = None
    """Already exact text — the caller renders the stored rational, so this module does not have a
    second opinion about how a number looks."""

    variant: str | None = None
    notes: tuple[str, ...] | None = None
    reviewer_summary: str | None = None
    """Post-verdict prose after fidelity validation, or ``None`` for the deterministic reason.

    This is presentation only.  It is appended to the frozen findings sheet rather than replacing
    ``reason``, so a reviewer can always compare the narration with the engine's exact words.
    """


@dataclass(frozen=True, slots=True)
class WorkbookSignoff:
    """An optional immutable approval record for a future publication-specific rendering.

    V1's worker writes the review workbook before approval and the download endpoint enforces the
    sign-off gate.  Keeping this input explicit means a future publication artifact cannot invent
    an approver by deriving one from display text.
    """

    approved_by: str
    approved_at: str

    def __post_init__(self) -> None:
        if not self.approved_by.strip() or not self.approved_at.strip():
            raise ValueError("workbook sign-off requires an approver and recorded time")


def _text(value: object) -> str:
    """One stored JSON value as text, without asserting a shape the database does not enforce.

    `trace` is `JSONB`, so nothing stops a row written by an older version of the writer from having
    a field this reader did not expect. Rendering whatever is there beats raising: the export's job
    is to show a reviewer what was recorded.
    """
    return "" if value is None else str(value)


def _stored_operands(trace: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The trace's operand list, or nothing when it is an abstention or an unfamiliar shape."""
    operands = trace.get("operands")
    if not isinstance(operands, list):
        return []
    return [operand for operand in operands if isinstance(operand, Mapping)]


def _column(value: str | None, *, old: bool, abstained: bool) -> str:
    """One of the #521 columns as a cell: the value, or which kind of absence this is."""
    if value is not None:
        return value
    return NOT_RECORDED if old else _absent(abstained)


def _predates_provenance(finding: StoredFinding) -> bool:
    """Whether this row was written before `findings` gained its four columns (#521).

    Told by `notes`, which is the only one of the four that is never null on a row written since:
    `record_finding` writes `[]` for a check that produced no notes. The other three are legitimately
    null on a new row — a PASS has no delta, a rule without a discriminator has no variant — so none
    of them can distinguish "nothing to record" from "recorded before the column existed".

    That distinction is the whole point of the marker. Without it a finding from last week and a
    finding from before the migration would print the same thing, and only one of them is missing
    something.
    """
    return finding.notes is None


def _absent(abstained: bool) -> str:
    """What a value column says when a *recorded* finding carries no value for it.

    A check that abstained has no delta to report; a decision with no delta declared none. Neither is
    the same as never having been recorded, which `_predates_provenance` answers.
    """
    return NOT_APPLICABLE if abstained else NONE_DECLARED


def _stored_finding_row(finding: StoredFinding) -> tuple[object, ...]:
    """One stored finding as a row, in `FINDING_COLUMNS` order.

    Keyed on whether a calculation was recorded, the same way `_finding_row` is keyed on whether one
    happened: an abstention trace carries a `reason` and no arithmetic, and a calculation trace
    carries the comparison and no prose.
    """
    trace = finding.trace
    abstained = is_abstention(Outcome(finding.outcome))
    comparison_value = trace.get("comparison")

    if comparison_value is None:
        comparison: str = NOT_APPLICABLE if abstained else NONE_DECLARED
        tolerance: str = comparison
        unit: str = comparison
    else:
        comparison = _text(comparison_value)
        tolerance = _text(trace.get("tolerance")) or NONE_DECLARED
        unit = _text(trace.get("arithmetic_unit")) or NONE_DECLARED

    # A decision's reason is a column since #521; an abstention's has always been in its trace. The
    # column is preferred when both are present, because it is what the engine actually said.
    old = _predates_provenance(finding)
    reason = finding.reason or _text(trace.get("reason")) or NOT_RECORDED

    pages = [
        _evidence_parts(_text(operand.get("evidence_ref")) or None)[0]
        for operand in _stored_operands(trace)
    ]

    return (
        finding.rule_id,
        finding.outcome,
        finding.severity,
        comparison,
        _column(finding.delta, old=old, abstained=abstained),
        tolerance,
        unit,
        _column(finding.variant, old=old, abstained=abstained),
        finding.snapshot_id,
        finding.engine_version,
        ", ".join(page for page in pages if page),
        reason,
        NOT_RECORDED if finding.notes is None else " | ".join(finding.notes),
        finding.reviewer_summary or reason,
    )


def _stored_operand_rows(finding: StoredFinding) -> list[tuple[object, ...]]:
    """One row per operand the stored trace recorded, in `OPERAND_COLUMNS` order."""
    rows: list[tuple[object, ...]] = []
    for operand in _stored_operands(finding.trace):
        page, document = _evidence_parts(_text(operand.get("evidence_ref")) or None)
        rows.append(
            (
                finding.rule_id,
                _text(operand.get("name")),
                # Already exact text: `render_value` wrote it, and re-rendering it here would be a
                # second opinion about how a number looks.
                _text(operand.get("value")),
                _text(operand.get("source")),
                page,
                document,
            )
        )
    return rows


#: The instant a produced workbook claims it was created and modified.
#:
#: openpyxl writes the clock into `docProps/core.xml`, and re-stamps `modified` at save time
#: whatever the workbook's properties say — so setting them before saving fixes `created` and not
#: `modified`. Normalised while the archive is being rewritten instead, which is one mechanism
#: rather than two and does not depend on how a library version chooses to fill the field.
#:
#: *When* a deliverable was produced is recorded by `output_artifacts.created_at`, on a row where it
#: can be audited — not inside a file whose identity is supposed to be its hash.
_FIXED_DOCUMENT_INSTANT: Final = "1980-01-01T00:00:00Z"

#: The two `docProps/core.xml` fields that carry it.
_DOCUMENT_INSTANT_FIELDS: Final = re.compile(
    rb"(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)

#: The timestamp written into every entry of a produced workbook.
#:
#: An `.xlsx` is a ZIP, and a ZIP records a modification time per entry. openpyxl stamps the clock,
#: so the same findings written twice produce different bytes — which makes a content-addressed key
#: not content-addressed. `workflow/stages.py:generate_outputs` hashes these bytes to decide whether
#: a deliverable has already been recorded, so a regeneration more than two seconds after the first
#: recorded a *second* deliverable for one set of findings.
#:
#: The value is the MS-DOS epoch, the earliest a ZIP can express. It is a constant rather than a
#: build date because the point is that it carries no information: a reader comparing two workbooks
#: should see a difference only where the findings differ.
_FIXED_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)

#: Where a workbook records when it says it was made.
_CORE_PROPERTIES: Final = "docProps/core.xml"


def _without_timestamps(archive: bytes) -> bytes:
    """The same workbook with every clock in it fixed.

    Two clocks, both of which made a content-addressed key not content-addressed: the modification
    time a ZIP records per entry, and the `created`/`modified` instants openpyxl writes into
    `docProps/core.xml`.

    Rewritten rather than patched in place: a ZIP records the time in both the local header and the
    central directory, and editing one of the two makes an archive some readers reject. Entry order,
    names, contents and compression are preserved exactly, so this changes the bytes that carry a
    clock and nothing else.
    """
    source = zipfile.ZipFile(BytesIO(archive))
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as destination:
        for entry in source.infolist():
            fixed = zipfile.ZipInfo(entry.filename, date_time=_FIXED_ZIP_TIMESTAMP)
            fixed.compress_type = entry.compress_type
            fixed.external_attr = entry.external_attr
            fixed.internal_attr = entry.internal_attr
            fixed.create_system = entry.create_system
            content = source.read(entry.filename)
            if entry.filename == _CORE_PROPERTIES:
                content = _DOCUMENT_INSTANT_FIELDS.sub(
                    rb"\g<1>" + _FIXED_DOCUMENT_INSTANT.encode() + rb"\g<2>", content
                )
            destination.writestr(fixed, content)
    return out.getvalue()


def write_stored_workbook(
    findings: Sequence[StoredFinding], *, signoff: WorkbookSignoff | None = None
) -> bytes:
    """The same workbook, built from stored rows instead of engine values.

    Same sheets, same columns, same text-cell discipline — `_write_sheet` is shared, so the two
    cannot drift in how they write a cell. The order given is the order written, as above.
    """
    if isinstance(findings, str) or not isinstance(findings, Sequence):
        raise TypeError("findings must be a sequence of StoredFinding values")
    for finding in findings:
        if not isinstance(finding, StoredFinding):
            raise TypeError("findings must contain only StoredFinding values")

    workbook = Workbook()
    workbook.remove(workbook.active)
    _write_summary(workbook, findings, signoff=signoff)
    _write_sheet(
        workbook, FINDINGS_SHEET, FINDING_COLUMNS, [_stored_finding_row(f) for f in findings]
    )
    _write_sheet(
        workbook,
        OPERANDS_SHEET,
        OPERAND_COLUMNS,
        [row for finding in findings for row in _stored_operand_rows(finding)],
    )

    out = BytesIO()
    workbook.save(out)
    return _without_timestamps(out.getvalue())


def write_workbook(findings: Sequence[Finding]) -> bytes:
    """The findings as `.xlsx` bytes: every finding a row, every value text.

    Bytes rather than a path, so the caller decides where this goes — `storage/` hashes and stores
    artifacts, and a function that wrote to disk itself would either bypass that or duplicate it.

    The order given is the order written. Sorting here would make two exports of the same run
    differ from the redline, which is the document a reader has beside it.
    """
    if isinstance(findings, str) or not isinstance(findings, Sequence):
        raise TypeError("findings must be a sequence of Finding values")
    for finding in findings:
        if not isinstance(finding, Finding):
            raise TypeError("findings must contain only Finding values")

    workbook = Workbook()
    # A new Workbook comes with one sheet already; both sheets below are created explicitly, so the
    # default would otherwise sit at the front of the file as an empty "Sheet".
    workbook.remove(workbook.active)
    _write_summary(workbook, findings, signoff=None)

    _write_sheet(workbook, FINDINGS_SHEET, FINDING_COLUMNS, [_finding_row(f) for f in findings])
    _write_sheet(
        workbook,
        OPERANDS_SHEET,
        OPERAND_COLUMNS,
        [row for finding in findings for row in _operand_rows(finding)],
    )

    out = BytesIO()
    workbook.save(out)
    return _without_timestamps(out.getvalue())
