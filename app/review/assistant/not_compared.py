"""The full check's Not-compared list (#1619): one set of rows for the PDF, the workbook and the CSV.

Under the architect-first rules (`classification.py`) an item that could not be compared is not on
the cloud drawing and nobody is asked to decide it. It is listed, by code at $0, in "… - Not
compared.pdf" and "… - Not compared.xlsx", grouped and sorted by reason then page, each row saying
what the vendor shows, what the architect shows, why it could not be compared and what is needed
to compare it, with the vendor's page reference. The rows here are the one source: the PDF
(`proof_drawing.not_compared_pdf`), the workbook (`xlsx`), its CSV fallback and the screen's count
all come from `rows`, so every count agrees. Every cell goes through the vendor-text guard
(`vendor_text`, #1766): the list is sent to the vendor. The register says the same reason for the
same item (`vendor_reason`), so the two files never disagree on why.

Only items of the check's products are listed: the full check's product scope (#1621,
`product_scope`) already keeps out-of-scope findings out of the answer, and `rows` can also be
given the products to keep.

Pure code (the workbook through `workbook.write_xlsx`): no PDF, no AI call.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.review.assistant.classification import REASON_WORDS, REASONS, TRADE_WORDS
from app.review.assistant.proof import classified, proof_group, vendor_value
from app.review.assistant.vendor_text import internal, item_name, vendor_text
from app.review.assistant.workbook import write_xlsx

__all__ = [
    "COLUMNS",
    "SET_SHEET",
    "Row",
    "csv_text",
    "rows",
    "set_rows_of",
    "summary_rows",
    "vendor_reason",
    "vendor_why",
    "xlsx",
]

#: The workbook's first sheet and the CSV, in Anant's order.
COLUMNS: Final = (
    "#",
    "Page",
    "Area",
    "Product",
    "Item",
    "What the vendor shows",
    "What the architect shows",
    "Why not compared",
    "What is needed to compare it",
    "Vendor page reference",
)
_NO_SIZE: Final = re.compile(
    r"\b(?:no|not)\b[^.;]*\b(?:size|width|depth|height|dimension|printed)", re.IGNORECASE
)
#: GV's own working words that never go to the vendor in this list, besides `vendor_text.internal`
#: (the AI, record ids, helpers): code's label ids ("p23.D17"), the compare call's answer and the
#: model, the blind second look, the OCR engines, the critic, a failed call.
_PROCESS: Final = re.compile(
    r"\bp\d+\.D\d+\b|\bthe answer\b|\bthe model\b|\bblind\b|\bOCR\b|\bcritic\b|"
    r"\bthe call failed\b|\bnot the form\b|\bcut short\b",
    re.IGNORECASE,
)
#: What a reason says when its detail is GV's own working words (the list is sent to the vendor).
_PLAIN_WHY: Final = "the drawings do not settle it"


def vendor_why(why: object) -> str:
    """A row's "why" as the vendor may read it: the detail, unless it holds GV's own working words
    (`_PROCESS`, `vendor_text.internal`: the screen keeps them), then the plain words."""
    words = _flat(why, 400)
    if not words or internal(words) or _PROCESS.search(words):
        return _PLAIN_WHY if words else ""
    return words


@dataclass(frozen=True, slots=True)
class Row:
    """One item that could not be compared."""

    finding_id: str
    page: int | None
    area: str
    product: str
    item: str
    vendor: str
    architect: str
    reason: str
    why: str
    needed: str
    reference: str
    file: str | None
    box: tuple[float, float, float, float] | None
    level: str = "item"
    """`item`: a finding of the answer (counted on screen); `set`: a gap of the set found by code
    (the finish check, #1623; the vendor consistency check), listed but not a finding."""

    @property
    def reason_words(self) -> str:
        return REASON_WORDS.get(self.reason, self.reason)

    @property
    def label(self) -> str:
        """The trade's words ("Field verify", "RFI to the architect", "Illegible", …)."""
        return TRADE_WORDS.get(self.reason, self.reason_words)


def vendor_reason(row: Row) -> str:
    """Why a row could not be compared, as the list's "Why not compared" column and the issue
    register both say it: the trade's words, then the detail ("Illegible: could not read …")."""
    return f"{row.label}: {row.why}" if row.why else row.label


def _guarded(text: object, limit: int = 160) -> str:
    """A cell as the vendor may read it (`vendor_text`): GV's own words never printed. (Only the
    guard: a value's own words, like a "blind corner" cabinet, are the drawings', not GV's.)"""
    return vendor_text(_flat(text, limit), "") or ""


#: Code's own label ids ("p23.D17"): never the vendor's page reference.
_LABEL_ID: Final = re.compile(r"^p\d+\.D\d+$")


def _flat(text: object, limit: int = 160) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


def _architect_shows(finding: Mapping[str, Any], reason: str) -> str:
    """What the architect shows, plainly: "not shown", "no size printed", or its value(s)."""
    words = _flat(finding.get("architect"), 120)
    if reason == "nothing_to_compare":
        if not words or words.lower() in ("n/a", "none", "-"):
            return "not shown"
        return f"no size printed ({words})" if not _NO_SIZE.search(words) else words
    if reason == "architect_disagrees":
        return f"{words} vs {_flat(finding.get('vendor'), 60)}"
    return words or "not shown"


def _box(raw: object) -> tuple[str | None, tuple[float, float, float, float]] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        box = (float(raw["x0"]), float(raw["y0"]), float(raw["x1"]), float(raw["y1"]))
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("file")
    return (None if name is None else str(name)), box


def _reference(finding: Mapping[str, Any], file: str | None) -> str:
    page = finding.get("page")
    dims = finding.get("dims") or {}
    vendor = dims.get("vendor") if isinstance(dims, Mapping) else None
    named = str(vendor.get("id") or "") if isinstance(vendor, Mapping) else ""
    # Code's own label ids ("p23.D17") are GV's working words: never the vendor's reference.
    label = f", label {named}" if named and not _LABEL_ID.match(named) else ""
    where = f" of {file}" if file else ""
    return vendor_text(f"page {page}{where}{label}", "") or "" if page is not None else ""


def in_scope(category: object, products: Sequence[str], known: Sequence[str]) -> bool:
    """An item of one of the check's products, or of no product at all."""
    name = str(category or "other")
    return not products or name in products or name == "other" or name not in known


def rows(
    findings: Sequence[Mapping[str, Any]],
    *,
    areas: Mapping[int, str] | None = None,
    products: Sequence[str] = (),
    known_products: Sequence[str] = (),
    product_label: Any = None,
) -> list[Row]:
    """Every Not-compared item of `findings` (`proof.proof_group`), sorted by reason (the order of
    `classification.REASONS`) then page. `areas`: page -> area (the page map's); `products`: the
    check's products (the scope), `known_products`: every product the registry knows."""
    order: dict[str, int] = {reason: at for at, reason in enumerate(REASONS)}
    out: list[Row] = []
    for finding in findings:
        if proof_group(finding) != "not_compared":
            continue
        if not in_scope(finding.get("category"), products, known_products):
            continue
        found = classified(finding)
        reason = found.reason or "not_proved"
        where = _box(finding.get("vendor_box")) or _box(finding.get("arch_box"))
        file = where[0] if where is not None else None
        page = finding.get("page") if isinstance(finding.get("page"), int) else None
        category = str(finding.get("category") or "other")
        named = item_name(dict(finding))
        out.append(
            Row(
                finding_id=str(finding.get("id") or ""),
                page=page,
                area=_guarded((areas or {}).get(page or 0, "")),
                product=_guarded(product_label(category) if product_label else category),
                item=_flat(named, 120) if named else f"Item on page {page}" if page else "Item",
                vendor=_guarded(vendor_value(finding), 120),
                architect=_guarded(_architect_shows(finding, reason)),
                reason=reason,
                why=vendor_why(found.why),
                needed=_guarded(found.settle, 200),
                reference=_reference(finding, file),
                file=file,
                box=where[1] if where is not None else None,
            )
        )
    out.sort(key=lambda row: (order.get(row.reason, len(order)), row.page or 0, row.item))
    return out


#: A set-level gap's `why` (`chat_unknowns` shape) as a Not-compared reason.
_GAP_REASONS: Final[Mapping[str, str]] = {
    "not_comparable": "finish_not_comparable",
    "unreadable": "unreadable",
    # The simple full check's rows that are not findings (#1665): its reason by name.
    **{
        reason: reason
        for reason in ("nothing_to_compare", "not_proved", "different_reference", "ai_unsure")
    },
}


def gap_rows(
    gaps: Sequence[Mapping[str, Any]], *, areas: Mapping[int, str] | None = None
) -> list[Row]:
    """The set's gaps code found (the finish check's "not comparable" codes, #1623; areas or rounds
    with no sheet, #1584) as Not-compared rows of the set (`level: set`)."""
    out: list[Row] = []
    for gap in gaps:
        if not isinstance(gap, Mapping) or not gap.get("what"):
            continue
        page = gap.get("page") if isinstance(gap.get("page"), int) else None
        reason = _GAP_REASONS.get(str(gap.get("why") or ""), "no_sheet")
        what = _flat(gap.get("what"), 160).replace(
            "the answer's item limit was reached", "too many items in one check to list it"
        )
        if internal(what) or _PROCESS.search(what):
            # The row's place stays (before its first ": "); its detail is GV's own words.
            head = what.split(": ", 1)[0] if ": " in what else ""
            what = f"{head}: {_PLAIN_WHY}" if head and not _PROCESS.search(head) else _PLAIN_WHY
        out.append(
            Row(
                finding_id="",
                page=page,
                area=_guarded((areas or {}).get(page or 0, "")),
                product="",
                item=vendor_text(what, _PLAIN_WHY) or _PLAIN_WHY,
                vendor="",
                architect="",
                reason=reason,
                why=REASON_WORDS.get(reason, reason),
                needed=_guarded(gap.get("needs"), 200),
                reference="" if page is None else f"page {page}",
                file=None,
                box=None,
                level="set",
            )
        )
    order: dict[str, int] = {reason: at for at, reason in enumerate(REASONS)}
    out.sort(key=lambda row: (order.get(row.reason, len(order)), row.page or 0, row.item))
    return out


def _table(found: Sequence[Row]) -> list[list[str]]:
    return [
        [
            str(at),
            "" if row.page is None else str(row.page),
            row.area,
            row.product,
            row.item,
            row.vendor,
            row.architect,
            vendor_reason(row),
            row.needed,
            row.reference,
        ]
        for at, row in enumerate(found, start=1)
    ]


def summary_rows(found: Sequence[Row]) -> list[list[str]]:
    """The Summary sheet: the counts by reason, then by product."""
    by_reason = Counter(row.reason for row in found)
    by_product = Counter(row.product or "the set as a whole" for row in found)
    out = [["By reason", TRADE_WORDS.get(r, r), str(by_reason[r])] for r in REASONS if by_reason[r]]
    out += [["By product", name, str(n)] for name, n in sorted(by_product.items())]
    items = sum(1 for row in found if row.level == "item")
    out.append(["Total", "items of this check", str(items)])
    if items != len(found):
        out.append(["Total", "found by code across the set", str(len(found) - items)])
    return out


#: The workbook's sheet of the set's own gaps code found (the PDF's section of the same name).
SET_SHEET: Final = "Found across the set"


def xlsx(found: Sequence[Row]) -> bytes:
    """The workbook: "Not compared" (the items: as many rows as the screen and the cloud drawing
    count), "Found across the set" (the set's own gaps code found: sheet gaps, finish notes, the
    labels the architect gives no size for; the PDF lists them apart too) and "Summary" (the
    counts). Raises `ModuleNotFoundError` when openpyxl is not installed (`csv_text` then)."""
    items = [row for row in found if row.level == "item"]
    gaps = [row for row in found if row.level != "item"]
    return write_xlsx(
        [
            ("Not compared", COLUMNS, _table(items)),
            (SET_SHEET, COLUMNS, _table(gaps)),
            ("Summary", ("Counted", "What", "Items"), summary_rows(found)),
        ]
    )


def set_rows_of(sheet: bytes) -> list[Row] | None:
    """The set's own rows (`level: set`) read back from a workbook `xlsx` made (#1803): an answer
    stored before its set gaps were kept with it can still have its list made again after the
    reviewer's decisions, with the same set rows. `None` when it is not such a workbook."""
    from importlib import import_module

    try:
        openpyxl = import_module("openpyxl")
        book = openpyxl.load_workbook(io.BytesIO(sheet), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - not a workbook this list made
        return None
    if SET_SHEET not in book.sheetnames:
        return None
    lines = list(book[SET_SHEET].iter_rows(values_only=True))
    if not lines or tuple(str(c or "") for c in lines[0][: len(COLUMNS)]) != COLUMNS:
        return None
    by_label = {
        TRADE_WORDS.get(reason, REASON_WORDS.get(reason, reason)): reason for reason in REASONS
    }
    out: list[Row] = []
    for line in lines[1:]:
        cells = [("" if c is None else str(c)) for c in line[: len(COLUMNS)]]
        if len(cells) < len(COLUMNS) or not cells[4]:
            continue
        _at, page, area, product, item, vendor, architect, why_words, needed, reference = cells
        # The column is "<label>: <why>" (`vendor_reason`); a label may hold ": " itself.
        label = max(
            (k for k in by_label if why_words == k or why_words.startswith(f"{k}: ")),
            key=len,
            default="",
        )
        reason = by_label.get(label, "no_sheet")
        why = why_words[len(label) + 2 :] if label else why_words
        out.append(
            Row(
                finding_id="",
                page=int(page) if page.isdigit() else None,
                area=area,
                product=product,
                item=item,
                vendor=vendor,
                architect=architect,
                reason=reason,
                why=why,
                needed=needed,
                reference=reference,
                file=None,
                box=None,
                level="set",
            )
        )
    return out


def csv_text(found: Sequence[Row]) -> str:
    """The rows as CSV (the workbook's fallback without openpyxl)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for line in _table(found):
        writer.writerow(["'" + c if c.startswith(("=", "+", "-", "@")) else c for c in line])
    return out.getvalue()


# ---- the two files of a full check -------------------------------------------------------------

#: The files' names end with this (the screen finds them by it).
FILE_KIND: Final = "Not compared"
XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def check_rows(
    workspace: Any, findings: Sequence[Mapping[str, Any]], products: Sequence[str]
) -> list[Row]:
    """`rows` for a full check's workspace: its page map's areas, its products (the scope)."""
    from importlib import import_module

    resolver = import_module("app.review.assistant.decision_resolver")
    plan = import_module("app.review.assistant.full_check_plan")
    findings_module = import_module("app.review.assistant.chat_findings")
    registry = import_module("vocabulary.product_registry").registry()
    main = findings_module.main_upload(findings_module.read_listing(workspace))
    name = None if main is None else str(main["name"])
    pack = workspace.set_pack
    page_map = plan.read_page_map(pack, assume=True, main_file=name) if pack is not None else None
    areas = resolver.page_areas(page_map, name) if page_map else {}
    # The product scope (#1621) is already a hard filter on the answer's findings
    # (`product_scope`, the finding book): every finding here is in scope.
    del products
    return rows(findings, areas=areas, product_label=registry.category_label)


def make_files(
    workspace: Any,
    found: Sequence[Row],
    pdf_out: Any,
    xlsx_out: Any,
    *,
    today: str | None = None,
) -> tuple[Any, Any]:
    """The two files (`chat_findings.MarkupFile`): "… - Not compared.pdf" and "… - Not
    compared.xlsx" (".csv" where openpyxl is missing), from the same rows."""
    import hashlib
    from datetime import UTC, datetime
    from importlib import import_module

    findings_module = import_module("app.review.assistant.chat_findings")
    drawing = import_module("app.review.assistant.proof_drawing")
    main = findings_module.main_upload(findings_module.read_listing(workspace))
    if main is None or not (workspace.uploads / str(main["name"])).is_file():
        raise LookupError("no uploads")
    name = str(main["name"])
    date = today or datetime.now(UTC).strftime("%Y-%m-%d")
    pack = workspace.set_pack
    drawing.not_compared_pdf(
        workspace.uploads / name,
        list(found),
        pdf_out,
        set_name=name,
        stamp=drawing.stamp_line(date),
        cache=None if pack is None else pack / "hires",
    )
    pdf = pdf_out.read_bytes()
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    try:
        sheet, ending = xlsx(found), "xlsx"
    except ModuleNotFoundError:
        sheet, ending = csv_text(found).encode("utf-8-sig"), "csv"
    xlsx_out.write_bytes(sheet)
    made = findings_module.MarkupFile
    return (
        made(
            findings_module.markup_name(name, FILE_KIND), len(pdf), hashlib.sha256(pdf).hexdigest()
        ),
        made(f"{stem} - {FILE_KIND}.{ending}"[:255], len(sheet), hashlib.sha256(sheet).hexdigest()),
    )
