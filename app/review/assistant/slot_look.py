"""The slot call in the full check (Step 4, behind `GV_ASSISTANT_SLOTS=on`; off by default).

After pairing, one call per paired vendor page: its first vendor drawing and the first architect
drawing paired with it (one item a page until Step 3 pairs per drawing). The call is an ordinary
roster specialist (`chat_engine._Agent`), so the engine's own call path carries it: its money, its
cap, its call log. It is shown two pictures with numbered marks, the mark table and the fixed slot
questions (`slot_prompt`), and its reply shape is built for its own marks
(`slot_answers.schema`): it lists only the marks it was dealt, and the parser refuses any other.

Code then takes over (`apply_roster`): it parses the reply (refusing typed numbers and unknown
marks), decides each answer (`slot_maths.decide`), records a finding for each answer whose values
differ through the book's normal path (`FindingBook.report`, naming the label of a single mark, so
the usual label checks apply and the later steps see it like any other finding: a two-label one
is judged by the devil's advocate's blind second pick like the rest), lays the slot and maths
records on it, and reports every slot as answered, not shown or not asked for the coverage ledger
(`SlotRuns.rows`). Answers that agree or could not be compared are kept on the run's slot state
(`SlotRuns.kept`) and in the coverage rows, not as findings: a Right is never listed in flag mode,
and the book's room is the AI's to share.

The open path is untouched: the page helpers, the whole-drawing look and the page reviewer still
run for the questions only the AI can judge and for everything that is not a size.

Verification: tests/review/assistant/test_slot_look.py (a fake engine, a fake crop, no AI call).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from app.review.assistant import slot_maths, slot_prompt, slots, whole_look
from app.review.assistant.coverage_ledger import SlotRow
from app.review.assistant.slot_answers import AnswerRefused, MarkMap, SlotReply, parse, schema
from app.review.assistant.whole_look import Drawing

__all__ = [
    "LENS",
    "Item",
    "SlotRuns",
    "apply_item",
    "items_for_page",
    "opening",
    "roster_agents",
]

_log = logging.getLogger(__name__)

#: The agents' lens and timings phase.
LENS: Final = "slots"
#: What one slot call is given at least (millionths of $): one call with two pictures and a
#: structured answer, as a whole-drawing look (`whole_look.ONE_LOOK_MICROS`).
ONE_CALL_MICROS: Final = whole_look.ONE_LOOK_MICROS
#: The tool a slot call may use: zoom, to read a picture sharper (never to type a value).
TOOLS: Final = frozenset({"zoom"})
#: Labels inside a drawing's box within this many points still belong to it.
_INSIDE_PT: Final = 6.0


@dataclass(frozen=True, slots=True)
class Item:
    """One paired item: a vendor drawing and the architect drawing paired with it."""

    vendor: Drawing
    architect: Drawing | None

    @property
    def key(self) -> str:
        """The coverage ledger's item key for the vendor page (`coverage_ledger.items_from_run`)."""
        return f"p{self.vendor.page}"


def items_for_page(
    page_map: Mapping[str, Any] | None, main_file: str | None, page: int, cross: Any
) -> list[Item]:
    """The page's paired items: each vendor drawing with the first architect drawing its look
    holds (`whole_look.plan_looks`: the cross-file pairs, or GV's area on combined sheets). A
    page no architect drawing is paired with has none."""
    items: list[Item] = []
    for look in whole_look.plan_looks(page_map, main_file, (page,), cross):
        architects = [d for d in look.drawings if d.role == "architect"]
        for drawing in look.drawings:
            if drawing.role == "vendor" and drawing.page == page:
                items.append(Item(drawing, architects[0] if architects else None))
    return items


def _inside(label: Mapping[str, Any], drawing: Drawing) -> bool:
    box = label.get("box")
    if not isinstance(box, Sequence) or len(box) != 4:
        return False
    x0, y0, x1, y1 = drawing.box
    cx, cy = (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2
    return x0 - _INSIDE_PT <= cx <= x1 + _INSIDE_PT and y0 - _INSIDE_PT <= cy <= y1 + _INSIDE_PT


def _labels(labels: Any, drawing: Drawing) -> list[Mapping[str, Any]]:
    """The labels code read inside one drawing, of that drawing's side (or unknown)."""
    if labels is None:
        return []
    try:
        page = labels.on_page(drawing.file, drawing.page, read_now=False) or []
    except Exception:  # noqa: BLE001 - no marks: the call says not_shown
        _log.info("assistant slots: labels not listed for page %s", drawing.page)
        return []
    return [
        label
        for label in page
        if _inside(label, drawing)
        and str(label.get("side") or "unknown") in (drawing.role, "unknown")
    ]


def mark_map(labels: Any, item: Item, seed: str) -> MarkMap:
    """The item's marks: every label code read in its two drawings, shuffled by `seed`."""
    dealt: list[tuple[Any, str, int, Mapping[str, Any]]] = [
        ("vendor", item.vendor.file, item.vendor.page, label)
        for label in _labels(labels, item.vendor)
    ]
    if item.architect is not None:
        dealt += [
            ("architect", item.architect.file, item.architect.page, label)
            for label in _labels(labels, item.architect)
        ]
    return MarkMap(dealt, seed)


type Crop = Callable[[str, int, tuple[float, float, float, float]], Any]
"""(file, page, box) -> a crop with `png`, `box`, `width`, `height` (`set_pack.crop_picture`)."""


def opening(
    item: Item,
    marks: MarkMap,
    crop: Crop,
    pictures: Path,
) -> list[dict[str, Any]]:
    """The call's pictures and mark table, drawn by code before its first call (no AI cost)."""
    content: list[dict[str, Any]] = []
    shown: dict[str, Sequence[float]] = {}
    sides: list[tuple[str, Drawing]] = [("vendor", item.vendor)]
    if item.architect is not None:
        sides.append(("architect", item.architect))
    for side, drawing in sides:
        who = "the VENDOR's shop drawing" if side == "vendor" else "the ARCHITECT's drawing"
        content.append(
            {"type": "text", "text": f"Picture: {who}, page {drawing.page} of {drawing.file}."}
        )
        try:
            cut = crop(drawing.file, drawing.page, drawing.box)
            marked = [(ref.mark, ref.label) for ref in marks.refs(side)]  # type: ignore[arg-type]
            png = slot_prompt.draw_slot_marks(cut.png, cut.box, marked)
        except Exception:  # noqa: BLE001 - said plainly; the other picture still goes
            _log.warning("assistant slots: picture of page %s not drawn", drawing.page)
            content.append({"type": "text", "text": "This picture could not be drawn."})
            continue
        picture_id = str(uuid4())
        pictures.mkdir(parents=True, exist_ok=True)
        (pictures / f"{picture_id}.png").write_bytes(png)
        shown[side] = cut.box
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "gv_picture",
                    "picture_id": picture_id,
                    "width": cut.width,
                    "height": cut.height,
                },
            }
        )
    content.append({"type": "text", "text": "The marks:\n" + slot_prompt.mark_table(marks, shown)})
    return content


@dataclass(slots=True)
class SlotRuns:
    """One full check's slot calls: each agent's item and marks, and what code did with them."""

    items: dict[str, tuple[Item, MarkMap]] = field(default_factory=dict)
    rows: list[SlotRow] = field(default_factory=list)
    recorded: list[str] = field(default_factory=list)
    kept: list[dict[str, Any]] = field(default_factory=list)
    """Every decided answer (recorded or not), for the run's log."""
    dropped: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def block(self) -> dict[str, Any]:
        """For the run's log: counts, and every refused or dropped answer's reason."""
        from app.review.assistant.coverage_ledger import slot_summary

        return {
            "items": len(self.items),
            "slots": slot_summary(self.rows),
            "recorded": len(self.recorded),
            "decided": len(self.kept),
            "dropped": list(self.dropped),
            "unread": list(self.unread),
        }


def _asked() -> tuple[slots.Slot, ...]:
    return slots.catalogue()


def apply_item(
    *,
    item: Item,
    marks: MarkMap,
    reply: Mapping[str, Any] | None,
    record: Callable[[dict[str, Any], dict[str, Any]], str | None],
    rules_md: str | None,
    when: datetime,
    state: SlotRuns,
    asked: Sequence[slots.Slot] | None = None,
) -> None:
    """One item's reply, by code. `record(raw, extra)` records a finding (the book's report, then
    the records laid on) and returns its id, or `None` when the book refused it."""
    asked = tuple(asked) if asked is not None else _asked()
    replies: list[SlotReply] = []
    if reply is not None:
        try:
            replies = parse(reply, asked, marks)
        except AnswerRefused as refused:
            state.dropped.append(f"{item.key}: the reply was refused: {refused}")
    answered = {one.key for one in replies}
    for one in replies:
        for why in one.refused:
            state.dropped.append(f"{item.key} {one.key}: {why}")
        if one.status == "not_shown":
            state.rows.append(SlotRow(item.key, one.key, "not_shown", one.reason or ""))
            continue
        slot = next(s for s in asked if s.key == one.key)
        for answer in one.answers:
            outcome = slot_maths.decide(slot, answer, rules_md=rules_md, when=when)
            finding_id = ""
            if outcome.differs:
                raw, extra = slot_maths.finding_fields(outcome)
                try:
                    finding_id = record(raw, extra) or ""
                except Exception as failed:  # noqa: BLE001 - one answer never stops the others
                    state.dropped.append(f"{item.key} {one.key}: not recorded: {failed}")
                if finding_id:
                    state.recorded.append(finding_id)
            state.kept.append({"item": item.key, **outcome.record(), "finding": finding_id})
            state.rows.append(
                SlotRow(
                    item.key,
                    one.key,
                    "answered",
                    outcome=str(outcome.record()["outcome"]),
                    red_barred=bool(outcome.red_barred),
                    finding=finding_id,
                )
            )
    for slot in asked:
        if slot.key not in answered:
            state.rows.append(SlotRow(item.key, slot.key, "not_asked"))


# ---- in the roster -----------------------------------------------------------------------------------


def roster_agents(
    run: Any,
    pages: Sequence[int],
    page_map: Mapping[str, Any] | None,
    matching: Any,
    state: SlotRuns,
    *,
    done: Collection[int] = frozenset(),
) -> list[Any]:
    """The slot calls as agents, one per vendor page (its first paired item), or none when the
    switch is off (`slots.slots_on`). Each waits for its page's pairs in its own thread (`ready`),
    then draws its pictures and sets its own reply shape; a page with no paired architect drawing
    is skipped (nothing is called)."""
    if not slots.slots_on():
        return []
    engine = run.engine
    from app.review.assistant.chat_request import MODELS

    model = MODELS.get(str(getattr(engine.settings, "whole_look_model", None) or ""))
    if model is None:
        model = engine._helper_model()
    tools = run._tools(TOOLS)
    wanted = sorted({int(p) for p in pages} - set(done))
    agents: list[Any] = []
    for page in wanted:
        agent = run._agent(
            call_id=f"slots-p{page}",
            description=f"Fixed questions · page {page}",
            prompt="",
            model=model,
            pages=(page,),
            lens=LENS,
            effort="medium",
            cap_micros=ONE_CALL_MICROS,
            soft_cap=True,
            system=slot_prompt.SYSTEM,
            tools=tools,
            phase=LENS,
        )
        agent.ready = _ready(engine, agent, page_map, matching, page, state)
        agents.append(agent)
    return agents


def _crop(engine: Any) -> Crop:
    from app.review.assistant import set_pack

    workspace = engine.tools.workspace
    folder = workspace.set_pack

    def cut(file: str, page: int, box: tuple[float, float, float, float]) -> Any:
        pdf = set_pack.drawings_only(folder, workspace.uploads, file)
        return set_pack.crop_picture(pdf, page, box, None, cache=folder / set_pack.HIRES)

    return cut


def _ready(
    engine: Any,
    agent: Any,
    page_map: Mapping[str, Any] | None,
    matching: Any,
    page: int,
    state: SlotRuns,
) -> Callable[[], str | None]:
    def ready() -> str | None:
        cross = (
            matching.wait_pages((page,), engine.cancel)
            if matching is not None
            else getattr(engine, "_cross", None)
        )
        items = items_for_page(page_map, engine.book.main_file, page, cross)
        if not items or items[0].architect is None:
            agent.skip = True  # no architect drawing paired with it: nothing to compare
            return None
        item = items[0]
        marks = mark_map(engine.book.dim_labels, item, agent.call_id)
        if not marks.marks("vendor"):
            agent.skip = True  # code read no size on the vendor's drawing: nothing to name
            return None
        asked = _asked()
        with state.lock:
            state.items[agent.call_id] = (item, marks)
        agent.output_format = schema(asked, marks)
        agent.opening = tuple(opening(item, marks, _crop(engine), engine.tools.workspace.pictures))
        return slot_prompt.brief(asked, has_architect=item.architect is not None)

    return ready


def _recorder(
    engine: Any, lock: threading.Lock
) -> Callable[[dict[str, Any], dict[str, Any]], str | None]:
    from app.review.assistant.sandbox import SandboxRefused

    book = engine.book

    def record(raw: dict[str, Any], extra: dict[str, Any]) -> str | None:
        with lock:
            try:
                finding, _notes = book.report(dict(raw), slot_maths.CREATED_BY)
            except SandboxRefused as refused:
                raise RuntimeError(str(refused)) from None
            updated = {**finding, **extra}
            if book.replace(updated):
                engine._show_finding(updated)
            return str(updated["id"])

    return record


def _rules_md(engine: Any) -> str | None:
    try:
        return str((engine.tools.workspace.skills / "rules.md").read_text(encoding="utf-8"))
    except (OSError, AttributeError):
        return None


def apply_roster(run: Any, done: Sequence[tuple[Any, Mapping[str, Any]]], state: SlotRuns) -> None:
    """Every slot call's answer, by code (`apply_item`), once the first phase's agents reported."""
    from app.review.assistant.reply_json import object_of_reply

    engine = run.engine
    record = _recorder(engine, threading.Lock())
    rules_md = _rules_md(engine)
    when = datetime.now(UTC)
    for agent, entry in done:
        held = state.items.get(agent.call_id)
        if held is None or getattr(agent, "skip", False):
            continue
        item, marks = held
        try:
            reply: Mapping[str, Any] | None = object_of_reply(str(entry.get("report") or ""))
        except ValueError:
            reply = None
            state.unread.append(item.key)
        try:
            apply_item(
                item=item,
                marks=marks,
                reply=reply,
                record=record,
                rules_md=rules_md,
                when=when,
                state=state,
            )
        except Exception:  # noqa: BLE001 - one item failing never stops the check
            _log.exception("assistant slots %s could not be applied", agent.call_id)
    _log.warning("assistant slots: %s", state.block())
