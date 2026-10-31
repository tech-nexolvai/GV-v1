"""GV's project schedule: the should-be values the vendor's sheets are checked against (plan
Phase 4 "should-be inputs", 2026-11-01).

Until now the page reviewer knew only the vendor's own drawings (and the architect's set, when it
was uploaded). GV often has the project's own lists besides: a finish schedule or material legend
(`ST-02  Quartz, GVQZ-19 Pure White`) and an item list (item number, description, the countertop's
own item number, room, and who supplies it: by GV or by others). One study measured +15 points on
dimension-compliance questions when the should-be values were in the model's context, and some of
GV's comments cannot be reached without them at all (an item number printed on no sheet).

**The upload.** A new upload kind, "the project schedule" (`SCHEDULE_WORDS`; the document kind
`schedule` the API already had): a spreadsheet (.xlsx, .csv) or a PDF table. It is never one of
the drawings: `chat_service._sync_uploads` copies it into the chat's own `schedules/` folder with
its own listing (`MANIFEST`), so the set pack, the page map and every reader of `uploads.json` stay
as they were (`sync`).

**Reading (code, $0).** Every table of the file (`read_tables`: openpyxl for a workbook, the csv
module for a CSV, pdfplumber's ruled then text strategy for a PDF, each loaded by name), then the
header row decides what each column holds (`ROLES`) and what the table is (`rows_of`):

- a **finish table** (a tag column: TAG, CODE, MARK, FINISH CODE...): tag -> description (the
  material, finish, colour, manufacturer and description cells together), and who supplies it;
- an **item table** (an ITEM column): item number -> description, countertop item number, room or
  area, scope words ("by GV", "by others", N.I.C.) read as a party (`party`), finish tags.

**Facts for each page** (`check`, through `SetContext.facts_for` and `view_questions`):

- for each finish tag the page prints, the schedule's description;
- for the page's item numbers, the schedule's description, countertop number, room and scope;
- where the page and the schedule differ (`Mismatch`), each a please-check fact and one of code's
  numbered questions (Qn) the reviewer must settle: a vendor material-table row whose description
  differs from the schedule's for the same tag (D); a finish tag of a scheduled family the schedule
  does not list (D); a title-block description unlike the schedule's (F); a countertop number the
  schedule gives that no sheet prints, or that the page prints differently (F); an item the
  schedule says is by others (E); an item the schedule lists that no sheet prints, on a page that
  prints its room (F: "the item number of a scope item printed on no sheet").

**Red (`SHOULD_BE_RED`, off).** One case is the textbook "the vendor says X, the governing document
says Y": a vendor material-table row (code read it from the drawing's text layer,
`schedule_index`) and the schedule's row for the same tag (code read the cell), whose product codes
share none (`agree` on codes only; a difference in words alone stays a please-check). With the
switch on, `add_reds` adds it through the finding book as a code finding (`created_by:
"project_schedule"`, `source: "project_schedule"`, `status: "different"`, `paired_by: "code"`,
`text_proof`), which the trust rule counts as the `text` witness (`chat_trust._text_made`) and the
proof groups make red only once its own kind (`check_record` kind `schedule`) has a clean record;
until then it is a Likely problem. Off until a measured run.

Plain code: no model call. Verification: `tests/review/assistant/test_project_schedule.py`
(made-up spreadsheets and PDF tables).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import re
import shutil
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant.set_context import TAG, family_of, item_key, scope_notes, tags_in

if TYPE_CHECKING:
    from app.review.assistant.schedule_index import Row
    from app.review.assistant.set_context import SetContext
    from app.review.assistant.view_questions import Question

__all__ = [
    "CREATED_BY",
    "MANIFEST",
    "SCHEDULE_WORDS",
    "SHOULD_BE_RED",
    "SOURCE",
    "FinishEntry",
    "ItemEntry",
    "Mismatch",
    "ProjectSchedule",
    "add_reds",
    "check",
    "facts",
    "is_schedule",
    "load",
    "party",
    "question_list",
    "read_bytes",
    "read_tables",
    "red_findings",
    "rows_of",
    "sync",
]

_log = logging.getLogger(__name__)

#: The upload kind's words in the chat's listing (`assistant_chats._KIND_WORDS["schedule"]`).
SCHEDULE_WORDS: Final = (
    "the project schedule (GV's finish schedule, material legend or item list; it governs)"
)
_SCHEDULE_LEAD: Final = "the project schedule"

#: The code-proved red path (see the module's docstring). Off until a measured run.
SHOULD_BE_RED: bool = False
#: Who made a red finding (the chat finding's `created_by`), and its source (what governs).
CREATED_BY: Final = "project_schedule"
SOURCE: Final = "project_schedule"
#: The question's `made_by` (`view_questions.Question`).
MADE_BY: Final = "schedule"

#: The chat folder's copy of the schedules and its listing.
FOLDER: Final = "schedules"
MANIFEST: Final = "schedules.json"

#: Reading bounds: sheets of a workbook, rows of a table, pages of a PDF.
MAX_SHEETS: Final = 20
MAX_ROWS: Final = 2000
MAX_PDF_PAGES: Final = 40
#: A header row is looked for in a table's first rows only, and its cells are short.
HEADER_SEARCH_ROWS: Final = 15
HEADER_CHARS: Final = 40
#: What one page's facts list at most.
MAX_FINISH_LINES: Final = 20
MAX_ITEM_LINES: Final = 8
MAX_UNPRINTED_LISTED: Final = 8
MAX_UNPRINTED_PER_PAGE: Final = 3
MAX_MISMATCHES: Final = 12
_SHOWN: Final = 80

Box = tuple[float, float, float, float]

#: What a header cell says its column holds, the more specific first. A countertop column before
#: an item column ("COUNTERTOP ITEM NO."), a finish code before a finish ("FINISH CODE").
ROLES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (role, re.compile(pattern, re.IGNORECASE))
    for role, pattern in (
        (
            "countertop",
            (
                r"\bCOUNTER\s*-?\s*TOPS?\b|\bC/T\b|\bCT\s*(?:NO|#|ITEM|NUMBER)|^TOPS?$|"
                r"\bTOP\s*(?:NO|#|ITEM|NUMBER)"
            ),
        ),
        ("tag", r"\b(?:FINISH|MATERIAL|MAT'?L)\s*(?:TAG|CODE|NO\.?|#|KEY|MARK|ID)\b"),
        ("description", r"\bITEM\s*DESC"),
        ("item", r"\bITEMS?\b"),
        ("scope", r"\bSCOPE\b|\b(?:SUPPLIED|PROVIDED|FURNISHED|INSTALLED)\s*BY\b|^BY$|\bRESPONSIB"),
        ("room", r"\bROOMS?\b|\bAREAS?\b|\bLOCATIONS?\b|\bSPACES?\b"),
        ("tag", r"^(?:TAGS?|CODES?|MARKS?|SYMBOLS?|KEY|ID|NO\.?|#)$"),
        ("finish_words", r"\bMATERIALS?\b|\bFINISH(?:ES)?\b"),
        (
            "description",
            (
                r"\bDESC(?:RIPTION)?\b|\bNAME\b|\bPRODUCT\b|\bMANUFACTURER\b|\bMFR\b|"
                r"\bCOLOU?R\b|\bPATTERN\b|\bMODEL\b|\bSTYLE\b|\bSERIES\b|\bTYPE\b|\bREMARKS?\b"
            ),
        ),
    )
)
#: Who supplies it, as a schedule's scope cell says it.
_GV: Final = re.compile(r"\bG\.?\s?V\.?\b|\bGRANITI\b", re.IGNORECASE)
_OTHERS: Final = re.compile(
    r"\bOTHERS?\b|\bG\.?\s?C\.?\b|\bOWNER\b|\bN\.?\s?I\.?\s?C\.?\b|\bNOT IN CONTRACT\b|\bOFCI\b|"
    r"\bOFOI\b|\bGENERAL CONTRACTOR\b|\bEXISTING\b",
    re.IGNORECASE,
)
#: A tag spelled one way (`ST-02`, `ST-2`, `ST02`: one).
_PARTS: Final = re.compile(r"^([A-Z]{1,5})-?(\d{1,3})([A-Z]?)(\.[A-Z]{1,3})?$")
#: A countertop's own item number: the item's number with a T, CT or .CT (`BOH-801T`, `ML-018.CT`).
_TOP_NUMBER: Final = re.compile(r"(?:\d)(?:C?T|\.C?T|T\.[A-Z]{1,3}|\.T)$")
#: A cell that says "none" (no countertop, no scope).
_NONE: Final = re.compile(r"^(?:N/?A|NONE|-+|—|–|0)$", re.IGNORECASE)


# ---- the upload ----------------------------------------------------------------------------------


def is_schedule(kind: object) -> bool:
    """An upload of the project schedule (its kind's words, `SCHEDULE_WORDS`)."""
    return str(kind or "").strip().lower().startswith(_SCHEDULE_LEAD)


def _file_name(name: str | None, index: int, taken: Sequence[str]) -> str:
    """The uploaded name as a plain file name (no folder, no leading dot, no odd character)."""
    base = Path(str(name or "")).name.strip()
    cleaned = re.sub(r"[^A-Za-z0-9._ ()-]", "_", base).lstrip(".").strip()[:120]
    cleaned = cleaned or f"schedule-{index}"
    while cleaned in taken:
        cleaned = f"{index}-{cleaned}"
    return cleaned


def sync(root: Path, sources: Sequence[Any]) -> list[dict[str, Any]]:
    """The project schedules among `sources` (`chat_service.UploadSource`: name, sha256, kind,
    read) copied into `root / FOLDER`, listed in `root / MANIFEST` (rewritten only when they
    changed). Returns the listing. A file whose bytes do not match its hash is refused."""
    folder, manifest = root / FOLDER, root / MANIFEST
    wanted: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        name = _file_name(source.name, index, [w["name"] for w in wanted])
        wanted.append({"name": name, "sha256": source.sha256, "kind": source.kind})
    try:
        current = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    except (OSError, ValueError):
        current = None
    if current == wanted and all((folder / w["name"]).is_file() for w in wanted):
        return wanted
    if folder.exists():
        shutil.rmtree(folder)
    if not wanted:
        manifest.unlink(missing_ok=True)
        return []
    folder.mkdir(parents=True, exist_ok=True)
    for item, source in zip(wanted, sources, strict=True):
        data = source.read()
        if hashlib.sha256(data).hexdigest() != source.sha256:
            raise ValueError("a stored schedule does not match its recorded hash")
        path = folder / item["name"]
        path.write_bytes(data)
        path.chmod(0o444)
    manifest.write_text(json.dumps(wanted, ensure_ascii=False), encoding="utf-8")
    return wanted


# ---- reading the file's tables -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grid:
    """One table as rows of cell texts, and where it is in the file."""

    file: str
    place: str
    """`sheet Finishes`, `page 2`, or empty (a CSV)."""
    rows: tuple[tuple[str, ...], ...]


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def _cell_text(value: object) -> str:
    """A workbook cell as text: a whole number without `.0`, a date as a date."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return _clean(value)


def _xlsx(data: bytes, name: str) -> list[Grid]:
    openpyxl = import_module("openpyxl")
    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        grids: list[Grid] = []
        for sheet in book.worksheets[:MAX_SHEETS]:
            rows = [
                tuple(_cell_text(v) for v in raw)
                for raw in sheet.iter_rows(values_only=True, max_row=MAX_ROWS)
            ]
            grids.append(Grid(name, f"sheet {sheet.title}", tuple(rows)))
        return grids
    finally:
        book.close()


def _csv(data: bytes, name: str) -> list[Grid]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [tuple(_clean(c) for c in row) for row in csv.reader(io.StringIO(text), dialect)]
    return [Grid(name, "", tuple(rows[:MAX_ROWS]))]


#: pdfplumber's settings: a schedule's ruled cells first, else its columns of text.
_LINES: Final = {"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3}
_TEXT: Final = {"vertical_strategy": "text", "horizontal_strategy": "text"}


def _pdf(data: bytes, name: str) -> list[Grid]:
    pdfplumber = import_module("pdfplumber")
    grids: list[Grid] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages[:MAX_PDF_PAGES], start=1):
            tables = [t for t in page.extract_tables(_LINES) if len(t) >= 2]
            if not tables:
                tables = [t for t in page.extract_tables(_TEXT) if len(t) >= 2]
            for table in tables:
                rows = tuple(tuple(_clean(c) for c in row) for row in table[:MAX_ROWS])
                grids.append(Grid(name, f"page {number}", rows))
    return grids


def read_tables(data: bytes, name: str) -> list[Grid]:
    """Every table of one schedule file, by what its bytes are (never by its name's extension: the
    chat's copy may be named `.pdf`): a PDF, a workbook (a zip), else text read as CSV."""
    if data[:5] == b"%PDF-":
        return _pdf(data, name)
    if data[:4] == b"PK\x03\x04":
        return _xlsx(data, name)
    return _csv(data, name)


# ---- what the rows say ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Where:
    """Where a row is in the schedule (for the facts: GV can find it)."""

    file: str
    place: str
    row: int

    def words(self) -> str:
        return ", ".join(p for p in (self.file, self.place, f"row {self.row}") if p)


@dataclass(frozen=True, slots=True)
class FinishEntry:
    tag: str
    description: str
    scope: str
    where: Where


@dataclass(frozen=True, slots=True)
class ItemEntry:
    item: str
    description: str
    countertop: str | None
    room: str
    scope: str
    party: str | None
    """`GV`, `others`, or `None` (not said)."""
    finishes: tuple[str, ...]
    where: Where


def canon(tag: str) -> str:
    """A tag or item number spelled one way: `ST-02`, `ST-2` and `st02` are `ST-2`."""
    text = re.sub(r"\s+", "", str(tag or "")).upper()
    found = _PARTS.match(text)
    if found is None:
        return text
    return f"{found.group(1)}-{int(found.group(2))}{found.group(3)}{found.group(4) or ''}"


def _finish_tag(text: str) -> str | None:
    """The cell as a finish tag (the whole cell; a family of two letters or more)."""
    cleaned = _clean(text).upper().strip(" :;,")
    if not cleaned or not TAG.fullmatch(cleaned):
        return None
    family = family_of(cleaned)
    return cleaned if family is not None and len(family) >= 2 else None


def _item_number(text: str) -> str | None:
    """The cell's item number: its first tag (`BOH-802`, `GRT-804T`), else a short code with a digit
    (`101A`), else `None`."""
    cleaned = _clean(text).upper()
    if not cleaned or _NONE.match(cleaned):
        return None
    found = TAG.search(cleaned)
    if found is not None:
        return found.group(0)
    if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-/]{0,19}", cleaned) and re.search(r"\d", cleaned):
        return cleaned
    return None


def party(text: str) -> str | None:
    """Who a scope cell says supplies it: `GV`, `others`, or `None` (neither named, or both:
    "GV / others" is not decided here)."""
    gv, others = bool(_GV.search(text)), bool(_OTHERS.search(text))
    if gv == others:
        return None
    return "GV" if gv else "others"


def _roles(row: Sequence[str]) -> dict[int, str]:
    """A header row's columns and what each holds (`ROLES`); empty when it is no header."""
    out: dict[int, str] = {}
    for n, cell in enumerate(row):
        text = _clean(cell)
        if not text or len(text) > HEADER_CHARS or _finish_tag(text) or re.search(r"\d{2}", text):
            continue
        role = next((r for r, pattern in ROLES if pattern.search(text)), None)
        if role is not None:
            out[n] = role
    kinds = set(out.values())
    if ("item" in kinds or "tag" in kinds) and len(kinds) >= 2:
        return out
    return {}


def _cells(row: Sequence[str], roles: Mapping[int, str], *wanted: str) -> list[str]:
    return [_clean(row[n]) for n, r in roles.items() if r in wanted and n < len(row) and row[n]]


def rows_of(grid: Grid) -> tuple[list[FinishEntry], list[ItemEntry]]:
    """The finish and item rows of one table. A header row (in the first `HEADER_SEARCH_ROWS`, or
    again further down: two tables on one sheet) decides the columns; a row whose key cell is not a
    tag or an item number is skipped (a title, a blank, a note)."""
    finishes: list[FinishEntry] = []
    items: list[ItemEntry] = []
    roles: dict[int, str] = {}
    for number, row in enumerate(grid.rows, start=1):
        found = _roles(row) if (number <= HEADER_SEARCH_ROWS or roles) else {}
        if found:
            roles = found
            continue
        if not roles:
            continue
        where = Where(grid.file, grid.place, number)
        kinds = set(roles.values())
        if "item" in kinds or "countertop" in kinds:
            # An item list: its ITEM column, else (a MARK column beside a countertop column) its tag
            # column holds the item numbers.
            key = next((n for n, r in roles.items() if r == "item"), -1)
            if key < 0:
                key = next((n for n, r in roles.items() if r == "tag"), -1)
            item = _item_number(row[key]) if 0 <= key < len(row) else None
            if item is None:
                continue
            tops = _cells(row, roles, "countertop")
            top = next((t for c in tops if (t := _item_number(c)) is not None), None)
            scope = "; ".join(_cells(row, roles, "scope"))
            kept = ("finish_words",) if "item" in kinds else ()
            tagged = [t for c in _cells(row, roles, "tag", *kept) for t in tags_in(c.upper())]
            items.append(
                ItemEntry(
                    item=item,
                    description="; ".join(_cells(row, roles, "description")),
                    countertop=top,
                    room="; ".join(_cells(row, roles, "room")),
                    scope=scope,
                    party=party(scope) if scope else None,
                    finishes=tuple(dict.fromkeys(t for t in tagged if family_of(t))),
                    where=where,
                )
            )
            continue
        key = next((n for n, r in roles.items() if r == "tag"), -1)
        tag = _finish_tag(row[key]) if 0 <= key < len(row) else None
        if tag is None:
            continue
        words = [
            c for c in _cells(row, roles, "finish_words", "description") if _finish_tag(c) != tag
        ]
        if words:
            finishes.append(
                FinishEntry(tag, "; ".join(words), "; ".join(_cells(row, roles, "scope")), where)
            )
    return finishes, items


@dataclass(slots=True)
class ProjectSchedule:
    """Every finish and item row GV's schedules list (`read_bytes`), each tag and item once (its
    first row; a later row saying something else is a `conflicts` line, told, never decided)."""

    files: list[str] = field(default_factory=list)
    finishes: dict[str, FinishEntry] = field(default_factory=dict)
    """By `canon(tag)`."""
    items: dict[str, ItemEntry] = field(default_factory=dict)
    """By `canon(item)`."""
    conflicts: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)

    def add(self, grid: Grid) -> None:
        from app.review.assistant.schedule_index import agree

        finishes, items = rows_of(grid)
        for entry in finishes:
            known = self.finishes.setdefault(canon(entry.tag), entry)
            if known is not entry and not agree(known.description, entry.description):
                self.conflicts.append(
                    f"{entry.tag}: {_said(known.description)} ({known.where.words()}) and "
                    f"{_said(entry.description)} ({entry.where.words()})"
                )
        for item in items:
            known_item = self.items.setdefault(canon(item.item), item)
            if known_item is not item and known_item.description != item.description:
                self.conflicts.append(
                    f"{item.item}: {_said(known_item.description)} "
                    f"({known_item.where.words()}) and {_said(item.description)} "
                    f"({item.where.words()})"
                )

    def finish(self, tag: str) -> FinishEntry | None:
        return self.finishes.get(canon(tag))

    def families(self) -> set[str]:
        return {f for e in self.finishes.values() if (f := family_of(e.tag)) is not None}

    def item_for(self, tag: str) -> ItemEntry | None:
        """The item row a printed tag is: its item number or its countertop number, spelled the
        same, else the same item (`item_key`: `BOH-802.C` is item `BOH-802`'s cabinet sheet)."""
        wanted = canon(tag)
        for entry in self.items.values():
            if wanted in (canon(entry.item), canon(entry.countertop or "")):
                return entry
        key = item_key(tag)
        if key is None:
            return None
        return next(
            (
                e
                for e in self.items.values()
                if item_key(e.item) == key and family_of(e.item) == family_of(tag)
            ),
            None,
        )

    def __bool__(self) -> bool:
        return bool(self.finishes or self.items)


def read_bytes(files: Iterable[tuple[str, bytes]]) -> ProjectSchedule:
    """The schedule from its files' bytes (a file that cannot be read is listed in `unread`)."""
    schedule = ProjectSchedule()
    for name, data in files:
        schedule.files.append(name)
        try:
            grids = read_tables(data, name)
        except Exception as failed:  # noqa: BLE001 - the other files still count
            _log.warning("project schedule: %s not read (%s)", name, type(failed).__name__)
            schedule.unread.append(name)
            continue
        for grid in grids:
            schedule.add(grid)
    return schedule


_CACHE: dict[str, ProjectSchedule] = {}
_CACHE_LOCK: Final = threading.Lock()
_MAX_CACHED: Final = 32


def load(root: Path) -> ProjectSchedule | None:
    """The chat's project schedule (`sync` copied it into `root`), read once per set of files;
    `None` when there is none or nothing in it could be read."""
    try:
        listing = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(listing, list) or not listing:
        return None
    key = ",".join(str(item.get("sha256")) for item in listing if isinstance(item, Mapping))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached or None
    files: list[tuple[str, bytes]] = []
    for item in listing:
        name = str(item.get("name") or "") if isinstance(item, Mapping) else ""
        try:
            files.append((name, (root / FOLDER / Path(name).name).read_bytes()))
        except OSError:
            _log.warning("project schedule: a listed file is missing")
    schedule = read_bytes(files)
    with _CACHE_LOCK:
        if len(_CACHE) >= _MAX_CACHED:
            _CACHE.clear()
        _CACHE[key] = schedule
    if schedule:
        _log.warning(
            "project schedule: %d file(s), %d finish row(s), %d item row(s), %d unread",
            len(schedule.files),
            len(schedule.finishes),
            len(schedule.items),
            len(schedule.unread),
        )
    return schedule or None


# ---- one page against the schedule ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mismatch:
    """Where a page and the schedule differ: a please-check fact and one of code's questions."""

    what: str
    """`table_row`, `unscheduled_tag`, `title_description`, `countertop_unprinted`,
    `countertop_differs`, `scope_others`, `unprinted_item`."""
    kind: str
    """The reviewer's kind: D finish, F labels (a title block, an item number), E scope."""
    text: str
    boxes: tuple[Box, ...]
    tag: str
    vendor: str
    should_be: str
    where: Where
    proof: Mapping[str, Any] | None = None
    """Only on a red-ready mismatch: both sides code-read, their product codes share none."""


def _said(text: str) -> str:
    shown = text if len(text) <= _SHOWN else text[: _SHOWN - 1] + "…"
    return f"'{shown}'"


def _box(raw: object) -> Box | None:
    if isinstance(raw, Mapping):
        raw = [raw.get(k) for k in ("x0", "y0", "x1", "y1")]
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1)


def _words(text: str) -> set[str]:
    from app.review.assistant.schedule_index import _CODE, _STOP

    plain = _CODE.sub(" ", text)
    return {w for w in re.findall(r"[a-z]{3,}", plain.lower()) if w not in _STOP}


def _alike(a: str, b: str) -> bool:
    """Two descriptions of one thing: they share a product code when both print one, else at least
    half of the shorter one's words are in the other (a title block says less than a schedule)."""
    from app.review.assistant.schedule_index import codes

    ca, cb = codes(a), codes(b)
    if ca and cb:
        return bool(ca & cb)
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return True
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def _vendor_rows(context: SetContext, page: int) -> list[Row]:
    """The page's material-table rows and labelled cells, as `schedule_index` read them."""
    from app.review.assistant import schedule_index

    index = context.schedule
    if index is not None:
        return list(index.rows.get(page, []))
    phrases = context.pages.get(page, [])
    rows = [r for t in schedule_index.table_rows(page, phrases) for r in t.rows]
    return rows + schedule_index.labelled_cells(page, phrases)


def _printed_anywhere(context: SetContext, tag: str) -> bool:
    wanted = canon(tag)
    return any(canon(t) == wanted for t in context.tags)


def _occurrences(context: SetContext, page: int, tag: str) -> list[Box]:
    return [b for raw in context.occurrences(page, tag)[:3] if (b := _box(raw)) is not None]


def _room_spots(phrases: Sequence[Mapping[str, Any]], room: str) -> list[Box]:
    """Where a page prints the room's name (a whole-word match, 3 letters or more)."""
    name = _clean(room)
    if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
        return []
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE)
    return [
        b for p in phrases if pattern.search(_clean(p.get("text"))) and (b := _box(p.get("box")))
    ][:2]


def _red_proof(row: Row, entry: FinishEntry) -> Mapping[str, Any] | None:
    """The proof of a red: the vendor's row and the schedule's both print product codes and share
    none (words alone never prove it)."""
    from app.review.assistant.schedule_index import codes

    theirs, ours = codes(row.description), codes(entry.description)
    if not theirs or not ours or theirs & ours:
        return None
    return {
        "kind": "schedule",
        "tag": row.tag,
        "schedule": {
            "file": entry.where.file,
            "place": entry.where.place,
            "row": entry.where.row,
            "text": entry.description,
            "codes": sorted(ours),
        },
        "vendor": {
            "page": row.page,
            "text": row.description,
            "codes": sorted(theirs),
            "source": row.source,
        },
        "rule": "the vendor's table row and GV's project schedule give the same tag product "
        "codes that share none; both read by code from text",
    }


def _finish_part(
    schedule: ProjectSchedule, context: SetContext, page: int, printed: Sequence[str]
) -> tuple[list[str], list[Mismatch]]:
    lines: list[str] = []
    found: list[Mismatch] = []
    families = schedule.families()
    for tag in printed:
        entry = schedule.finish(tag)
        if entry is not None:
            scope = f"; scope {_said(entry.scope)}" if entry.scope else ""
            lines.append(f"  {tag}: {_said(entry.description)}{scope} ({entry.where.words()})")
            continue
        family = family_of(tag)
        if family in families and schedule.item_for(tag) is None:
            listed = sorted(e.tag for e in schedule.finishes.values() if family_of(e.tag) == family)
            found.append(
                Mismatch(
                    "unscheduled_tag",
                    "D",
                    f"This page prints {tag}; the project schedule lists the {family} family as "
                    f"{', '.join(listed[:8])} and not {tag} (a wrong code, or a finish missing "
                    "from the schedule: please check).",
                    tuple(_occurrences(context, page, tag)),
                    tag,
                    tag,
                    ", ".join(listed[:8]),
                    Where(", ".join(schedule.files), "", 0),
                )
            )
    from app.review.assistant.schedule_index import agree

    for row in _vendor_rows(context, page):
        entry = schedule.finish(row.tag)
        if entry is None:
            continue
        if agree(row.description, entry.description):
            continue
        boxes = (row.box,) + ((row.text_box,) if row.text_box else ())
        found.append(
            Mismatch(
                "table_row",
                "D",
                f"This page's material {'cell' if row.source == 'cell' else 'table row'} "
                f"{row.tag} {_said(row.description)}; the project schedule ({entry.where.words()})"
                f" says {entry.tag} is {_said(entry.description)}. The schedule governs (please "
                "check).",
                boxes,
                row.tag,
                row.description,
                entry.description,
                entry.where,
                _red_proof(row, entry),
            )
        )
    return lines[:MAX_FINISH_LINES], found


def _item_part(
    schedule: ProjectSchedule, context: SetContext, page: int, printed: Sequence[str]
) -> tuple[list[str], list[Mismatch]]:
    from app.review.assistant import title_block

    phrases = context.pages.get(page, [])
    cells = {c.name: c for c in title_block.cells(phrases)}
    entries: dict[str, tuple[ItemEntry, str]] = {}
    for tag in printed:
        if schedule.finish(tag) is not None:
            continue
        entry = schedule.item_for(tag)
        if entry is not None:
            entries.setdefault(canon(entry.item), (entry, tag))
    lines: list[str] = []
    found: list[Mismatch] = []
    notes = [party(note) for note in scope_notes(phrases)]
    for entry, tag in list(entries.values())[:MAX_ITEM_LINES]:
        bits = [f"description {_said(entry.description)}" if entry.description else ""]
        bits.append(f"countertop item {entry.countertop}" if entry.countertop else "")
        bits.append(f"room {_said(entry.room)}" if entry.room else "")
        bits.append(f"scope {_said(entry.scope)}" if entry.scope else "")
        bits.append(f"finishes {', '.join(entry.finishes)}" if entry.finishes else "")
        said = "; ".join(b for b in bits if b) or "no other cell"
        lines.append(f"  Item {entry.item} (printed here as {tag}): {said} ({entry.where.words()})")
        spots = tuple(_occurrences(context, page, tag))
        item_cell = cells.get("item")
        described = cells.get("description")
        if (
            described is not None
            and entry.description
            and not _alike(described.value, entry.description)
        ):
            found.append(
                Mismatch(
                    "title_description",
                    "F",
                    f"The title block's description {_said(described.value)}; the project "
                    f"schedule ({entry.where.words()}) describes item {entry.item} as "
                    f"{_said(entry.description)} (please check).",
                    (described.box,),
                    entry.item,
                    described.value,
                    entry.description,
                    entry.where,
                )
            )
        top = entry.countertop
        if top:
            where_cell = (item_cell.box,) if item_cell is not None else spots
            if not _printed_anywhere(context, top):
                found.append(
                    Mismatch(
                        "countertop_unprinted",
                        "F",
                        f"The project schedule ({entry.where.words()}) gives item {entry.item} "
                        f"the countertop item number {top}; no sheet of the set prints {top} "
                        "(the title block's item cell or the top's callout should name it: please "
                        "check).",
                        tuple(where_cell),
                        entry.item,
                        "not printed",
                        top,
                        entry.where,
                    )
                )
            else:
                odd = [
                    t
                    for t in printed
                    if item_key(t) == item_key(entry.item)
                    and canon(t) not in (canon(entry.item), canon(top))
                    and _TOP_NUMBER.search(t)
                ]
                for t in odd[:1]:
                    found.append(
                        Mismatch(
                            "countertop_differs",
                            "F",
                            f"This page prints {t} as the top's number; the project schedule "
                            f"({entry.where.words()}) numbers item {entry.item}'s countertop "
                            f"{top} (please check).",
                            tuple(_occurrences(context, page, t)),
                            t,
                            t,
                            top,
                            entry.where,
                        )
                    )
        if entry.party == "others" and "others" not in notes:
            found.append(
                Mismatch(
                    "scope_others",
                    "E",
                    f"The project schedule ({entry.where.words()}) says item {entry.item} is "
                    f"supplied by others ({_said(entry.scope)}); this sheet draws it with no "
                    "'by others' note (in GV's scope, or to be marked by others: please check).",
                    spots,
                    entry.item,
                    "no 'by others' note",
                    entry.scope,
                    entry.where,
                )
            )
    return lines, found


def _item_printed(context: SetContext, entry: ItemEntry) -> bool:
    """A sheet prints the item: its number in any of its drawings' spellings (`item_key`:
    `ABC-102.C` is item `ABC-102`'s cabinet sheet), or its countertop number."""
    key, family = item_key(entry.item), family_of(entry.item)
    return any(
        (key is not None and item_key(t) == key and family_of(t) == family) for t in context.tags
    ) or bool(entry.countertop and _printed_anywhere(context, entry.countertop))


def _unprinted(schedule: ProjectSchedule, context: SetContext) -> list[ItemEntry]:
    """The schedule's items (not by others) that no sheet prints (`_item_printed`)."""
    return [
        e
        for e in schedule.items.values()
        if e.party != "others"
        and TAG.fullmatch(e.item)
        and not _printed_anywhere(context, e.item)
        and not _item_printed(context, e)
    ]


def check(schedule: ProjectSchedule, context: SetContext, page: int) -> tuple[list[str], list[Mismatch]]:  # fmt: skip
    """`page` against the schedule: the fact lines for its reviewer and where they differ
    (`Mismatch`, at most `MAX_MISMATCHES`). Nothing when the page touches nothing in it."""
    context.build()
    printed = context.tags_on(page)
    finish_lines, found = _finish_part(schedule, context, page, printed)
    item_lines, item_found = _item_part(schedule, context, page, printed)
    found += item_found
    lines: list[str] = []
    if finish_lines:
        lines.append("Finish tags this page prints, as the project schedule describes them:")
        lines += finish_lines
    if item_lines:
        lines.append("This page's items in the project schedule:")
        lines += item_lines
    unprinted = _unprinted(schedule, context)
    phrases = context.pages.get(page, [])
    near = 0
    for entry in unprinted:
        spots = _room_spots(phrases, entry.room) if entry.room else []
        if not spots or near >= MAX_UNPRINTED_PER_PAGE:
            continue
        near += 1
        found.append(
            Mismatch(
                "unprinted_item",
                "F",
                f"The project schedule ({entry.where.words()}) lists item {entry.item} "
                f"{_said(entry.description)} in {_said(entry.room)}"
                + (f", {_said(entry.scope)}" if entry.scope else "")
                + f", and no sheet of the set prints {entry.item}. This page prints "
                f"{_said(entry.room)}: if it draws this item, its item number is missing (please "
                "check).",
                tuple(spots),
                entry.item,
                "not printed",
                entry.item,
                entry.where,
            )
        )
    if unprinted:
        shown = "; ".join(
            f"{e.item} {_said(e.description)}" + (f" ({_said(e.room)})" if e.room else "")
            for e in unprinted[:MAX_UNPRINTED_LISTED]
        )
        more = len(unprinted) - MAX_UNPRINTED_LISTED
        lines.append(
            f"Schedule items no sheet of the set prints: {shown}"
            + (f" (+{more} more)" if more > 0 else "")
            + "."
        )
    found = found[:MAX_MISMATCHES]
    if found:
        lines.append("Where this page and the project schedule differ (please check each):")
        lines += [f"  {m.text}" for m in found]
    if not lines:
        return [], []
    head = (
        f"GV's project schedule ({', '.join(schedule.files)}; code read {len(schedule.finishes)} "
        f"finish row(s) and {len(schedule.items)} item row(s)). It is the should-be list and "
        "governs over the vendor's sheets:"
    )
    tail = [f"The schedule itself differs: {c}." for c in schedule.conflicts[:3]]
    return [head, *lines, *tail], found


def facts(schedule: ProjectSchedule | None, context: SetContext, page: int) -> list[str]:
    """The schedule's fact lines for `page` (`SetContext.facts_for`); none without a schedule."""
    if not schedule:
        return []
    lines, _found = check(schedule, context, page)
    return lines


def question_list(
    schedule: ProjectSchedule | None, context: SetContext, page: int, start: int, room: int
) -> list[Question]:
    """Code's questions from the page's mismatches (`view_questions.opening_blocks`), numbered on
    from `start`, at most `room`; a mismatch with no spot on the page is a fact only."""
    if not schedule or room <= 0:
        return []
    from app.review.assistant.view_questions import Question

    _lines, found = check(schedule, context, page)
    out: list[Question] = []
    for mismatch in found:
        if not mismatch.boxes or len(out) >= room:
            continue
        out.append(
            Question(start + len(out), mismatch.kind, mismatch.text, mismatch.boxes[:4], MADE_BY)
        )
    return out


# ---- the red path (off: `SHOULD_BE_RED`) ---------------------------------------------------------


def _box_dict(box: Sequence[float], page: int, file: str) -> dict[str, Any]:
    return {
        "page": page,
        "file": file,
        "x0": round(float(box[0]), 1),
        "y0": round(float(box[1]), 1),
        "x1": round(float(box[2]), 1),
        "y1": round(float(box[3]), 1),
    }


def red_findings(
    schedule: ProjectSchedule | None, context: SetContext, page: int
) -> list[dict[str, Any]]:
    """The code-proved reds of `page` (`Mismatch.proof`), as chat findings; none while
    `SHOULD_BE_RED` is off."""
    if not SHOULD_BE_RED or not schedule:
        return []
    _lines, found = check(schedule, context, page)
    out: list[dict[str, Any]] = []
    for m in found:
        if m.proof is None or not m.boxes:
            continue
        out.append(
            {
                "page": page,
                "item": f"Material table row {m.tag}: differs from GV's project schedule",
                "category": "other",
                "architect": m.should_be[:160],
                "vendor": m.vendor[:160],
                "status": "different",
                "why": (
                    f"The vendor's material table lists {m.tag} as {_said(m.vendor)}; GV's project "
                    f"schedule ({m.where.words()}) lists {m.tag} as {_said(m.should_be)}. Both "
                    "were read by code from text, and their product codes share none."
                ),
                "verified": False,
                "source": SOURCE,
                "vendor_box": _box_dict(m.boxes[0], page, context.file),
                "paired_by": "code",
                "created_by": CREATED_BY,
                "text_proof": dict(m.proof),
            }
        )
    return out


def add_reds(engine: Any, context: SetContext, pages: Sequence[int]) -> int:
    """The engine's hook (`set_context.add_to_answer`, a full check's finish, $0): each picked
    page's code-proved reds added through the finding book, unless the answer already has one on
    that tag and page. Returns how many were added; 0 while `SHOULD_BE_RED` is off."""
    schedule = getattr(context, "project", None)
    if not SHOULD_BE_RED or not schedule:
        return 0
    book = engine.book
    added = 0
    with engine.finding_lock:
        live = book.live()
        for page in pages:
            for finding in red_findings(schedule, context, page):
                if any(
                    f.get("page") == page
                    and f.get("created_by") == CREATED_BY
                    and f.get("vendor") == finding["vendor"]
                    for f in live
                ):
                    continue
                stored = book.add_code_finding(finding, created_by=CREATED_BY)
                if stored is None:
                    continue
                engine._show_finding(stored)
                live.append(stored)
                added += 1
    return added
