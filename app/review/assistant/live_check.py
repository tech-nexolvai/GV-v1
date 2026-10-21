"""The full check's live state, for the live screen (#1710): drawings, phases, decided findings.

While a full check runs, the screen shows the reviewer's own drawings being checked (one tile per
vendor drawing: waiting, being compared, answered, failed or skipped, with its area), one plain
phase with its counts (getting ready, comparing n of m drawings, double-checking n of m second
looks, making n of 5 files), each finding as soon as code has DECIDED it, a count of the
differences waiting for the blind second look, and "stopping" after Stop. This module keeps that
state for one running message (`LiveCheck`, made by `chat_service` for a full check) and sends each
change as one small versioned event; `snapshot()` is the whole state in one call, for a page that
reconnects mid-run (`GET /chats/{chat_id}/live`, and `ChatOut.live`).

**Events** (each `{"v": LIVE_VERSION, <name>: <shape>}`; the shapes are `chat_contract`'s
`ChatLive*Out`). Additive: a screen that does not know them skips them.

- `check_phase` (`phase`): the phase, its step, done/total and unit, the check's start time.
- `check_drawings` (`drawings`): the whole work list at once, every drawing `waiting` (or
  `skipped` with code's reason); then `check_drawing` (`drawing`) each time one changes.
- `check_finding` (`finding`): one decided finding (or a change to one), with its outcome
  (`wrong`, `right`, `rule_note` + the question for the architect, `not_compared` + reason and
  what is needed), impact and product.
- `check_tally` (`tally`): Wrong / Right / Not compared so far and `waiting_second_look`.
- `check_stopping` (`stopping`): after Stop, the drawings still finishing and those never started.
- `check_service` (`reason`): the AI service stopped every call (a refused key, no payment, not
  reachable): the provider's plain reason, once.

**Honest counts** (Anant 2026-10-19, owner choice 1: only proved Wrongs while it runs, plus a count
of those waiting for a second look; no red ever appears and then disappears):

- a finding counts only once the run has DECIDED it (code's own findings after their code checks;
  each drawing's items once code verified them; each red candidate after its blind look; every
  finding as recorded; the final list before the files), never on its first raw `finding` event;
- Wrong is `proof.proof_group == "clouded"` (the cloud drawing's own rule): a candidate the blind
  look has not seen yet is only counted in `waiting_second_look`, and a candidate the look agreed
  with shows as Wrong only when that rule already says so on it (else it waits for its record);
- Wrongs that are the same item (same page, round and both values) count once (`same_as`): the final
  list folds them (`full_check_lenses.same_wrongs`, which also needs the same spot), so the live
  Wrong count never passes the final one;
- Right and Not compared are provisional until `final` (the final checks may move an item between
  them); a Wrong that a later check takes back is sent as such (`previous: "wrong"`), never hidden,
  and logged loudly (it is a bug: it should not happen).

The simple full check calls the hooks (`work_list`, `drawing_started`, `drawing_finished`,
`code_checks`, `verified`, `second_look`, `decide`, `making_files`); the roster calls none, so there the state
follows the `progress` events (pages, its three phases) and the findings are decided once, at
`writing`. Thread-safe: the compare and blind workers report at once; every event is published
under the state's lock, so the screen sees the changes in the order they were made. This module
never calls into the engine (no engine lock is taken under this one).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.review.assistant.finding_checks import to_inches
from app.review.assistant.impact import impact_of
from app.review.assistant.proof import proof_of

__all__ = [
    "EVENTS",
    "FILES_EXPECTED",
    "LIVE_VERSION",
    "LiveCheck",
]

_log = logging.getLogger(__name__)

#: The events' and the snapshot's version: a change a screen cannot read raises it.
LIVE_VERSION: Final = 1
#: The live events, in the order a check sends them first.
EVENTS: Final = (
    "check_phase",
    "check_drawings",
    "check_drawing",
    "check_finding",
    "check_tally",
    "check_stopping",
    "check_service",
)
#: The files a full check makes: cloud drawing, check report, Not-compared PDF and workbook, and
#: the package ("making n of 5 files"; the count follows the files really made).
FILES_EXPECTED: Final = 5
#: The engine's events this state follows.
_OBSERVED: Final = frozenset({"finding", "finding_dropped", "file", "progress"})
_OUTCOME: Final[Mapping[str, str]] = {
    "clouded": "wrong",
    "right": "right",
    "dismissed": "right",
    # #1839: the architect's size breaks a GV rule and the vendor follows it: decided.
    "rule_note": "rule_note",
    "not_compared": "not_compared",
}
#: The roster's progress phases as the live phases.
_ROSTER_PHASE: Final[Mapping[str, str]] = {
    "reading": "comparing",
    "checking": "double_checking",
    "writing": "making_files",
}

type Publish = Callable[[str, dict[str, Any]], None]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: object) -> str | None:
    """A value as code's inches (`45.25`), so two spellings of one size are one key."""
    if value in (None, ""):
        return None
    exact: Decimal | None
    try:
        exact = Decimal(str(value))
    except (InvalidOperation, ValueError):
        exact = to_inches(str(value))
    if exact is None:
        return " ".join(str(value).lower().split())
    return format(exact.normalize(), "f")


def _same_item_key(row: Mapping[str, Any], finding: Mapping[str, Any]) -> tuple[Any, ...]:
    """What makes two Wrongs the same item for the live count: page, round, both values."""
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    vendor = _number((inches or {}).get("vendor")) or _number(finding.get("vendor"))
    architect = _number((inches or {}).get("architect")) or _number(
        finding.get("architect") or row.get("should_be")
    )
    return (row.get("page"), finding.get("round"), vendor, architect)


class LiveCheck:
    """One running full check's live state (see the module)."""

    def __init__(
        self,
        publish: Publish,
        *,
        mode: str = "simple",
        service: Callable[[], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._publish = publish
        self.mode = "simple" if mode == "simple" else "roster"
        self._service_of = service
        self._now = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self.started_at = self._now()
        self._phase: dict[str, Any] = {
            "phase": "getting_ready",
            "step": None,
            "done": None,
            "total": None,
            "unit": None,
            "phase_started_at": _iso(self.started_at),
        }
        self._drawings: dict[str, dict[str, Any]] = {}
        self._areas: dict[int, str] = {}
        self._rows: dict[str, dict[str, Any]] = {}
        """Decided findings by their live key (a finding id, or a candidate's key until recorded)."""
        self._member: dict[str, str] = {}
        """Finding id (or candidate key) -> the live key of its row."""
        self._facts: dict[str, dict[str, Any]] = {}
        """Each row's grouping facts: its Wrong key, round."""
        self._latest: dict[str, dict[str, Any]] = {}
        """The newest version of every finding seen (the roster decides from these)."""
        self._waiting: set[str] = set()
        self._held: set[str] = set()
        """Candidates the blind look agreed with that the proof rule does not show as Wrong yet:
        still waiting, settled when recorded."""
        self._verify = [0, 0]
        self._looks = [0, 0]
        self._files = 0
        self._stopping: dict[str, int] | None = None
        self._service: str | None = None
        self._last_tally: dict[str, int] | None = None
        self._started = False
        self._ended = False
        self.withdrawn = 0
        """Wrongs a later check took back (should stay 0; each is logged)."""

    # ---- start, phases, the end -----------------------------------------------------------------

    def start(self) -> None:
        """The check has started (after `message_start`): getting ready, with its start time."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._send_phase()

    def phase(
        self,
        name: str,
        *,
        step: str | None = None,
        done: int | None = None,
        total: int | None = None,
        unit: str | None = None,
    ) -> None:
        with self._lock:
            if self._ended:
                return
            changed = name != self._phase["phase"] or step != self._phase["step"]
            self._phase.update(
                {"phase": name, "step": step, "done": done, "total": total, "unit": unit}
            )
            if changed:
                self._phase["phase_started_at"] = _iso(self._now())
            self._send_phase()

    def making_files(self, findings: Sequence[Mapping[str, Any]]) -> None:
        """The final list is known (after the final checks): every finding decided for good, then
        the files' phase."""
        with self._lock:
            self.decide(findings, final=True)
            self.phase(
                "making_files",
                done=self._files,
                total=max(FILES_EXPECTED, self._files),
                unit="files",
            )

    def finish(self, stopped: str | None = None) -> None:
        """The answer is being stored: `done` (the last event before `message_end`)."""
        with self._lock:
            if self._ended:
                return
            self._check_service()
            self._waiting.clear()
            self._held.clear()
            self._send_tally()
            self._phase.update(
                {
                    "phase": "done",
                    "step": "stopped" if stopped == "cancelled" else None,
                    "done": None,
                    "total": None,
                    "unit": None,
                    "phase_started_at": _iso(self._now()),
                }
            )
            self._send_phase()
            self._ended = True

    # ---- the drawings ---------------------------------------------------------------------------

    def work_list(self, items: Iterable[Any], skipped: Mapping[str, str]) -> None:
        """The whole work list at once: every drawing waiting, or skipped with code's reason."""
        with self._lock:
            self._drawings = {}
            for item in items:
                area = getattr(item, "area", None) or None
                page = int(getattr(item, "page", 0) or 0)
                if area and page not in self._areas:
                    self._areas[page] = str(area)
                why = skipped.get(str(item.id))
                self._drawings[str(item.id)] = {
                    "id": str(item.id),
                    "page": page,
                    "round": getattr(item, "round", None),
                    "area": None if area is None else str(area),
                    "status": "skipped" if why is not None else "waiting",
                    "reason": why,
                }
            self._send("check_drawings", "drawings", [dict(d) for d in self._drawings.values()])
            self._send_phase()

    def drawing_started(self, item_id: str) -> None:
        with self._lock:
            drawing = self._drawings.get(item_id)
            if drawing is None or drawing["status"] != "waiting":
                return
            if self._phase["phase"] != "comparing":
                self._comparing()
            drawing["status"] = "comparing"
            self._send("check_drawing", "drawing", dict(drawing))

    def drawing_finished(self, item_id: str, status: str, reason: str | None = None) -> None:
        """A drawing's compare call ended (`answered` or `failed` with the reason), or code could
        not verify an answered one (`failed`)."""
        with self._lock:
            drawing = self._drawings.get(item_id)
            if drawing is None or drawing["status"] == "skipped":
                return
            if (drawing["status"], drawing["reason"]) == (status, reason):
                return
            drawing["status"] = status
            drawing["reason"] = reason
            self._send("check_drawing", "drawing", dict(drawing))
            if status == "failed":
                self._check_service()
            if self._phase["phase"] == "comparing":
                self._comparing()
            if self._stopping is not None:
                self._send_stopping()

    def compare_started(self) -> None:
        """The compare calls start (the drawings' phase, 0 of m)."""
        with self._lock:
            self._comparing()

    def stopping(self) -> None:
        """Stop was pressed: the drawings being compared finish; nothing new starts."""
        with self._lock:
            if self._ended or self._stopping is not None:
                return
            self._stopping = {}
            self._send_stopping()
            self._send_phase()

    # ---- the findings ---------------------------------------------------------------------------

    def observe(self, event: str, data: Mapping[str, Any]) -> None:
        """An event the engine sent (`finding`, `finding_dropped`, `file`, `progress`)."""
        if event not in _OBSERVED or not isinstance(data, Mapping):
            return
        with self._lock:
            if self._ended:
                return
            if event == "finding":
                finding = data.get("finding")
                if isinstance(finding, Mapping) and finding.get("id"):
                    fid = str(finding["id"])
                    self._latest[fid] = dict(finding)
                    if fid in self._member:
                        self._apply(fid, finding, final=None)
                        self._send_tally()
            elif event == "finding_dropped":
                fid = str(data.get("finding_id") or "")
                self._latest.pop(fid, None)
                if fid in self._member:
                    self._dropped(fid)
                    self._send_tally()
            elif event == "file":
                self._files += 1
                if self._phase["phase"] == "making_files":
                    self.phase(
                        "making_files",
                        done=self._files,
                        total=max(FILES_EXPECTED, self._files),
                        unit="files",
                    )
            elif event == "progress" and self.mode == "roster":
                self._roster_progress(data.get("progress"))

    def decide(self, findings: Sequence[Mapping[str, Any]], *, final: bool = False) -> None:
        """These findings are decided as they are now (a decision point of the run). Recorded
        findings that carry a candidate's key (`simple.key`) take over that candidate's row. With
        `final`, every row is final and a row whose finding is no longer in the list is removed."""
        with self._lock:
            seen: set[str] = set()
            for finding in findings:
                if not finding.get("id"):
                    continue
                fid = str(finding["id"])
                seen.add(fid)
                self._latest[fid] = dict(finding)
                simple = finding.get("simple")
                key = simple.get("key") if isinstance(simple, Mapping) else None
                if isinstance(key, str) and fid not in self._member:
                    if key in self._member:
                        self._member[fid] = self._member.pop(key)
                    self._waiting.discard(key)
                    self._held.discard(key)
                self._apply(fid, finding, final=final or None)
            if final:
                for member in [m for m in self._member if m not in seen]:
                    self._dropped(member)
                self._waiting.clear()
                self._held.clear()
            self._send_tally()

    def verified(
        self,
        item_id: str,
        found: Sequence[Any],
        *,
        scoped: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> None:
        """One drawing's items after code verified them (`simple_check.verify.Verified`): Right and
        Not compared items as decided for now, red candidates waiting for the second look."""
        with self._lock:
            for one in found:
                fields = getattr(one, "finding", None)
                key = str(getattr(one, "key", "") or "")
                outcome = getattr(one, "outcome", None)
                if not key or not isinstance(fields, Mapping) or not fields:
                    continue
                if scoped is not None and not _safe_scoped(scoped, fields):
                    continue  # another product than the check's: never shown
                if outcome == "candidate":
                    self._waiting.add(key)
                elif outcome in ("right", "not_compared"):
                    self._apply(key, _candidate(one), final=None)
            self._verify[0] += 1
            self.phase(
                "double_checking",
                step="verifying",
                done=self._verify[0],
                total=max(self._verify),
                unit="drawings",
            )
            self._send_tally()

    def code_checks(self) -> None:
        """Code's own checks of the whole set (its strings, finishes and pairs, $0) after the
        compare calls: double-checking, with no count."""
        with self._lock:
            self.phase("double_checking", step="code_checks")

    def verifying(self, total: int) -> None:
        """Code starts verifying the answered drawings (n of m)."""
        with self._lock:
            self._verify = [0, total]
            self.phase("double_checking", step="verifying", done=0, total=total, unit="drawings")

    def second_looks(self, total: int) -> None:
        """The blind second looks start (n of m)."""
        with self._lock:
            self._looks = [0, total]
            if total:
                self.phase(
                    "double_checking", step="second_looks", done=0, total=total, unit="looks"
                )

    def second_look(self, done: Any) -> None:
        """One red candidate after its blind look (or after none: a stop, no money): Wrong when the
        look agreed and the proof rule shows it so, else Not compared with the reason."""
        with self._lock:
            key = str(getattr(done, "key", "") or "")
            if key not in self._waiting:
                return
            self._waiting.discard(key)
            self._looks[0] += 1
            outcome = getattr(done, "outcome", None)
            fields = getattr(done, "finding", None)
            if isinstance(fields, Mapping) and fields:
                view = _candidate(done)
                if outcome == "red" and self._view(view)["outcome"] != "wrong":
                    self._held.add(key)  # proved by the look; shown once recorded
                elif outcome in ("red", "not_compared"):
                    self._apply(key, view, final=None)
            if self._looks[1]:
                self.phase(
                    "double_checking",
                    step="second_looks",
                    done=min(self._looks[0], self._looks[1]),
                    total=self._looks[1],
                    unit="looks",
                )
            self._send_tally()

    def looked(self, after: Sequence[Any]) -> None:
        """Every candidate's state after the blind looks: the ones no look was made for (a stop, no
        money, another product) settled too."""
        with self._lock:
            for one in after:
                key = str(getattr(one, "key", "") or "")
                if key in self._waiting and getattr(one, "outcome", None) != "candidate":
                    self.second_look(one)
            self.phase("double_checking", step="final_checks")

    # ---- reading it -----------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The whole live state now (`ChatLiveStateOut`)."""
        with self._lock:
            return {
                "v": LIVE_VERSION,
                "mode": self.mode,
                "started_at": _iso(self.started_at),
                "phase": self._phase_out(),
                "drawings": [dict(d) for d in self._drawings.values()],
                "findings": [dict(row) for row in self._rows.values() if not row["removed"]],
                "tally": self._tally(),
                "counts": self.counts(),
                "stopping": None if self._stopping is None else self._stopping_out(),
                "service": self._service,
            }

    def counts(self) -> dict[str, int | None]:
        """The numbers the chat list and the folded work line need."""
        with self._lock:
            done: int | None = None
            total: int | None = None
            if self._drawings:
                compared = [d for d in self._drawings.values() if d["status"] != "skipped"]
                done = sum(1 for d in compared if d["status"] in ("answered", "failed"))
                total = len(compared)
            return {
                "drawings_done": done,
                "drawings_total": total,
                "second_looks_done": self._looks[0],
                "second_looks_total": self._looks[1],
                "files_made": self._files,
            }

    # ---- inside ---------------------------------------------------------------------------------

    def _send(self, event: str, name: str, value: Any) -> None:
        try:
            self._publish(event, {"v": LIVE_VERSION, name: value})
        except Exception:  # noqa: BLE001 - a gone screen never stops the check
            _log.debug("live check event not sent")

    def _phase_out(self) -> dict[str, Any]:
        return {
            **self._phase,
            "started_at": _iso(self.started_at),
            "stopping": self._stopping is not None,
        }

    def _send_phase(self) -> None:
        if self._started:
            self._send("check_phase", "phase", self._phase_out())

    def _comparing(self) -> None:
        counts = self.counts()
        done, total = counts["drawings_done"] or 0, counts["drawings_total"] or 0
        unchanged = self._phase["phase"] == "comparing" and (
            self._phase["done"],
            self._phase["total"],
        ) == (done, total)
        if not unchanged:
            self.phase("comparing", done=done, total=total, unit="drawings")

    def _stopping_out(self) -> dict[str, int]:
        compared = [d for d in self._drawings.values() if d["status"] != "skipped"]
        return {
            "in_flight": sum(1 for d in compared if d["status"] == "comparing"),
            "not_started": sum(1 for d in compared if d["status"] == "waiting"),
        }

    def _send_stopping(self) -> None:
        """Sent when Stop is pressed and each time the drawings still finishing change (the ones
        never started are counted as they are let go, without an event each)."""
        out = self._stopping_out()
        before = self._stopping
        self._stopping = out
        if not before or out["in_flight"] != before.get("in_flight"):
            self._send("check_stopping", "stopping", dict(out))

    def _check_service(self) -> None:
        if self._service is not None or self._service_of is None:
            return
        try:
            reason = self._service_of()
        except Exception:  # noqa: BLE001 - the drawings still say their own reasons
            reason = None
        if isinstance(reason, str) and reason:
            self._service = reason
            self._send("check_service", "reason", reason)

    def _roster_progress(self, progress: object) -> None:
        if not isinstance(progress, Mapping):
            return
        name = _ROSTER_PHASE.get(str(progress.get("phase")))
        if name is None:
            return
        if name == "making_files":
            if self._phase["phase"] != "making_files":
                self.making_files(list(self._latest.values()))
            return
        if name == "comparing":
            done, total = progress.get("pages_done"), progress.get("pages_total")
            if (self._phase["phase"], self._phase["done"], self._phase["total"]) != (
                name,
                done,
                total,
            ):
                self.phase(name, done=done, total=total, unit="pages")
            return
        if self._phase["phase"] != name:
            self.phase(name, step="final_checks")

    # -- the rows --

    def _view(self, finding: Mapping[str, Any]) -> dict[str, Any]:
        """A finding as the live screen shows it (`ChatLiveFindingOut`'s fields, no key)."""
        try:
            proof = proof_of(finding)
        except Exception:  # noqa: BLE001 - an item code cannot place is not counted as Wrong
            _log.exception("live check: no proof for %s", finding.get("id"))
            proof = {"group": "not_compared"}
        outcome = _OUTCOME.get(str(proof.get("group")), "not_compared")
        pairing = finding.get("pairing") if isinstance(finding.get("pairing"), Mapping) else {}
        proved_by = None
        if outcome == "wrong":
            looked = isinstance((pairing or {}).get("advocate"), Mapping)
            proved_by = "checked_twice" if looked else "code"
        try:
            impact = impact_of(finding) if outcome != "right" else None
        except Exception:  # noqa: BLE001 - shown without its score
            impact = None
        page = finding.get("page")
        page = page if isinstance(page, int) else None
        return {
            "outcome": outcome,
            "proved_by": proved_by,
            "page": page,
            "area": self._areas.get(page) if page is not None else None,
            "item": str(finding.get("item") or ""),
            "vendor": _text(finding.get("vendor")),
            "architect": _text(finding.get("architect")),
            "should_be": _text(proof.get("should_be")),
            "product": str(finding.get("category") or "other"),
            "reason": (
                _text(proof.get("reason")) if outcome in ("not_compared", "rule_note") else None
            ),
            "needs": (
                _text(proof.get("settle"))
                if outcome == "not_compared"
                else (
                    _text((proof.get("rule_note") or {}).get("question"))  # GV asks
                    if outcome == "rule_note"
                    else None
                )
            ),
            "impact": (
                None
                if impact is None
                else {"score": int(impact["score"]), "why": str(impact["why"])}
            ),
        }

    def _apply(self, member: str, finding: Mapping[str, Any], *, final: bool | None) -> None:
        """A member's (a finding's, or a candidate's until recorded) newest state onto its row."""
        view = self._view(finding)
        fid = None if _is_candidate(member) else str(finding.get("id") or member)
        same = _same_item_key(view, finding)
        found = self._member.get(member)
        row = None if found is None else self._rows.get(found)
        if found is None or row is None:
            key = member
            row = {
                "key": key,
                "id": fid,
                **view,
                "same_as": None,
                "removed": False,
                "previous": None,
                "final": bool(final),
            }
            self._rows[key] = row
            self._member[member] = key
            self._facts[key] = {"same": same}
            if view["outcome"] == "wrong":
                row["same_as"] = self._same_wrong(key)
            self._send("check_finding", "finding", dict(row))
            return
        key = found
        if row["outcome"] == "wrong" and row["same_as"] is None and view["outcome"] != "wrong":
            # A shown Wrong taken back: a same-item Wrong still standing keeps the row Wrong.
            if self._hand_over(member, view, finding, final=final):
                return
            self.withdrawn += 1
            _log.warning(
                "live check: a Wrong shown live was taken back (%s -> %s)", key, view["outcome"]
            )
        before = dict(row)
        previous = row["outcome"]
        row.update(view)
        row["id"] = fid or row["id"]
        row["removed"] = False
        if previous != view["outcome"]:
            row["previous"] = previous
        if final is not None:
            row["final"] = bool(final)
        self._facts[key] = {"same": same}
        if view["outcome"] != "wrong":
            row["same_as"] = None
        elif previous != "wrong":
            row["same_as"] = self._same_wrong(key)
        if row != before:
            self._send("check_finding", "finding", dict(row))

    def _same_wrong(self, key: str) -> str | None:
        """The live key of a shown Wrong that is the same item, or None."""
        same = self._facts.get(key, {}).get("same")
        for other, row in self._rows.items():
            if (
                other != key
                and not row["removed"]
                and row["outcome"] == "wrong"
                and row["same_as"] is None
                and self._facts.get(other, {}).get("same") == same
            ):
                return other
        return None

    def _hand_over(
        self,
        member: str,
        view: Mapping[str, Any] | None,
        finding: Mapping[str, Any] | None,
        *,
        final: bool | None = None,
    ) -> bool:
        """A shown Wrong's own finding left the answer (`view` None) or is no longer Wrong, while a
        same-item Wrong (its hidden twin) still stands: the shown row stays Wrong and now stands for
        the twin's finding; the twin's row takes this member's new state (or is removed). The
        screen sees no red disappear. False when there is no twin."""
        key = self._member[member]
        twin = next(
            (
                other
                for other, row in self._rows.items()
                if row["same_as"] == key and not row["removed"] and row["outcome"] == "wrong"
            ),
            None,
        )
        if twin is None:
            return False
        shown, hidden = self._rows[key], self._rows[twin]
        moved = [m for m, owner in self._member.items() if owner == twin]
        for one in moved:
            self._member[one] = key
        self._member[member] = twin
        keep = ("id", "page", "area", "item", "vendor", "architect", "should_be", "product")
        shown.update({name: hidden[name] for name in (*keep, "proved_by", "impact", "final")})
        self._facts[key], self._facts[twin] = self._facts.get(twin, {}), self._facts.get(key, {})
        if view is None or finding is None:
            self._member.pop(member, None)
            hidden["removed"] = True
        else:
            hidden.update(view)
            hidden["id"] = None if _is_candidate(member) else str(finding.get("id") or member)
            hidden["previous"] = "wrong"
            hidden["same_as"] = None
            if final is not None:
                hidden["final"] = bool(final)
            self._facts[twin] = {"same": _same_item_key(view, finding)}
        self._send("check_finding", "finding", dict(shown))
        self._send("check_finding", "finding", dict(hidden))
        return True

    def _dropped(self, member: str) -> None:
        """A member's finding left the answer (folded into another, or not in the final list)."""
        key = self._member.get(member)
        row = None if key is None else self._rows.get(key)
        if row is None or row["removed"]:
            self._member.pop(member, None)
            return
        if row["outcome"] == "wrong" and row["same_as"] is None:
            if self._hand_over(member, None, None):
                return
            self.withdrawn += 1
            _log.warning("live check: a Wrong shown live left the answer (%s)", key)
        self._member.pop(member, None)
        row["removed"] = True
        self._send("check_finding", "finding", dict(row))

    def _tally(self) -> dict[str, int]:
        shown = [row for row in self._rows.values() if not row["removed"]]
        return {
            "wrong": sum(1 for r in shown if r["outcome"] == "wrong" and r["same_as"] is None),
            "right": sum(1 for r in shown if r["outcome"] == "right"),
            "rule_note": sum(1 for r in shown if r["outcome"] == "rule_note"),
            "not_compared": sum(1 for r in shown if r["outcome"] == "not_compared"),
            # A look that agreed but is not shown as Wrong yet still waits (for its record).
            "waiting_second_look": len(self._waiting) + len(self._held),
        }

    def _send_tally(self) -> None:
        tally = self._tally()
        if tally != self._last_tally:
            self._last_tally = tally
            self._send("check_tally", "tally", dict(tally))


def _text(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _is_candidate(member: str) -> bool:
    """A candidate's key (`p23.v1:p23.D17`), not a finding id (`F12`)."""
    return ":" in member


def _candidate(one: Any) -> dict[str, Any]:
    """A verified item as it will be recorded (`run._Run._add`'s fields: its critic and pairing,
    code's readings in its checks), for the live view before it is."""
    from app.review.assistant.chat_trust import judged

    fields = dict(getattr(one, "finding", None) or {})
    checks = dict(fields.get("checks") or {})
    critic = fields.get("critic")
    if isinstance(critic, Mapping) and critic.get("result"):
        checks["critic"] = critic.get("result")
    for key, read in (getattr(one, "reads", None) or {}).items():
        existing = checks.get(key)
        matched = isinstance(existing, Mapping) and existing.get("result") == "match"
        if isinstance(read, Mapping) and not matched:
            checks[key] = dict(read)
    fields["checks"] = checks
    fields["simple"] = {"key": getattr(one, "key", None)}
    try:
        return judged(fields)
    except Exception:  # noqa: BLE001 - its own fields, not judged: never shown as Wrong
        fields["checks"] = {}
        return fields


def _safe_scoped(scoped: Callable[[Mapping[str, Any]], bool], fields: Mapping[str, Any]) -> bool:
    try:
        return bool(scoped(fields))
    except Exception:  # noqa: BLE001 - unsure: not shown (the record decides)
        return False
