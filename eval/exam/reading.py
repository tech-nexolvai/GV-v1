"""The reading exam: which labels it asks about, how each is cut out, and how its key is written (rebuild plan Step 1A).

The pairing and diff exams score what a run did with the numbers it read. This one scores the
reading itself: a code-cut picture of one printed label, and the exact text printed in it. Rebuild
Step 2 (the two-company reader for pen-stroke numbers) is kept only if it seals no wrong value
here (`reading_score`).

**What it asks about.** Architect numbers drawn as pen strokes that code did not read, sampled by
why code failed (`REASONS`, the reason `reading_coverage.json` gives first), each reason in
proportion to how often it happens with at least `MINIMUM` where that many exist; plus three
control groups (`CONTROLS`) of labels code did read: architect typed, vendor typed and vendor pen
strokes. The sample is deterministic: candidates are ordered by a hash of `SEED` and their own
place, so building it again from the same packs picks the same labels.

**How a label is cut** (`code_crop`): the label's box padded by E2's `padding_pt` and clipped short
of every neighbouring label or unread spot on the page by E2's clip gaps
(`extraction/slot_reader/runs.py`, `E2_CROP_SETTINGS`), never inside the label's own box. A reader
shown one label cannot copy another. A wider `context_box` is cut for the person who confirms it.

**What a key row says** (`KEY_COLUMNS`): where the label is (job tag, file, page, box in the PDF
points `dims.json` uses), why it is in the exam, its pictures, a proposed reading (text exactly as
printed, which turn reads upright, how sure), and `confirmed`, which a person fills: `yes` (the
proposal is the truth), a corrected text, or `unreadable`. `NOT_A_NUMBER` is the text for a spot
that prints no dimension at all.

**Client values.** A key holds printed values, file names and crops of the client's drawings, so it
is written only under `~/gv-local/exam/reading/`, never in this repository. This module reads no
PDF and draws nothing: `scripts/build_reading_exam.py` renders the crops.
"""

from __future__ import annotations

import csv
import hashlib
import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from extraction.slot_reader.runs import E2_CROP_SETTINGS

__all__ = [
    "CONFIDENCE",
    "CONTEXT_PAD_PT",
    "CONTROLS",
    "CONTROL_EACH",
    "KEY_COLUMNS",
    "MINIMUM",
    "NOT_A_NUMBER",
    "REASONS",
    "SEED",
    "STROKE_GROUP",
    "TOTAL",
    "Candidate",
    "KeyRow",
    "allocate",
    "box_text",
    "candidates_of_pack",
    "code_crop",
    "context_box",
    "index_html",
    "label_id",
    "parse_box",
    "read_key",
    "sample",
    "with_proposal",
    "write_key",
]

type Box = tuple[float, float, float, float]

#: Why code did not read an architect pen-stroke number, as `reading_coverage.json` names it: the
#: six the rebuild plan's measurement split the 2,398 unread Portage strokes by.
REASONS: Final = ("sideways", "stacked_fraction", "other", "lines_through", "mm_row", "crowded")
#: The control groups: labels code did read, so a reader's rate on them is a baseline.
CONTROLS: Final = ("architect_typed", "vendor_typed", "vendor_strokes")
#: The group of every sampled unread architect stroke.
STROKE_GROUP: Final = "architect_strokes"
#: About how many labels the exam holds, how many of each control group, and the fewest of each
#: reason where that many exist.
TOTAL: Final = 300
CONTROL_EACH: Final = 20
MINIMUM: Final = 15
#: The sample's seed: change it and a different sample is drawn.
SEED: Final = "gv-reading-exam/1"
#: How far past the label the person's context picture reaches (points).
CONTEXT_PAD_PT: Final = 24.0
#: The text of a spot that prints no dimension (a word, a tag, a symbol counted as a number).
NOT_A_NUMBER: Final = "[not a number]"
#: How sure a proposal is.
CONFIDENCE: Final = ("sure", "unsure", "unreadable")

#: E2's crop rules (27 agreed-right, 0 agreed-wrong), as points.
_PAD_PT: Final = float(E2_CROP_SETTINGS.padding_pt)
_GAP_X_PT: Final = float(E2_CROP_SETTINGS.clip_gap_x_pt)
_GAP_Y_PT: Final = float(E2_CROP_SETTINGS.clip_gap_y_pt)
#: The routes `dims.json` gives a label code read from the PDF's text, and from pen strokes.
_TYPED_ROUTES: Final = frozenset({"text"})
_STROKE_ROUTES: Final = frozenset({"drawn", "lettering"})
#: A label written along the page's height (or at an angle) reads upright only turned.
_SIDEWAYS_ORIENTATIONS: Final = frozenset({"v", "turned"})

#: The key's columns, in order: the ones the exam was specified with first, then what helps a
#: person confirm a row (local only, like the rest of the key).
KEY_COLUMNS: Final = (
    "id",
    "job",
    "file_index",
    "page",
    "side",
    "reason",
    "box",
    "crops",
    "proposed_text",
    "proposed_turn",
    "confidence",
    "confirmed",
    "group",
    "also",
    "file",
    "context",
    "code_text",
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One label the exam may ask about."""

    job: str
    file_index: int
    file: str
    page: int
    side: str
    group: str
    """`STROKE_GROUP`, or one of `CONTROLS`."""
    reason: str
    """For an unread stroke, the first of `REASONS` code gave; for a control, its group."""
    box: Box
    also: tuple[str, ...] = ()
    """The other reasons code gave, after the first."""
    code_text: str = ""
    """What code read, for a control; empty for an unread stroke."""
    sideways: bool = False
    """Written along the page's height: the two upright turns are cut too."""

    @property
    def id(self) -> str:
        return label_id(self.job, self.file_index, self.page, self.box)


@dataclass(slots=True)
class KeyRow:
    """One row of a reading exam's key, as `KEY_COLUMNS` writes it."""

    id: str
    job: str
    file_index: int
    page: int
    side: str
    reason: str
    box: Box
    crops: tuple[str, ...] = ()
    """Paths under the key's folder: the code-cut crop, then the 90 and 270 turns if cut."""
    proposed_text: str = ""
    proposed_turn: int | None = None
    confidence: str = ""
    confirmed: str = ""
    group: str = ""
    also: tuple[str, ...] = ()
    file: str = ""
    context: str = ""
    code_text: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def label_id(job: str, file_index: int, page: int, box: Sequence[float]) -> str:
    """A label's id: a short hash of where it is, the same on every build."""
    digest = hashlib.sha256(f"{job}|{file_index}|{page}|{box_text(box)}".encode()).hexdigest()
    return f"rd-{digest[:10]}"


def box_text(box: Sequence[float]) -> str:
    """A box as the key writes it: `x0,y0,x1,y1`, points to two places."""
    return ",".join(f"{float(value):.2f}" for value in box)


def parse_box(text: str) -> Box:
    """A box from `box_text` (or any four comma-separated numbers)."""
    parts = [float(part) for part in text.replace(" ", "").strip("[]").split(",")]
    if len(parts) != 4:
        raise ValueError(f"a box has four numbers, not {len(parts)}: {text!r}")
    return parts[0], parts[1], parts[2], parts[3]


def _box(value: object) -> Box | None:
    """Four numbers as a box, or `None`. A box with no width or no height (one straight stroke,
    such as a `1`) is kept, as the coverage counts it; one with neither is not."""
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x1 < x0 or y1 < y0 or (x1 == x0 and y1 == y0):
        return None
    return (x0, y0, x1, y1)


def candidates_of_pack(
    job: str, dims: Mapping[str, Any], coverage: Mapping[str, Any]
) -> tuple[list[Candidate], dict[tuple[int, int], list[Box]]]:
    """Every label of one set pack the exam may ask about, and every page's boxes (labels and
    unread spots alike, keyed by file index and page) for `code_crop` to stay clear of.

    `dims` is the pack's `dims.json`, `coverage` its `reading_coverage.json`. Unread strokes are
    the architect's whose first reason is one of `REASONS`; controls are labels code read and did
    not work out itself (`derived`): typed (`text` route) on either side, and the vendor's strokes.
    """
    files = [str(name) for name in dims.get("files") or []]
    index_of = {name: index for index, name in enumerate(files)}
    out: list[Candidate] = []
    boxes: dict[tuple[int, int], list[Box]] = {}
    for page in dims.get("pages") or []:
        name, number = str(page.get("file")), page.get("page")
        if name not in index_of or not isinstance(number, int):
            continue
        where = (index_of[name], number)
        for label in page.get("labels") or []:
            box = _box(label.get("box"))
            if box is None:
                continue
            boxes.setdefault(where, []).append(box)
            if label.get("derived"):
                continue
            side, route = str(label.get("side") or ""), str(label.get("route") or "")
            group = None
            if route in _TYPED_ROUTES and side in {"architect", "vendor"}:
                group = f"{side}_typed"
            elif route in _STROKE_ROUTES and side == "vendor":
                group = "vendor_strokes"
            if group is None:
                continue
            out.append(
                Candidate(
                    job=job,
                    file_index=where[0],
                    file=name,
                    page=number,
                    side=side,
                    group=group,
                    reason=group,
                    box=box,
                    code_text=str(label.get("text") or ""),
                    sideways=str(label.get("orientation") or "") in _SIDEWAYS_ORIENTATIONS,
                )
            )
    for page in coverage.get("pages") or []:
        name, number = str(page.get("file")), page.get("page")
        if name not in index_of or not isinstance(number, int):
            continue
        where = (index_of[name], number)
        for spot in page.get("not_read") or []:
            box = _box(spot.get("box"))
            if box is None:
                continue
            boxes.setdefault(where, []).append(box)
            reason = str(spot.get("reason") or "")
            if spot.get("kind") != "strokes" or spot.get("side") != "architect":
                continue
            if reason not in REASONS:
                continue
            also = tuple(str(r) for r in spot.get("also") or [])
            out.append(
                Candidate(
                    job=job,
                    file_index=where[0],
                    file=name,
                    page=number,
                    side="architect",
                    group=STROKE_GROUP,
                    reason=reason,
                    box=box,
                    also=also,
                    sideways="sideways" in (reason, *also),
                )
            )
    return out, boxes


def allocate(available: Mapping[str, int], total: int, minimum: int) -> dict[str, int]:
    """How many to take of each kind: `total` shared in proportion to what each kind has, except
    that a kind whose share would fall below `minimum` gets `minimum` (or all it has, if fewer) and
    the others share what is left, again in proportion. Whole numbers by largest remainder. The
    counts add up to `total`, or to everything there is when that is less, or to more when the
    minimums alone pass `total` (each kind still gets its minimum); no kind gets more than it
    has."""
    if sum(available.values()) <= total:
        return dict(available)
    fixed: dict[str, int] = {}
    while True:
        free = [kind for kind in available if kind not in fixed]
        budget = total - sum(fixed.values())
        pool = sum(available[kind] for kind in free)
        if budget <= 0 or pool <= 0:
            shares: dict[str, float] = {kind: 0.0 for kind in free}
            break
        shares = {kind: budget * available[kind] / pool for kind in free}
        low = [kind for kind in free if shares[kind] < min(minimum, available[kind])]
        if not low:
            break
        for kind in low:
            fixed[kind] = min(minimum, available[kind])
    take = dict(fixed)
    whole = {kind: int(share) for kind, share in shares.items()}
    spare = max(0, total - sum(fixed.values()) - sum(whole.values()))
    for kind in sorted(shares, key=lambda kind: (-(shares[kind] - whole[kind]), kind))[:spare]:
        whole[kind] += 1
    take.update(whole)
    return {kind: take.get(kind, 0) for kind in available}


def _order(candidate: Candidate, seed: str) -> str:
    return hashlib.sha256(f"{seed}|{candidate.id}".encode()).hexdigest()


def sample(
    candidates: Iterable[Candidate],
    *,
    total: int = TOTAL,
    control_each: int = CONTROL_EACH,
    minimum: int = MINIMUM,
    seed: str = SEED,
) -> list[Candidate]:
    """The exam's labels: `control_each` of every control group, and the rest of `total` from the
    unread strokes by reason (`allocate`). Within a group, candidates are taken in the order of a
    hash of `seed` and their id, so the same packs always give the same sample. A label listed
    twice (the same id) counts once."""
    unique = {candidate.id: candidate for candidate in candidates}
    by_kind: dict[str, list[Candidate]] = {}
    for candidate in unique.values():
        by_kind.setdefault(candidate.reason, []).append(candidate)
    for listed in by_kind.values():
        listed.sort(key=lambda candidate: _order(candidate, seed))
    chosen: list[Candidate] = []
    controls = 0
    for kind in CONTROLS:
        picked = by_kind.get(kind, [])[:control_each]
        controls += len(picked)
        chosen.extend(picked)
    counts = allocate(
        {kind: len(by_kind.get(kind, [])) for kind in REASONS}, max(0, total - controls), minimum
    )
    for kind in REASONS:
        chosen.extend(by_kind.get(kind, [])[: counts[kind]])
    return chosen


def code_crop(
    box: Sequence[float],
    neighbours: Iterable[Sequence[float]],
    *,
    pad: float = _PAD_PT,
    gap_x: float = _GAP_X_PT,
    gap_y: float = _GAP_Y_PT,
) -> Box:
    """The crop a reader is shown of one label (E2's rule, `slot_reader/runs.py`): its box padded
    by `pad`, then stopped `gap_x` short of every neighbour beside it and `gap_y` short of every
    neighbour above or below it. Only the padding is ever clipped: the crop always holds the whole
    box. A neighbour that overlaps the box itself is not clipped against."""
    bx0, by0, bx1, by1 = (float(v) for v in box)
    x0, y0, x1, y1 = bx0 - pad, by0 - pad, bx1 + pad, by1 + pad
    others = [tuple(float(v) for v in other) for other in neighbours]
    others = [o for o in others if o != (bx0, by0, bx1, by1)]
    for ox0, oy0, ox1, oy1 in others:
        if oy1 > y0 and oy0 < y1:
            if ox1 <= bx0 and ox1 + gap_x > x0:
                x0 = ox1 + gap_x
            if ox0 >= bx1 and ox0 - gap_x < x1:
                x1 = ox0 - gap_x
    for ox0, oy0, ox1, oy1 in others:
        if ox1 > x0 and ox0 < x1:
            if oy1 <= by0 and oy1 + gap_y > y0:
                y0 = oy1 + gap_y
            if oy0 >= by1 and oy0 - gap_y < y1:
                y1 = oy0 - gap_y
    return (min(x0, bx0), min(y0, by0), max(x1, bx1), max(y1, by1))


def context_box(box: Sequence[float], pad: float = CONTEXT_PAD_PT) -> Box:
    """The person's wider picture of a label: its box padded by `pad` on every side."""
    x0, y0, x1, y1 = (float(v) for v in box)
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


# ---- the key file --------------------------------------------------------------------------------


def _turn(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() else None


def read_key(path: Path) -> list[KeyRow]:
    """Every row of a key file, in file order. Columns it does not know are kept in `extra`."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    known = set(KEY_COLUMNS)
    out: list[KeyRow] = []
    for row in rows:
        out.append(
            KeyRow(
                id=row["id"],
                job=row["job"],
                file_index=int(row["file_index"]),
                page=int(row["page"]),
                side=row.get("side") or "",
                reason=row.get("reason") or "",
                box=parse_box(row["box"]),
                crops=tuple(part for part in (row.get("crops") or "").split("|") if part),
                proposed_text=row.get("proposed_text") or "",
                proposed_turn=_turn(row.get("proposed_turn") or ""),
                confidence=row.get("confidence") or "",
                confirmed=row.get("confirmed") or "",
                group=row.get("group") or "",
                also=tuple(part for part in (row.get("also") or "").split("|") if part),
                file=row.get("file") or "",
                context=row.get("context") or "",
                code_text=row.get("code_text") or "",
                extra={key: value for key, value in row.items() if key not in known and key},
            )
        )
    return out


def _clean(text: str) -> str:
    """A cell: no tab or line break, so a printed value never splits a row."""
    return " ".join(str(text).split())


def write_key(path: Path, rows: Iterable[KeyRow]) -> None:
    """Write a key file. Refuses to replace one that exists: a key is filled by a person."""
    if path.exists():
        raise FileExistsError(f"{path} exists; a reading key is never overwritten")
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(KEY_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.id,
                    row.job,
                    str(row.file_index),
                    str(row.page),
                    row.side,
                    row.reason,
                    box_text(row.box),
                    "|".join(row.crops),
                    _clean(row.proposed_text),
                    "" if row.proposed_turn is None else str(row.proposed_turn),
                    row.confidence,
                    _clean(row.confirmed),
                    row.group,
                    "|".join(row.also),
                    _clean(row.file),
                    row.context,
                    _clean(row.code_text),
                ]
            )


def with_proposal(row: KeyRow, text: str, turn: int | None, confidence: str) -> KeyRow:
    """`row` with a proposed reading. `confidence` must be one of `CONFIDENCE`; a turn, when
    given, 0, 90 or 270."""
    if confidence not in CONFIDENCE:
        raise ValueError(f"confidence is one of {CONFIDENCE}, not {confidence!r}")
    if turn not in (None, 0, 90, 270):
        raise ValueError(f"a turn is 0, 90 or 270, not {turn!r}")
    return replace(row, proposed_text=text, proposed_turn=turn, confidence=confidence)


def index_html(rows: Sequence[KeyRow], title: str) -> str:
    """A page a person confirms the key on: each label's pictures beside its proposal, with yes /
    no / unreadable and a box for a corrected text. Its button copies the answers as two-column
    TSV (`id`, `confirmed`) for `scripts/score_reading_exam.py --answers`. Every value is
    escaped: a printed label is data, never markup."""
    body: list[str] = []
    for number, row in enumerate(rows, 1):
        pictures = "".join(
            f'<figure><img src="{html.escape(path)}" alt=""><figcaption>'
            f"{html.escape(_caption(position))}</figcaption></figure>"
            for position, path in enumerate(row.crops)
        )
        if row.context:
            pictures += (
                f'<figure><img src="{html.escape(row.context)}" alt="" class="context">'
                "<figcaption>around it (red box)</figcaption></figure>"
            )
        ident = html.escape(row.id)
        body.append(
            f'<tr data-id="{ident}"><td>{number}<br><small>{ident}</small><br>'
            f"<small>{html.escape(row.job)} p{row.page} {html.escape(row.side)}</small></td>"
            f"<td>{html.escape(row.reason)}"
            + (f"<br><small>also {html.escape(', '.join(row.also))}</small>" if row.also else "")
            + f"</td><td class=pics>{pictures}</td>"
            f"<td class=prop><b>{html.escape(row.proposed_text) or '&mdash;'}</b><br>"
            f"<small>turn {'' if row.proposed_turn is None else row.proposed_turn} &middot; "
            f"{html.escape(row.confidence)}</small></td>"
            f'<td class=ans><label><input type=radio name="{ident}" value="yes"> yes</label> '
            f'<label><input type=radio name="{ident}" value="no"> no</label> '
            f'<label><input type=radio name="{ident}" value="unreadable"> unreadable</label><br>'
            f'<input type=text placeholder="the right text" name="{ident}-text"></td></tr>'
        )
    return _PAGE.format(title=html.escape(title), rows="\n".join(body), count=len(rows))


def _caption(position: int) -> str:
    return ("as cut", "turned 90", "turned 270")[position] if position < 3 else ""


_PAGE: Final = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1em; }}
table {{ border-collapse: collapse; }}
td {{ border-bottom: 1px solid #ccc; padding: 6px; vertical-align: top; }}
td.pics figure {{ display: inline-block; margin: 0 8px 0 0; text-align: center; }}
td.pics img {{ max-height: 160px; image-rendering: pixelated; border: 1px solid #ddd; }}
td.pics img.context {{ max-height: 220px; }}
td.prop b {{ font-size: 1.3em; }}
figcaption {{ font-size: 0.75em; color: #666; }}
</style></head><body>
<h1>{title}</h1>
<p>{count} labels. For each one: <b>yes</b> if the proposal is exactly what is printed (and the turn
reads upright), <b>no</b> with the right text typed in the box, or <b>unreadable</b>.
"{not_a_number}" means the spot prints no dimension at all. Then press the button and paste the
answers into a file for the scorer (<code>--answers</code>).</p>
<button onclick="copyAnswers()">Copy answers (TSV)</button>
<table>
<tr><th>#</th><th>why</th><th>pictures</th><th>proposal</th><th>your answer</th></tr>
{rows}
</table>
<script>
function copyAnswers() {{
  const lines = ["id\\tconfirmed"];
  document.querySelectorAll("tr[data-id]").forEach(row => {{
    const id = row.dataset.id;
    const picked = row.querySelector('input[type=radio]:checked');
    const typed = row.querySelector('input[type=text]').value.trim();
    let answer = typed || (picked ? picked.value : "");
    if (answer === "no") answer = "";
    if (answer) lines.push(id + "\\t" + answer);
  }});
  navigator.clipboard.writeText(lines.join("\\n") + "\\n");
  alert((lines.length - 1) + " answers copied");
}}
</script>
</body></html>
""".replace("{not_a_number}", NOT_A_NUMBER)
