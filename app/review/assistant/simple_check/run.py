"""The simple full check's run: work list, code findings, compare, verify, blind look, outputs.

CONTRACT-SIMPLE (2026-10-19, Anant: "go ahead"); behind `assistant_full_check_mode = "simple"`
(the roster stays the default until a paid test beats it). In order:

1. **The work list** (`worklist.py`, code, $0): every vendor drawing of the set with its area's
   architect drawings; every page covered or skipped with code's reason. The product scope leaves
   out an area whose pages name only products out of scope (`product_scope.outside_unit`, as the
   roster). A pilot (`ChatMessageIn.pilot`) compares only the first N items and lists the rest.
2. **Code's own findings** ($0): GV Review's recorded results (GV's rule rows and the AI page
   review), the vendor consistency check (strings), the finish check (tags), code's own pairs. Each
   Wrong one is judged by the devil's advocate's CODE checks with the GV-note fix: its vendor value's
   own ink, roles, newest round, units, slip; never the old "inside a GV note's box" check.
3. **One compare call per work item** (`compare.compare_all`, in parallel, prompt-cached). No fixed
   price, no share split, no helper agents: only the per-answer runaway guard
   (`assistant_message_cap_usd`) stops it, and a stopped or failed item is listed, never dropped.
   Each item's usage is a row on the Usage page and is charged to the answer. The screen shows
   "Comparing drawing 12 of 36".
4. **Code verifies** each answer (`verify.py`) and **one blind second look** per red candidate
   (`blind.py`) must agree before anything is red.
5. **The existing outputs**: findings in the chat finding shape (merged onto GV Review's recorded
   row of the same item when there is one), the Not-compared rows (an architect that gives no size,
   room context), the trust report, the cloud drawing, the check report, the Not-compared PDF and
   workbook, the send-ready package, code's summary. Every work item ends answered, failed or
   skipped with its reason (a `work_items` block); any failed item makes the check incomplete
   (review result S0, "Check incomplete: not for sending"), said in the summary and on the cover.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from app.review.assistant.chat_request import MODELS, ChatModel
from app.review.assistant.chat_summary import CODE_SUMMARY_NOTE, code_summary
from app.review.assistant.chat_trust import judged
from app.review.assistant.filler_range import covered_labels
from app.review.assistant.finding_checks import to_inches
from app.review.assistant.full_check_advocate import code_checks, decide, ink_colour, needs_pairing
from app.review.assistant.product_scope import refiled
from app.review.assistant.simple_check import verify as verify_module
from app.review.assistant.simple_check.blind import BlindSettings, look_all
from app.review.assistant.simple_check.schema import CompareOutcome, WorkItem
from app.review.assistant.simple_check.verify import (
    CREATED_BY,
    BlindAnswer,
    Item,
    Pack,
    ValueReader,
    Verified,
    with_blind,
)

if TYPE_CHECKING:
    from app.review.assistant.chat_engine import ChatEngine, MessageOutcome

__all__ = [
    "SIMPLE_DIR",
    "SimpleOptions",
    "load_blind",
    "load_outcomes",
    "replay",
    "run",
    "work_item_rows",
]

_log = logging.getLogger(__name__)

#: The compare call's prompt id on the Usage page.
COMPARE_PROMPT_ID: Final = "review-assistant-chat-simple-compare-{provider}-{harness}"
#: What a compare call's cost is held at while it runs (`_guarded`): its pictures' tokens, the
#: text of the request and the cached system prompt, and this much reply (thinking and the form).
_REQUEST_TEXT_TOKENS: Final = 8_000
_REPLY_HOLD_TOKENS: Final = 16_000
#: A chat's simple-check runs, each with its saved answers (`<chat>/simple/<run>/`).
SIMPLE_DIR: Final = "simple"
#: The order findings are recorded in (the finding book holds `MAX_FINDINGS`): what matters first.
_ORDER: Final = {"red": 0, "not_compared": 1, "right": 2}
#: Who made a finding the run judges with the code checks (everything but its own).
_SKIP_JUDGING: Final = frozenset({CREATED_BY})
_MICROS: Final = 1_000_000
#: Why a red candidate of a product outside the check's scope gets no blind look (it is never
#: shown: the finding book leaves it out, and only counts it).
OUTSIDE_SCOPE: Final = "outside the product scope of this check"
#: How long the run waits for the set pack to be made (its labels are read by OCR at upload).
PACK_WAIT_SECONDS: Final = 1800.0
_PACK_POLL_SECONDS: Final = 2.0
#: The words a pilot's left-out drawings are skipped with (`_scope_and_pilot`).
_PILOT: Final = "a pilot run compares only the first"
#: The next step of a check that compared no drawing at all.
NOTHING_NEXT: Final = "Next: nothing can be sent to the vendor until the drawings are compared."
#: Code's summary lines that read as a finished comparison: never printed when no drawing was
#: compared (a $0 run on plain CAD files, 2026-10-19, printed "Checked 6 of 6 pages" and "Every
#: item could be compared." next to "Nothing was compared").
_AS_IF_CHECKED: Final = re.compile(
    r"^(?:Checked |Every item could be compared\.|No wrong value was proved by code so far\.)"
)
#: The next step of a check that did not finish: never "send it to the vendor".
NOT_FINISHED_NEXT: Final = (
    "Next: finish the check (compare the drawings left) before anything is sent to the vendor."
)
#: The note under the simple check's summary (the screen reads a last line "*Summary written by
#: GV Review…*" as its note).
SIMPLE_SUMMARY_NOTE: Final = "*Summary written by GV Review from code's own checks.*"


@dataclass(frozen=True, slots=True)
class SimpleOptions:
    """The simple check's settings for one run (`app/config.py`: `assistant_simple_*`), and the
    pilot's size (`ChatMessageIn.pilot`)."""

    compare: Mapping[str, Any] = field(default_factory=dict)
    """`compare.CompareSettings`'s fields."""
    blind: BlindSettings = field(default_factory=BlindSettings)
    pilot: int | None = None
    replay: str | None = None
    """A run folder of this chat (`simple/<run>`): its saved compare answers and blind looks are
    used instead of model calls ($0); a drawing with no saved answer is not compared."""
    compare_missing: bool = False
    """With `replay` (#1710, "Compare them again"): the drawings the run has no answer for (failed,
    stopped, never started) are compared now, and the blind looks it could not make are made now;
    every saved answer and look is used again at $0, so the new answer is the whole result."""

    @classmethod
    def of(cls, raw: object) -> SimpleOptions:
        """From the plain mapping the API builds (it never imports this module: no drawing code
        in `app/api/`)."""
        if isinstance(raw, SimpleOptions):
            return raw
        given = raw if isinstance(raw, Mapping) else {}
        pilot = given.get("pilot")
        return cls(
            compare=dict(given.get("compare") or {}),
            blind=BlindSettings(
                str(given.get("blind_model") or "claude-opus-5-5"),
                str(given.get("blind_effort") or "high"),
            ),
            pilot=int(pilot) if isinstance(pilot, int) and pilot > 0 else None,
            replay=str(given["replay"]) if given.get("replay") else None,
            compare_missing=bool(given.get("compare_missing")) and bool(given.get("replay")),
        )


def _compare() -> Any:
    return import_module("app.review.assistant.simple_check.compare")


def work_item_rows(
    items: Sequence[WorkItem],
    outcomes: Mapping[str, CompareOutcome],
    skipped: Mapping[str, str],
) -> list[dict[str, Any]]:
    """The `work_items` block: each work item answered, failed or skipped, with its reason, the
    model that answered and what it cost (the list price estimate)."""
    rows: list[dict[str, Any]] = []
    for item in items:
        outcome = outcomes.get(item.id)
        reason: str | None
        model: str | None
        if item.id in skipped:
            status, reason, model, cost = "skipped", skipped[item.id], None, 0.0
        elif outcome is None:
            status, reason, model, cost = "failed", "not compared (the run ended first)", None, 0.0
        else:
            status = "answered" if outcome.status == "ok" else "failed"
            reason = outcome.reason if outcome.status != "ok" else None
            model = outcome.model or outcome.asked_model
            cost = outcome.cost_usd
        rows.append(
            {
                "id": item.id,
                "page": item.page,
                "round": item.round,
                "status": status,
                "reason": reason,
                "model": model,
                "cost_usd": f"{cost:.4f}",
            }
        )
    return rows


@dataclass
class _Run:
    engine: ChatEngine
    options: SimpleOptions
    items: list[WorkItem] = field(default_factory=list)
    compared: list[WorkItem] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    outcomes: dict[str, CompareOutcome] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    """Not-compared rows that are not findings (`engine.extra_gaps`)."""
    not_listed: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    cap_hit: str | None = None
    pack: Pack | None = None
    page_map: Mapping[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    run_dir: Path | None = None
    """Where this run's compare answers and blind looks are saved (`<chat>/simple/<run>`)."""
    reserved: int = 0
    """What the compare calls in flight may still cost (millionths of $): the runaway guard counts
    it as spent, so the calls started never pass the cap together."""
    reservations: dict[str, int] = field(default_factory=dict)
    refused: set[str] = field(default_factory=set)
    """Drawings not started because the runaway guard would have been passed."""
    saved_blind: dict[str, dict[str, Any]] = field(default_factory=dict)
    blind_failed: int = 0
    """Red candidates whose blind look was asked for but could not be made (the call failed, the
    reply was cut short or not the form): Not compared, said in the summary."""
    blind_skipped: str | None = None
    """Why the red candidates got no blind look (a stop, no money), when they did not."""
    blind_skipped_count: int = 0
    out_of_scope: int = 0
    """Items of products outside the check's scope: never looked at again, never shown."""

    # -- the run --

    def run(self) -> MessageOutcome:
        engine = self.engine
        workspace = engine.tools.workspace
        folder = workspace.set_pack
        from app.review.assistant.full_check_plan import wait_for_page_map

        if folder is None:
            return self._finish("error", "the drawing set's pack (page map, labels) is not made")
        with engine.timings.phase("set_pack"):
            self.page_map = wait_for_page_map(
                folder, engine.cancel, assume=True, main_file=engine.book.main_file
            )
        if engine.cancel.is_set():
            return self._finish("cancelled")
        if self.page_map is None:
            return self._finish("error", "the drawing set's page map is not ready")
        with engine.timings.phase("set_pack"):
            # The work list needs the whole pack (every label, code's pairs), not just the page map.
            waited = _wait_for_pack(folder, engine.cancel)
        if engine.cancel.is_set():
            return self._finish("cancelled")
        if waited is not None:
            return self._finish("error", waited)
        with engine.timings.phase("planning"):
            try:
                worklist = import_module(
                    "app.review.assistant.simple_check.worklist"
                ).build_worklist(folder, engine.book.main_file)
                self.pack = Pack.from_folder(folder, engine.book.main_file)
            except Exception as failed:  # noqa: BLE001 - said plainly, nothing compared
                _log.exception("simple check: work list failed")
                return self._finish("error", f"the work list could not be made ({failed})")
            self.items = list(worklist.items)
            self._scope_and_pilot()
        self._tell("work_list", self.items, self.skipped)
        engine.critic_off = True
        with engine.timings.phase("seeding"):
            try:
                engine._seed_recorded()
            except Exception:  # noqa: BLE001 - the check goes on; the trust report says it
                _log.exception("simple check could not seed the recorded differences")
        try:
            engine._start_markup_base()
        except Exception:  # noqa: BLE001 - the drawing makes its own copy at the end
            _log.warning("simple check could not start the markup copy")
        engine._started_scope = engine.scope()
        engine.emit(
            "scope", {"index": None, "scope": json.loads(json.dumps(engine._started_scope))}
        )
        self._cover_pages(worklist)
        self._progress()
        # The vendor consistency check's line work (about a second a page) is read beside the
        # compare calls and kept in the set pack; code's own findings then come after them, so
        # the first compare call no longer waits for half a minute of code work.
        warm = threading.Thread(
            target=import_module("app.review.assistant.vendor_consistency").warm_line_work,
            args=(folder, workspace.uploads),
            daemon=True,
            name="gv-simple-line-work",
        )
        warm.start()
        self._open_run_dir()
        if not engine.cancel.is_set():
            self._tell("compare_started")
            if self.options.replay:
                if self.options.compare_missing and self._replayed() is None:
                    return self._finish("error", "the run to compare again is not in this chat")
                missing = self._load_saved()
                if missing and not engine.cancel.is_set():
                    self._compare_all(folder, missing)
            else:
                self._compare_all(folder)
        self._tell("code_checks")
        warm.join()
        # Code's own findings ($0), judged by the code checks (GV-note fix).
        engine.add_code_findings()
        with engine.timings.phase("advocate"):
            self._judge_code_findings()
        self._tell("decide", engine.book.live())
        # A stop (the reviewer's, or the runaway guard's) never throws away what was paid for:
        # every answered drawing is verified and recorded; only the blind looks wait for money.
        with engine.timings.phase("verify"):
            verified = self._verify_all()
        with engine.timings.phase("blind"):
            verified = self._blind(verified)
        self._tell("looked", [v for v, _item in verified])
        with engine.timings.phase("finish_checks"):
            self._record(verified)
        self._tell("decide", engine.book.live())
        if engine.cancel.is_set():
            return self._finish("cancelled")
        return self._finish("cost_cap" if self.cap_hit else None)

    # -- 1. the work list --

    def _scope_and_pilot(self) -> None:
        engine = self.engine
        scope = engine.product_scope
        if not scope.every and engine.tools.workspace.set_pack is not None:
            from app.review.assistant.product_scope import outside_unit
            from app.review.assistant.vendor_consistency import page_texts

            main = engine.book.main_file or ""
            try:
                read = page_texts(engine.tools.workspace.set_pack, [main])
            except Exception:  # noqa: BLE001 - every item is then compared
                read = {}
            texts = {page: text for (name, page), text in read.items() if name == main}
            for item in self.items:
                why = outside_unit([item.page, *item.architect_pages], texts, scope)
                if why is not None:
                    self.skipped[item.id] = f"outside the product scope ({why})"
        pilot = self.options.pilot
        left = [item for item in self.items if item.id not in self.skipped]
        if pilot is not None and len(left) > pilot:
            for item in left[pilot:]:
                self.skipped[item.id] = f"{_PILOT} {pilot} drawings"
            left = left[:pilot]
        self.compared = left

    def _cover_pages(self, worklist: Any) -> None:
        """The coverage map at once: pages code skipped, with its reason; pages only looked at as
        an architect's or the brand prototype's drawing."""
        engine = self.engine
        pages: list[dict[str, Any]] = []
        compared_pages = {item.page for item in self.compared}
        for cover in worklist.pages:
            if cover.page in compared_pages:
                continue
            if cover.how == "skipped":
                pages.append(
                    {"page": cover.page, "status": "not_comparable", "note": cover.reason or ""}
                )
            elif cover.how in ("architect", "reference"):
                words = "architect's" if cover.how == "architect" else "brand prototype's"
                pages.append(
                    {
                        "page": cover.page,
                        "status": "not_comparable",
                        "note": f"the {words} drawing, compared with the vendor's",
                    }
                )
            elif cover.how == "vendor":
                reasons = {self.skipped.get(i) for i in cover.items if i in self.skipped}
                # A pilot's left-out drawings are not reached (the check is not finished); a
                # drawing outside the product scope is deliberately not compared.
                pilot = bool(reasons) and all(r and r.startswith(_PILOT) for r in reasons)
                pages.append(
                    {
                        "page": cover.page,
                        "status": "not_opened" if pilot else "not_comparable",
                        "note": "; ".join(sorted(r for r in reasons if r)) or "not compared",
                    }
                )
        if pages:
            try:
                engine.book.set_coverage({"pages": pages}, "code")
                engine._show_coverage()
            except Exception:  # noqa: BLE001 - the pages are then unknown, said in the summary
                _log.warning("simple check could not record the skipped pages")

    def _tell(self, name: str, *args: Any, **kwargs: Any) -> None:
        """The live screen's state (`live_check.LiveCheck`, #1710), when the check has one (a
        full check's message): drawings, phases, decided findings. It never stops the check."""
        live = getattr(self.engine, "live", None)
        if live is None:
            return
        try:
            getattr(live, name)(*args, **kwargs)
        except Exception:  # noqa: BLE001 - the live screen never stops the check
            _log.exception("simple check: live state %s failed", name)

    def _live_look(self, done: Verified) -> None:
        self._tell("second_look", done)

    def _progress(self) -> None:
        progress = self.engine._progress
        if progress is None:
            return
        try:
            progress.set_work(len(self.outcomes), len(self.compared))
        except Exception:  # noqa: BLE001 - progress never stops the check
            _log.debug("simple check progress not shown")

    # -- 2. code's own findings --

    def _judge_code_findings(self) -> None:
        """Each Wrong code finding with a code witness, judged by the advocate's code checks with
        the GV-note fix (no annotation box check): its vendor value's own ink, roles, newest round,
        units and slip. A failed check makes it grey with the reason, as the roster's advocate."""
        from app.review.assistant import witness

        engine = self.engine
        for finding in engine.book.live():
            if finding.get("status") != "different" or finding.get("created_by") in _SKIP_JUDGING:
                continue
            if not witness.witnesses(finding.get("checks") or {}, finding.get("status")):
                continue
            try:
                verdict = self._code_verdict(finding)
            except Exception:  # noqa: BLE001 - an item code could not judge is not proved
                _log.exception("simple check could not judge %s", finding.get("id"))
                verdict = None
            if verdict is not None:
                self._write_verdict(str(finding["id"]), *verdict)

    def _label(self, finding: Mapping[str, Any], side: str) -> dict[str, Any] | None:
        dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
        given = (dims or {}).get(side)
        labels = self.engine.book.dim_labels
        if isinstance(given, Mapping) and given.get("id"):
            try:
                return dict(
                    labels.resolve(
                        given["id"],
                        str(given.get("file") or self.engine.book.main_file),
                        int(given["page"]),
                    )
                )
            except (LookupError, KeyError, TypeError, ValueError):
                return dict(given)
        box = finding.get("vendor_box" if side == "vendor" else "arch_box")
        want = to_inches(str(finding.get(side) or ""))
        if not isinstance(box, Mapping) or want is None:
            return None
        file = str(box.get("file") or self.engine.book.main_file or "")
        page = int(box.get("page") or finding["page"])
        hits = []
        for label in labels.on_page(file, page, read_now=False) or []:
            x0, y0, x1, y1 = (float(v) for v in label.get("box") or (0, 0, 0, 0))
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            inside = float(box["x0"]) <= cx <= float(box["x1"]) and (
                float(box["y0"]) <= cy <= float(box["y1"])
            )
            if inside and label.get("side") == side and _same(label.get("inches"), want):
                hits.append(dict(label))
        return hits[0] if len(hits) == 1 else None

    def _code_verdict(
        self, finding: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        vendor = self._label(finding, "vendor")
        architect = self._label(finding, "architect")
        code_paired = not needs_pairing(finding)
        if vendor is None:
            # The vendor's own string or a rule read from boxes: it keeps its code witness.
            return None
        newest = None
        same = None
        if self.pack is not None:
            key = self.pack.area_key(str(vendor.get("file")), int(vendor.get("page") or 0))
            newest = self.pack.newest_round(key)
            if newest is not None:
                same = self.pack.newer_same(vendor, newest)
        checks = code_checks(
            vendor,
            None if code_paired else architect,
            annotations=(),  # the GV-note fix: only the value's own ink says it is GV's markup
            page_map=self.page_map,
            # A label read from the vendor's black or grey layer is the vendor's, whatever GV
            # drew over it (`verify._ink_check`); any other label's own spot is looked at.
            vendor_ink=(1, 0.0) if vendor.get("route") in ("text", "drawn") else self._ink(vendor),
            newer_same=same,
        )
        if not code_paired and not any(c.outcome != "pass" for c in checks):
            # An AI pairing code did not make (GV Review's page review): it needs the blind look,
            # which only the compare call's own items get. It stays as it is (Not compared).
            return None
        verdict = decide(
            mode="ai_pair_plus_code",
            checks=checks,
            printed=True,
            helper_pick=None,
            advocate=None,
            extent=None,
            code_paired=code_paired,
        )
        pairing = {
            "vendor": f"p{vendor.get('page')}.{vendor.get('id')}",
            "architect": (
                None if architect is None else f"p{architect.get('page')}.{architect.get('id')}"
            ),
            "mode": "simple",
            "checks": [[c.name, c.outcome, c.why] for c in checks],
        }
        return verdict.critic(), pairing

    def _write_verdict(
        self, finding_id: str, critic: dict[str, Any], pairing: dict[str, Any]
    ) -> None:
        def change(current: dict[str, Any]) -> dict[str, Any]:
            current["checks"] = {**(current.get("checks") or {}), "critic": critic["result"]}
            current["critic"] = critic
            current["pairing"] = pairing
            return current

        self.engine._update_finding(finding_id, change)

    def _ink(self, label: Mapping[str, Any]) -> tuple[int, float] | None:
        """The value's own ink on the drawings-only copy (GV's markup hidden): (ink pixels, the
        share in colour), or None when its picture cannot be drawn."""
        from app.review.assistant import set_pack

        workspace = self.engine.tools.workspace
        folder = workspace.set_pack
        if folder is None:
            return None
        try:
            pdf = set_pack.drawings_only(folder, workspace.uploads, str(label.get("file")))
            x0, y0, x1, y1 = (float(v) for v in label.get("box") or ())
            page = int(label.get("file_page") or label["page"])  # its own page (#1722)
            crop = set_pack.crop_picture(
                pdf, page, (x0, y0, x1, y1), 300, cache=folder / set_pack.HIRES
            )
            return ink_colour(crop.png)
        except Exception:  # noqa: BLE001 - unknown ink: the item is not red
            _log.info("simple check ink not looked at: %s", label.get("id"))
            return None

    def _read(self, file: str, page: int, box: Sequence[float], expected: str) -> dict[str, Any]:
        """`verify_value` at a spot (the printed text, then the drawn ink by two OCR engines)."""
        verify_value = import_module("app.review.assistant.verify_value")
        workspace = self.engine.tools.workspace
        pdf = workspace.uploads / file
        spot = {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
        cache = None if workspace.set_pack is None else workspace.set_pack / "hires"
        found = verify_value.verify_value(pdf, page, spot, expected, page_cache=cache)
        return dict(found.as_dict())

    # -- 3. the compare calls --

    def _compare_all(self, folder: Path, items: Sequence[WorkItem] | None = None) -> None:
        engine = self.engine
        items = self.compared if items is None else list(items)
        compare = _compare()
        prompts = import_module("app.review.assistant.simple_check.prompts")
        settings = compare.CompareSettings(**self.options.compare)
        system = prompts.system_prompt(engine._rules_summary(), engine.product_scope.label)
        source = compare.PictureSource(folder, engine.tools.workspace.uploads)
        try:
            source.prefetch(items)
        except Exception:  # noqa: BLE001 - each picture draws its page itself
            _log.info("simple check: pages not prefetched")
        watcher = threading.Thread(target=self._watch_cancel, daemon=True, name="gv-simple-stop")
        watcher.start()
        try:
            with engine.timings.phase("compare"):
                compare.compare_all(
                    items,
                    provider=engine.provider,
                    source=self._guarded(source.pictures, settings),
                    system=system,
                    settings=settings,
                    stop=self.stop,
                    on_outcome=self._on_outcome,
                )
        finally:
            self.stop.set()
            watcher.join(timeout=2)

    def _guarded(self, draw: Any, settings: Any) -> Any:
        """The compare call's picture source, guarded: a drawing starts only when what it may
        cost, with what the calls in flight may still cost, fits the per-answer cap (and the set's
        budget). Its cost is held until its answer is charged."""
        engine = self.engine

        def pictures(item: WorkItem) -> Any:
            if self.stop.is_set():
                raise _NotStarted("the check was stopped")
            sent = draw(item)
            model, _effort = settings.for_item(item)
            tokens = _REQUEST_TEXT_TOKENS + sum(int(one.picture.tokens) for one in sent)
            hold = engine._price(model, tokens, _REPLY_HOLD_TOKENS) or 0
            with self.lock:
                over = engine._over(self.reserved + hold)
                if over is not None:
                    self.cap_hit = self.cap_hit or over
                    self.refused.add(item.id)
                    self.stop.set()
                    raise _NotStarted("the per-answer cap would be passed")
                self.reserved += hold
                self.reservations[item.id] = hold
            self._tell("drawing_started", item.id)
            return sent

        return pictures

    def _watch_cancel(self) -> None:
        while not self.stop.wait(0.5):
            if self.engine.cancel.is_set():
                self.stop.set()

    def _on_outcome(self, outcome: CompareOutcome) -> None:
        """One item's compare call is done: its usage recorded and charged, its pictures logged as
        seen, the runaway guard checked, the progress moved on."""
        engine = self.engine
        with self.lock:
            self.reserved -= self.reservations.pop(outcome.work_item, 0)
            if outcome.work_item in self.refused:
                outcome = outcome.model_copy(
                    update={"reason": "not started: the per-answer cap would be passed"}
                )
            self.outcomes[outcome.work_item] = outcome
        self._save_outcome(outcome)
        model = _model(outcome.model) or _model(outcome.asked_model)
        if model is not None and outcome.usage:
            self._charge(model, outcome)
        if outcome.status == "ok":
            seen = [
                {
                    "by": "compare",
                    "file": picture.file,
                    "page": picture.own_page,
                    "box": dict(zip(("x0", "y0", "x1", "y1"), picture.box, strict=True)),
                    "dpi": picture.dpi,
                    "picture_id": None,
                }
                for picture in outcome.pictures
            ]
            engine.tools.seen.extend(seen)
        self._progress()
        ok = outcome.status == "ok"
        self._tell(
            "drawing_finished",
            outcome.work_item,
            "answered" if ok else "failed",
            None if ok else outcome.reason,
        )

    def _charge(self, model: ChatModel, outcome: CompareOutcome) -> None:
        from app.review.assistant.chat_engine import HARNESS_ID

        engine = self.engine
        usage = dict(outcome.usage)
        billed_in, out = engine._weighted(model.rates_key, usage)
        started = time.monotonic_ns() - int(outcome.seconds * 1_000_000_000)
        try:
            engine.ledger.record(
                model_key=model.rates_key,
                prompt_id=COMPARE_PROMPT_ID.format(
                    provider=engine.provider.name, harness=HARNESS_ID
                ),
                started_ns=started,
                input_tokens=billed_in,
                output_tokens=out,
                stop_reason="end_turn" if outcome.status == "ok" else None,
                error=None if outcome.status == "ok" else RuntimeError(outcome.reason or "failed"),
            )
        except Exception:  # noqa: BLE001 - the run goes on; the failure is logged
            _log.exception("simple check usage could not be recorded")
        # Charged on this worker thread, to the compare phase (the timings are per thread).
        with engine.timings.phase("compare"):
            engine._charge(model, usage)

    # -- 4. code verifies, the blind look --

    def _verify_all(self) -> list[tuple[Verified, Item]]:
        assert self.pack is not None
        found: list[tuple[Verified, Item]] = []
        products = self.engine.product_scope.products
        answered = sum(
            1
            for i in self.compared
            if (o := self.outcomes.get(i.id)) is not None and o.status == "ok"
        )
        self._tell("verifying", answered)
        for item in self.compared:
            outcome = self.outcomes.get(item.id)
            if outcome is None or outcome.status != "ok" or outcome.result is None:
                continue
            view = Item.of(item)
            try:
                verification = verify_module.verify_answer(
                    outcome.result,
                    view,
                    self.pack,
                    ink=self._ink,
                    read=self._read,
                    products=products,
                )
            except Exception as failed:  # noqa: BLE001 - the item fails, with the reason
                _log.exception("simple check: verify failed on %s", item.id)
                self.outcomes[item.id] = outcome.model_copy(
                    update={"status": "failed", "reason": f"code could not verify it ({failed})"}
                )
                self._tell("drawing_finished", item.id, "failed", self.outcomes[item.id].reason)
                self._tell("verified", item.id, ())
                continue
            found += [(v, view) for v in verification.items]
            self._tell("verified", item.id, verification.items, scoped=self._scoped)
            if verification.missing_labels:
                _log.info(
                    "simple check: %s leaves %d labels unaccounted",
                    item.id,
                    len(verification.missing_labels),
                )
        return found

    def _blind(self, verified: list[tuple[Verified, Item]]) -> list[tuple[Verified, Item]]:
        """Each red candidate's blind look: the saved one on a replay; none after a stop or when
        the money ran out (the item is then Not compared, "the blind look was not made"); else
        one call each. Every look is saved with the run."""
        candidates = [(v, item) for v, item in verified if v.needs_blind]
        if not candidates:
            return verified
        # The strict product scope: a candidate of another product is never paid a look (the
        # finding book would leave it out anyway). It ends Not compared, never red.
        outside = {
            id(v): with_blind(v, None, OUTSIDE_SCOPE)
            for v, _ in candidates
            if not self._scoped(v.finding)
        }
        candidates = [(v, item) for v, item in candidates if id(v) not in outside]
        if outside:
            _log.info("simple check: %d red candidates outside the product scope", len(outside))
        if not candidates:
            return [(outside.get(id(v), v), item) for v, item in verified]
        if self.options.replay and self.options.compare_missing:
            return self._looks_again(verified, candidates, outside)
        engine = self.engine
        looked: list[Verified]
        if self.options.replay:
            looked = [_with_saved(v, self.saved_blind.get(v.key)) for v, _item in candidates]
            self.blind_failed = sum(
                1 for done in looked if done.outcome != "red" and _no_look(done)
            )
        elif engine.cancel.is_set() or self.cap_hit is not None:
            why = (
                "the check was stopped"
                if engine.cancel.is_set()
                else "the per-answer cap was reached"
            )
            self.blind_skipped, self.blind_skipped_count = why, len(candidates)
            looked = [with_blind(v, None, why) for v, _item in candidates]
        else:
            self._tell("second_looks", len(candidates))
            looked = look_all(engine, candidates, self.options.blind, on_done=self._live_look)
            self.blind_failed = sum(
                1 for done in looked if done.outcome != "red" and _no_look(done)
            )
        for done in looked:
            self._save_blind(done)
        after = {id(old): new for (old, _item), new in zip(candidates, looked, strict=True)}
        after.update(outside)
        return [(after.get(id(v), v), item) for v, item in verified]

    def _looks_again(
        self,
        verified: list[tuple[Verified, Item]],
        candidates: list[tuple[Verified, Item]],
        outside: Mapping[int, Verified],
    ) -> list[tuple[Verified, Item]]:
        """Compare them again (#1710): each red candidate's saved look is used again ($0); a look
        the run never made, or that failed, is made now, under the same rules as a first run (none
        after a stop or when the money ran out)."""
        again = {
            id(v): _with_saved(v, self.saved_blind[v.key])
            for v, _item in candidates
            if _look_made(self.saved_blind.get(v.key))
        }
        fresh = [(v, item) for v, item in candidates if id(v) not in again]
        made: dict[int, Verified] = {}
        if fresh:
            engine = self.engine
            if engine.cancel.is_set() or self.cap_hit is not None:
                why = (
                    "the check was stopped"
                    if engine.cancel.is_set()
                    else "the per-answer cap was reached"
                )
                self.blind_skipped, self.blind_skipped_count = why, len(fresh)
                done = [with_blind(v, None, why) for v, _item in fresh]
            else:
                self._tell("second_looks", len(fresh))
                done = look_all(engine, fresh, self.options.blind, on_done=self._live_look)
                self.blind_failed = sum(1 for one in done if one.outcome != "red" and _no_look(one))
            made = {id(v): one for (v, _item), one in zip(fresh, done, strict=True)}
        looked = [again.get(id(v)) or made[id(v)] for v, _item in candidates]
        for one in looked:
            self._save_blind(one)
        after = {id(old): new for (old, _item), new in zip(candidates, looked, strict=True)}
        after.update(outside)
        return [(after.get(id(v), v), item) for v, item in verified]

    def _filed(self, row: Mapping[str, Any]) -> str | None:
        """The product a Not-compared row of this run is filed under (`refiled`, as a finding),
        or `None` when it is another product's."""
        engine = self.engine
        scope = getattr(engine, "product_scope", None)
        if scope is None:
            return str(row.get("category") or "other")
        page = row.get("page")
        words_of = getattr(engine, "page_words", None)
        words: Any = ""
        if callable(words_of) and isinstance(page, int):
            words = lambda: words_of(page)
        fields = {
            "category": row.get("category") or "other",
            "item": row.get("item") or "",
            "created_by": CREATED_BY,
        }
        return refiled(fields, scope, words)

    def _scoped(self, finding: Mapping[str, Any]) -> bool:
        """Whether an item of this run is of a product in the check's scope, as the finding book
        files it (`product_scope.refiled`: its product, else its name's words, then its page's)."""
        engine = self.engine
        scope = getattr(engine, "product_scope", None)
        if scope is None or scope.every or not finding:
            return True
        page = finding.get("page")
        words_of = getattr(engine, "page_words", None)
        words: Any = ""
        if callable(words_of) and isinstance(page, int):
            words = lambda: words_of(page)
        return refiled({**finding, "created_by": CREATED_BY}, scope, words) is not None

    # -- saved answers (re-verified at $0 later: `replay`) --

    def _open_run_dir(self) -> None:
        root = self.engine.tools.workspace.root
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.run_dir = root / SIMPLE_DIR / f"{stamp}-{uuid4().hex[:8]}"
        try:
            (self.run_dir / "compare").mkdir(parents=True, exist_ok=True)
            (self.run_dir / "blind").mkdir(parents=True, exist_ok=True)
            (self.run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "items": [item.id for item in self.compared],
                        "skipped": self.skipped,
                        "replay_of": self.options.replay,
                        "compare": dict(self.options.compare),
                        "blind": {
                            "model": self.options.blind.model,
                            "effort": self.options.blind.effort,
                        },
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            _log.exception("simple check: the run folder could not be made")
            self.run_dir = None

    def _save_outcome(self, outcome: CompareOutcome) -> None:
        if self.run_dir is None:
            return
        try:
            _write(
                self.run_dir / "compare" / f"{_safe(outcome.work_item)}.json",
                outcome.model_dump_json(indent=1),
            )
        except OSError:
            _log.exception("simple check: %s's answer could not be saved", outcome.work_item)

    def _save_blind(self, done: Verified) -> None:
        if self.run_dir is None:
            return
        pairing = done.finding.get("pairing") if isinstance(done.finding, Mapping) else None
        look = pairing.get("advocate") if isinstance(pairing, Mapping) else None
        record = {"key": done.key, "look": look, "outcome": done.outcome, "reason": done.reason}
        try:
            _write(self.run_dir / "blind" / f"{_safe(done.key)}.json", json.dumps(record, indent=1))
        except OSError:
            _log.exception("simple check: %s's blind look could not be saved", done.key)

    def _replayed(self) -> Path | None:
        return _replay_dir(self.engine.tools.workspace.root, self.options.replay)

    def _load_saved(self) -> list[WorkItem]:
        """A replay ($0): the saved answers of an earlier run of this chat, as if just answered (no
        model call; nothing charged again). With `compare_missing`, the drawings it has no answer
        for are returned (to be compared now) instead of failing."""
        source = self._replayed()
        found = load_outcomes(source) if source is not None else {}
        self.saved_blind = load_blind(source) if source is not None else {}
        missing: list[WorkItem] = []
        for item in self.compared:
            outcome = found.get(item.id)
            if self.options.compare_missing and (outcome is None or outcome.status != "ok"):
                missing.append(item)
                continue
            if outcome is None:
                outcome = CompareOutcome(
                    work_item=item.id,
                    status="failed",
                    reason="no saved answer in the run replayed",
                    asked_model="",
                    effort="",
                )
            else:
                # Replayed, not paid again: its pictures still count as seen.
                outcome = outcome.model_copy(update={"usage": {}, "cost_usd": 0.0})
            self._on_outcome(outcome)
        return missing

    # -- 5. the findings and the rows --

    def _record(self, verified: list[tuple[Verified, Item]]) -> None:
        engine = self.engine
        # A vendor filler GV's filler range already decided (`filler_range`, code's own finding):
        # the compare answer's Right or Not-compared item on the same label is not listed again
        # (a red from the architect comparison still is).
        decided = covered_labels(engine.book.live(), engine.book.main_file)
        verified = [
            pair
            for pair in verified
            if pair[0].outcome == "red" or self._label_key(pair[0]) not in decided
        ]
        ordered = sorted(
            (pair for pair in verified if pair[0].finding),
            key=lambda pair: _ORDER.get(pair[0].outcome, 3),
        )
        for one, _item in ordered:
            if one.outcome == "candidate":
                continue  # never left so: the blind look ran on every candidate
            kept = self._add(one)
            if kept is None:
                # Another product than the check's (the strict scope): never listed, not even
                # as Not compared; the finding book counted it.
                self.out_of_scope += 1
            elif not kept:
                if one.outcome == "right":
                    self.not_listed += 1
                else:
                    self.rows.append(_row(one, "the answer's item limit was reached"))
        # The labels the architect gives no size for, one row per drawing (and product), not one
        # per label: a set has hundreds (the app test: 1,186 rows beside 46 items on screen).
        # Labels the AI was unsure about (no architect label) are grouped the same way, with their
        # own reason.
        no_size: dict[tuple[str, str, str], list[Verified]] = {}
        for one, item in verified:
            if one.outcome != "listed":
                continue
            row = _row(one)
            if one.reason_code not in _GROUPED or not one.vendor:
                self.rows.append(row)
                continue
            filed = self._filed(row)
            if filed is None:
                self.out_of_scope += 1  # another product's label: never listed
                continue
            no_size.setdefault((item.id, filed, str(one.reason_code)), []).append(one)
        for (item_id, filed, _reason), labels in no_size.items():
            self.rows.append(_no_size_row(item_id, filed, labels))
        engine.extra_gaps = list(self.rows)

    def _label_key(self, one: Verified) -> tuple[str, int, str] | None:
        vendor = one.vendor
        if not isinstance(vendor, Mapping) or not vendor.get("id"):
            return None
        try:
            page = int(vendor.get("page") or 0)
        except (TypeError, ValueError):
            return None
        file = str(vendor.get("file") or self.engine.book.main_file or "")
        return (file, page, str(vendor["id"]))

    def _add(self, one: Verified) -> bool | None:
        """The item as a finding: merged onto GV Review's recorded row of the same item (same page,
        same two values, no spot yet), else added. False when the book is full; None when the
        finding book left it out as another product than the check's (the strict scope)."""
        engine = self.engine
        book = engine.book
        fields: dict[str, Any] = own_pages(one.finding, self.pack)
        extra: dict[str, Any] = {
            k: fields.pop(k) for k in ("critic", "pairing", "keep_reason", "round") if k in fields
        }
        extra["simple"] = one.record()
        with engine.finding_lock:
            seed = _seed_for(fields, book.live())
            if seed is not None:
                changes = {
                    k: v
                    for k, v in {**fields, **extra}.items()
                    if k not in ("item", "category", "created_by", "page")
                }
                changed = book.prove_by_code(str(seed["id"]), changes)
                if changed is not None:
                    engine._show_finding(changed)
                    return True
            before = book.out_of_scope
            added = book.add_code_finding(fields, created_by=CREATED_BY)
            if added is None:
                return None if book.out_of_scope > before else False
            updated = {**added, **extra}
            checks = dict(added.get("checks") or {})
            critic = extra.get("critic")
            if isinstance(critic, Mapping):
                checks["critic"] = critic.get("result")
            for key, read in one.reads.items():
                existing = checks.get(key)
                matched = isinstance(existing, Mapping) and existing.get("result") == "match"
                if isinstance(read, Mapping) and not matched:
                    checks[key] = dict(read)
            updated["checks"] = checks
            updated = judged(updated)
            if book.replace(updated):
                engine._show_finding(updated)
        return True

    # -- 6. the finish --

    def _finish(self, stopped: str | None, why: str | None = None) -> MessageOutcome:
        engine = self.engine
        from app.review.assistant.chat_engine import MessageOutcome

        cap_hit = self.cap_hit if stopped == "cost_cap" else None
        failed = [
            item
            for item in self.compared
            if (o := self.outcomes.get(item.id)) is None or o.status != "ok"
        ]
        left = sum(1 for reason in self.skipped.values() if reason.startswith(_PILOT))
        if why is not None:
            engine.incomplete_note = why
        elif failed:
            engine.incomplete_note = (
                f"{len(failed)} of {len(self.compared)} drawings could not be compared"
            )
        elif left:
            # A pilot is not a full check: never a review result for the vendor (S0).
            engine.incomplete_note = (
                f"a pilot run: {len(self.compared)} of {len(self.compared) + left} drawings "
                "compared"
            )
        elif not self.compared:
            # Nothing was compared (no vendor drawing, or none of the products picked): no review
            # result says the drawings were checked.
            engine.incomplete_note = self._nothing_compared()
        elif self.blind_failed:
            # A failed second look counts like a failed compare call (orchestrator, 2026-10-19):
            # an outage, not the drawings, left these possible differences undecided.
            engine.incomplete_note = (
                f"{self.blind_failed} possible "
                f"{'difference' if self.blind_failed == 1 else 'differences'} could not get "
                "their second check (the service did not answer); check again to settle them"
            )
        self._cover_answered(failed)
        try:
            engine._store_scope()
        except Exception:  # noqa: BLE001 - stored without its scope block
            _log.exception("simple check scope failed")
        if stopped == "cancelled":
            engine._code_finish()
        else:
            try:
                engine._trust_phase()
            except Exception:  # noqa: BLE001 - stored without its trust report
                _log.exception("simple check trust report failed")
        engine._close_checks()
        files: list[str] = []
        if engine._progress is not None:
            engine._progress.phase("writing")
        self._tell("making_files", engine.book.live())
        try:
            files = engine._full_check_files(stopped)
        except Exception:  # noqa: BLE001 - the answer still ends, with its summary
            _log.exception("simple check files failed")
        text = self._summary(stopped, cap_hit, files, failed, why)
        index = engine._add_block({"type": "text", "text": text, "by": "code"})
        engine.emit("text_delta", {"index": index, "text": text, "by": "code"})
        rows = work_item_rows(self.items, self.outcomes, self.skipped)
        engine._add_block(
            {
                "type": "work_items",
                "work_items": rows,
                "run": self.run_dir.name if self.run_dir is not None else None,
            }
        )
        try:
            engine._accuracy()
        except Exception:  # noqa: BLE001 - stored without its score
            _log.exception("simple check accuracy failed")
        text = engine._agree_counts(text)
        engine._finish_timings()
        api_content = [
            {"role": "user", "content": engine.user_content},
            {"role": "assistant", "content": [{"type": "text", "text": text}]},
        ]
        return MessageOutcome(
            text=text,
            blocks=engine.stored_blocks(),
            api_content=json.loads(json.dumps(api_content)),
            cost_micros=engine.spent,
            stopped=stopped,
            cap_hit=cap_hit,
            seen=[dict(entry) for entry in engine.tools.seen],
        )

    def _cover_answered(self, failed: Sequence[WorkItem]) -> None:
        """Each compared page in the coverage map: different (a red item on it), same, or not
        reached with the reason when its drawing's compare call failed."""
        engine = self.engine
        live = engine.book.live()
        red_pages = {
            int(f.get("page") or 0)
            for f in live
            if f.get("created_by") == CREATED_BY and f.get("status") == "different"
        }
        failing = {item.page: (self.outcomes.get(item.id)) for item in failed}
        pages: list[dict[str, Any]] = []
        for page in sorted({item.page for item in self.compared}):
            if page in failing:
                outcome = failing[page]
                reason = (outcome.reason if outcome is not None else None) or "not reached"
                pages.append(
                    {"page": page, "status": "not_opened", "note": f"Not compared: {reason}"[:290]}
                )
            else:
                status = "different" if page in red_pages else "same"
                pages.append({"page": page, "status": status})
        if pages:
            try:
                engine.book.set_coverage({"pages": pages}, "code")
                engine._show_coverage()
            except Exception:  # noqa: BLE001 - said in the summary
                _log.warning("simple check could not record the compared pages")

    def _nothing_compared(self) -> str:
        """Why no drawing was compared (no error): the work list has none, or every one shows
        only products outside the check's scope."""
        outside = sum(1 for r in self.skipped.values() if r.startswith("outside the product scope"))
        if self.items and outside == len(self.items):
            label = str(getattr(self.engine.product_scope, "label", "") or "").lower()
            return (
                f"no drawing of {label or 'the products picked'} in this set: the "
                f"{len(self.items)} vendor drawings show only other products"
            )
        # The work list reads GV's combined sheets (the architect's drawing pasted over the
        # vendor's, each headed by whose it is). Separately uploaded vendor and architect files
        # are not paired yet: never a review result as if they were checked.
        return (
            "no vendor drawing was found on GV's combined sheets (the architect's drawing over "
            "the vendor's, each headed by whose it is); separately uploaded vendor and architect "
            "files are not compared yet"
        )

    def _summary(
        self,
        stopped: str | None,
        cap_hit: str | None,
        files: Sequence[str],
        failed: Sequence[WorkItem],
        why: str | None,
    ) -> str:
        engine = self.engine
        book, settings = engine.book, engine.settings
        from app.review.assistant.chat_engine import cap_text

        text = code_summary(
            book.live(),
            coverage=book.coverage_map()["counts"] if book.coverage_used else None,
            page_count=engine.tools.page_count,
            products=settings.products,
            # A Stop is said below in this check's own words (what was compared and verified).
            stopped=None if stopped == "cancelled" else stopped,
            cap_hit=cap_hit,
            cap_usd=cap_text(settings.message_cap_micros),
            budget_usd=cap_text(settings.package_cap_micros),
            turns=settings.lead_max_turns,
            files=files,
            reasons=[row.reason for row in engine.not_compared] or None,
        )
        pilot = [why for why in self.skipped.values() if why.startswith(_PILOT)]
        # A check that did not finish is never "send it to the vendor" (its cover and letter say
        # "Check incomplete: not for sending").
        unfinished = (
            why is not None
            or bool(failed)
            or stopped is not None
            or bool(pilot)
            or not self.compared
            or bool(self.blind_failed)
        )
        nothing_ran = why is not None or (not self.compared and not failed)
        kept: list[str] = []
        for line in text.splitlines():
            if line.strip() == CODE_SUMMARY_NOTE:
                continue
            if nothing_ran and _AS_IF_CHECKED.match(line.strip()):
                continue
            if nothing_ran and not engine.book.live() and re.match(r"^\d+ wrong ", line):
                continue  # "0 wrong, 0 right, 0 not compared": nothing was looked at
            if line.startswith("Next:"):
                line = NOTHING_NEXT if nothing_ran else (NOT_FINISHED_NEXT if unfinished else line)
            kept.append(line)
        text = "\n".join(kept).strip()
        lines = [text]
        answered = sum(1 for item in self.compared if item.id not in {i.id for i in failed})
        if nothing_ran:
            # One honest sentence, first: nothing above it reads as a finished check.
            honest = (
                f"**The check could not run: {why}. Nothing was compared.**"
                if why is not None
                else f"**Nothing was compared: {self._nothing_compared()}.**"
            )
            lines.insert(0, honest)
        elif failed:
            names = ", ".join(
                f"{item.id} ({(self.outcomes[item.id].reason if item.id in self.outcomes else None) or 'not reached'})"
                for item in failed[:8]
            )
            more = f" and {len(failed) - 8} more" if len(failed) > 8 else ""
            lines.append(
                f"**Check incomplete: {len(failed)} of {len(self.compared)} drawings could not be "
                f"compared: {names}{more}. Not for sending until they are.**"
            )
        elif not pilot:
            lines.append(f"Every drawing in the work list was compared ({answered} drawings).")
        if stopped == "cancelled" and why is None:
            lines.append(
                f"**Stopped: {answered} of {len(self.compared)} drawings were compared and "
                "verified; the rest were not started.**"
            )
        if self.blind_skipped:
            lines.append(
                f"{self.blind_skipped_count} possible differences got no blind second look "
                f"({self.blind_skipped}): they are Not compared, never red."
            )
        if self.blind_failed:
            lines.append(
                f"{self.blind_failed} possible differences could not get their blind second look "
                "(the call failed or its reply was unusable): they are Not compared, never red, "
                "and the check is not for sending. Check again to settle them."
            )
        if pilot:
            lines.append(
                f"Pilot run: {len(self.compared)} drawings compared; {len(pilot)} more left out "
                "on purpose (not a full check)."
            )
        if self.not_listed:
            lines.append(
                f"{self.not_listed} more items match the architect but are not listed (the "
                "answer's item limit)."
            )
        lines.append(f"This check used ${engine.spent / _MICROS:.2f}.")
        if self.run_dir is not None and self.outcomes:
            lines.append(
                f"Every answer is saved with the chat (run {self.run_dir.name}): it can be checked "
                "again at no cost."
            )
        # The screen's note under a code-written summary: without one it says "the AI did not
        # finish its own", which reads like a failure; the simple check has no AI summary at all.
        lines.append(SIMPLE_SUMMARY_NOTE)
        return "\n\n".join(lines)


def _pack_state(folder: Path) -> str | None:
    try:
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return str(status.get("state")) if isinstance(status, Mapping) else None


def _wait_for_pack(folder: Path, cancel: threading.Event) -> str | None:
    """Waits until the set pack is made (its labels and code's pairs too); why not, else None."""
    ends = time.monotonic() + PACK_WAIT_SECONDS
    while True:
        state = _pack_state(folder)
        if state == "ready":
            return None
        if state == "failed":
            return "the drawing set's pack (labels, pairs) could not be made"
        if time.monotonic() >= ends:
            return "the drawing set's pack (labels, pairs) was still being made"
        if cancel.wait(_PACK_POLL_SECONDS):
            return None


class _NotStarted(RuntimeError):
    """A drawing the run did not start (a stop, or the runaway guard)."""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "item"


def _write(path: Path, text: str) -> None:
    held = path.with_name(f".{path.name}.{uuid4().hex[:6]}")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _replay_dir(root: Path, run: str | None) -> Path | None:
    """The run folder a replay names, inside this chat's `simple/` folder only."""
    if not run or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run):
        return None
    folder = root / SIMPLE_DIR / run
    return folder if folder.is_dir() else None


def load_outcomes(run_dir: Path) -> dict[str, CompareOutcome]:
    """A run's saved compare answers, by work item."""
    out: dict[str, CompareOutcome] = {}
    for path in sorted((run_dir / "compare").glob("*.json")):
        try:
            outcome = CompareOutcome.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _log.warning("simple check: saved answer %s not read", path.name)
            continue
        out[outcome.work_item] = outcome
    return out


def load_blind(run_dir: Path) -> dict[str, dict[str, Any]]:
    """A run's saved blind looks, by the candidate's key."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "blind").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("key"):
            out[str(record["key"])] = record
    return out


def _no_look(done: Verified) -> bool:
    """A red candidate whose blind look was asked for but could not be made (`blind.look`'s
    reasons: the call failed, the reply was cut short or not the form, its pictures)."""
    return "blind second look was not made" in done.reason and not any(
        why in done.reason for why in ("the check was stopped", "runaway guard")
    )


def _look_made(saved: Mapping[str, Any] | None) -> bool:
    """A saved blind look that was made and answered (not one that failed or was never made)."""
    look = saved.get("look") if isinstance(saved, Mapping) else None
    return isinstance(look, Mapping) and not look.get("failed")


def _with_saved(candidate: Verified, saved: Mapping[str, Any] | None) -> Verified:
    """A candidate after its saved blind look (a replay): the same answer, never a new call."""
    look = saved.get("look") if isinstance(saved, Mapping) else None
    if not isinstance(look, Mapping):
        return with_blind(candidate, None, "no saved blind look in the run replayed")
    if look.get("failed"):
        return with_blind(candidate, None, str(look["failed"]))
    return with_blind(candidate, BlindAnswer.from_dict(look))


def _model(name: object) -> ChatModel | None:
    return MODELS.get(str(name)) if name else None


def _same(value: object, want: Decimal) -> bool:
    try:
        return Decimal(str(value)) == want
    except (InvalidOperation, ValueError):
        return False


def _seed_for(
    fields: Mapping[str, Any], live: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """GV Review's recorded row of the same item: on the same page, no vendor spot yet, printing
    the same two values (the pair defines the item; `pair_findings.same_item`'s rule)."""
    if fields.get("status") != "different":
        return None
    vendor = to_inches(str(fields.get("vendor") or ""))
    architect = to_inches(str(fields.get("architect") or ""))
    if vendor is None or architect is None:
        return None
    for other in live:
        if (
            other.get("created_by") == "gv_review"
            and other.get("source") == "gv_review"
            and other.get("page") == fields.get("page")
            and not other.get("vendor_box")
            and to_inches(str(other.get("vendor") or "")) == vendor
            and to_inches(str(other.get("architect") or "")) == architect
        ):
            return other
    return None


def own_pages(finding: Mapping[str, Any], pack: Pack | None) -> dict[str, Any]:
    """A finding as the chat's finding book keeps it: every spot on its file's own page (#1722).
    Verify names pages by set page (a separate architect file is numbered after the vendor's); the
    book, the evidence and the outputs read each spot's file at its own page. The vendor's file
    keeps its numbers, so a combined set's finding is unchanged."""
    fields = dict(finding)
    if pack is None or pack.pages is None or not pack.pages.separate:
        return fields
    for key in ("arch_box", "vendor_box"):
        box = fields.get(key)
        if isinstance(box, Mapping) and isinstance(box.get("page"), int) and box.get("file"):
            fields[key] = {**box, "page": pack.own_page(str(box["file"]), int(box["page"]))}
    dims = fields.get("dims")
    if isinstance(dims, Mapping):
        fields["dims"] = {
            side: (
                {**label, "page": pack.own_page(str(label["file"]), int(label["page"]))}
                if isinstance(label, Mapping)
                and isinstance(label.get("page"), int)
                and label.get("file")
                else label
            )
            for side, label in dims.items()
        }
    spot = fields.get("arch_box")
    if isinstance(fields.get("vs_page"), int):
        if isinstance(spot, Mapping) and isinstance(spot.get("page"), int):
            fields["vs_page"] = int(spot["page"])
        else:
            file, page = pack.pages.file_of(int(fields["vs_page"]))
            fields["vs_page"] = page if file != pack.main_file else fields["vs_page"]
    return fields


#: What settles a single listed label, by its reason.
_ROW_NEEDS: Final = {
    "nothing_to_compare": "the architect's size for it",
    "ai_unsure": "a printed architect value for it, or a clearer look at both drawings",
}


def _row(one: Verified, why: str | None = None) -> dict[str, Any]:
    """A Not-compared row that is not a finding (`not_compared.gap_rows`'s shape)."""
    vendor = one.vendor or {}
    where = (
        f"{vendor.get('text')} (page {vendor.get('page')}, label {vendor.get('id')})"
        if vendor
        else one.key
    )
    about = one.about or {}
    finding = one.finding or {}
    page = vendor.get("page") if vendor else (about.get("page") or finding.get("page"))
    return {
        "page": page if isinstance(page, int) else None,
        "what": f"{where}: {why or one.reason}",
        "why": one.reason_code or "not_proved",
        "needs": _ROW_NEEDS.get(str(one.reason_code), ""),
        "scope": "set",
        # What files the row under the check's product scope (`product_scope.scoped_gaps`).
        "item": about.get("item") or finding.get("item"),
        "category": about.get("category") or finding.get("category"),
    }


#: How many values a grouped "no architect size" row names before "and N more".
_NO_SIZE_SHOWN: Final = 6
#: The listed labels grouped one row per drawing, and what each group's row says about them.
_GROUP_WORDS: Final = {
    "nothing_to_compare": (
        "the architect gives no size for",
        "the architect's sizes for them",
        "dimensions with no architect size",
    ),
    "ai_unsure": (
        "not settled from the drawings (no printed architect value found):",
        "a printed architect value for them, or a clearer look at both drawings",
        "dimensions not settled from the drawings",
    ),
}
_GROUPED: Final = frozenset(_GROUP_WORDS)


def _no_size_row(item_id: str, filed: str, labels: Sequence[Verified]) -> dict[str, Any]:
    """One Not-compared row for a drawing's labels the architect gives no size for (or the AI was
    unsure about, with no architect label): the page, how many, and the first few values with
    their labels."""
    reason = str(labels[0].reason_code or "nothing_to_compare")
    said, needs, named = _GROUP_WORDS.get(reason, _GROUP_WORDS["nothing_to_compare"])
    vendor = labels[0].vendor or {}
    page = vendor.get("page")
    shown = ", ".join(
        f"{(one.vendor or {}).get('text')} ({(one.vendor or {}).get('id')})"
        for one in labels[:_NO_SIZE_SHOWN]
    )
    more = f" and {len(labels) - _NO_SIZE_SHOWN} more" if len(labels) > _NO_SIZE_SHOWN else ""
    many = "dimension" if len(labels) == 1 else "dimensions"
    return {
        "page": page if isinstance(page, int) else None,
        "what": (
            f"Vendor drawing on page {page}: {len(labels)} {many} {said.rstrip(':')}: {shown}{more}"
        ),
        "why": reason,
        "needs": needs,
        "scope": "set",
        "item": f"{item_id}: {named}",
        "category": filed,
    }


def pack_ink(pack_dir: Path, main_file: str | None = None) -> Any:
    """A label's own ink on the pack's markup-free copy (`verify._ink_check`'s `ink`)."""
    from app.review.assistant import set_pack

    def ink(label: Mapping[str, Any]) -> tuple[int, float] | None:
        name = str(label.get("file") or main_file or "")
        pdf = pack_dir / f"{set_pack.stem(name)}_drawings_only.pdf"
        try:
            x0, y0, x1, y1 = (float(v) for v in label.get("box") or ())
            page = int(label.get("file_page") or label["page"])  # its own page (#1722)
            crop = set_pack.crop_picture(
                pdf, page, (x0, y0, x1, y1), 300, cache=pack_dir / set_pack.HIRES
            )
            return ink_colour(crop.png)
        except Exception:  # noqa: BLE001 - unknown ink: not red
            return None

    return ink


def pack_reader(pack_dir: Path) -> ValueReader:
    """`verify_value` at a spot of the pack's markup-free copy (the same reading as the upload:
    `verify_value` leaves GV's markup out either way)."""
    from app.review.assistant import set_pack

    verify_value = import_module("app.review.assistant.verify_value")

    def read(file: str, page: int, box: Sequence[float], expected: str) -> dict[str, Any]:
        pdf = pack_dir / f"{set_pack.stem(file)}_drawings_only.pdf"
        spot = {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
        found = verify_value.verify_value(
            pdf, page, spot, expected, page_cache=pack_dir / set_pack.HIRES
        )
        return dict(found.as_dict())

    return read


def replay(
    run_dir: Path,
    pack_dir: Path,
    *,
    main_file: str | None = None,
    products: Sequence[str] | None = None,
    read: ValueReader | None = None,
    ink: Any = None,
) -> dict[str, list[Verified]]:
    """A saved run verified again at $0 (no model call): each answered work item's items, by
    work item id, after the run's own saved blind looks (a candidate with none saved stays Not
    compared). The same code as the run: the pack's work list and facts, `verify_answer`, the
    markup-free copy's ink and `verify_value` readings unless others are given."""
    worklist = import_module("app.review.assistant.simple_check.worklist")
    items = worklist.build_worklist(pack_dir, main_file).items
    pack = Pack.from_folder(pack_dir, main_file)
    outcomes = load_outcomes(run_dir)
    saved = load_blind(run_dir)
    looks = ink if ink is not None else pack_ink(pack_dir, main_file)
    reader = read if read is not None else pack_reader(pack_dir)
    out: dict[str, list[Verified]] = {}
    for item in items:
        outcome = outcomes.get(item.id)
        if outcome is None or outcome.status != "ok" or outcome.result is None:
            continue
        found = verify_module.verify_answer(
            outcome.result, Item.of(item), pack, ink=looks, read=reader, products=products
        )
        out[item.id] = [
            _with_saved(v, saved.get(v.key)) if v.needs_blind else v for v in found.items
        ]
    return out


def run(engine: ChatEngine) -> MessageOutcome:
    """The simple full check (see the module)."""
    return _Run(engine, SimpleOptions.of(engine.settings.simple)).run()
