"""The simple full check's funnel (#1767): how far each vendor dimension got, step by step.

A run keeps its answers (`simple/<run>/compare`, `blind`, `resolve`) and its settings (`run.json`),
but the counts that explain a miss used to live only in memory. `funnel.json` is written next to
them at the end of every run and every replay. Read top to bottom, it is the path every item takes:

1. **labels read**: the vendor and architect labels code numbered on each drawing (by reading route);
2. **the AI's answer**: the pairs it made (same, different, different points, unsure, no architect
   value), its other items, the labels it named unaccounted and those it never named;
3. **code verifies**: each item's outcome, the Not-compared reason codes, the first code check that
   failed, and every check's pass / fail count;
4. **resolve**: items the loop looked at again, and how each ended;
5. **the blind second look**: asked, agreed, disagreed, failed or not made (and why);
6. **the end**: red, Right, Not compared, covered by code's own finding, or left out by a GV rule.

Each step is counted per vendor drawing (`drawings`) and in total (`totals`), with the label ids
behind the counts. `trail` lists every item the AI or code called different, missing or unsure, by
ids only. Pure code, no model call, no drawing values beyond label ids; the run never stops for it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from app.review.assistant.simple_check.schema import CompareOutcome, WorkItem
from app.review.assistant.simple_check.verify import Verification, Verified

__all__ = ["FILE", "SCHEMA", "build", "headline"]

#: The funnel's format name (bumped when a field changes meaning).
SCHEMA: Final = "gv-simple-funnel/1"
#: Its file in the run folder (`<chat>/simple/<run>/funnel.json`).
FILE: Final = "funnel.json"
#: The AI verdicts that put an item on the trail (it, or code, saw a possible difference).
_TRAIL_VERDICTS: Final = frozenset({"different", "different_points", "unsure"})
_NOT_MADE: Final = "blind second look was not made ("
_SKIPPED: Final = ("the check was stopped", "per-answer cap", "runaway guard", "product scope")


def _ref(label: Mapping[str, Any] | None) -> str | None:
    if not isinstance(label, Mapping) or not label.get("id"):
        return None
    return f"p{label.get('page')}.{label.get('id')}"


def _work_item(key: str) -> str:
    return key.split(":", 1)[0]


def _blind(one: Verified) -> tuple[str, str | None] | None:
    """How a red candidate's blind second look went: `agreed`, `disagreed`, `failed` (asked, not
    made) or `skipped` (never asked: a stop, the money, the product scope; or no saved look on a
    replay), with why; None when it was not a candidate."""
    look = next((c for c in one.checks if c.name == "blind"), None)
    if look is None:
        return None
    if look.outcome == "pass" and one.outcome == "red":
        return "agreed", None
    why = look.why or one.reason
    if _NOT_MADE in why:
        inner = why.split(_NOT_MADE, 1)[1].rsplit(")", 1)[0]
        if any(word in inner for word in _SKIPPED) or "no saved blind look" in inner:
            return "skipped", inner
        return "failed", inner
    return "disagreed", None


def _resolve(one: Verified) -> tuple[str, str | None] | None:
    """How the resolve loop ended on an item (None: it did not enter): `right`, `candidate`,
    `not_finished` (a stop or the money) or `not_settled` with the last round's status."""
    if not one.resolve:
        return None
    statuses = [str(r.get("status") or "") for r in one.resolve]
    if "not_asked" in statuses:
        return "not_finished", None
    last = statuses[-1]
    if last in ("right", "candidate"):
        return last, None
    return "not_settled", last or "unknown"


def _first_failed(one: Verified) -> str | None:
    return next((c.name for c in one.checks if c.outcome == "fail" and c.name != "blind"), None)


def _final(one: Verified) -> str:
    return {"listed": "not_compared", "candidate": "not_compared"}.get(one.outcome, one.outcome)


def _raw(outcome: CompareOutcome | None) -> dict[str, Mapping[str, Any]]:
    """The answer's own entries by the key verify gives them (`run._raw_entries`'s rule)."""
    if outcome is None or outcome.result is None:
        return {}
    data = outcome.result.model_dump(mode="json")
    work = outcome.work_item
    out: dict[str, Mapping[str, Any]] = {
        f"{work}:{pair.get('vendor_label')}": pair for pair in data.get("pairs") or []
    }
    for at, entry in enumerate(data.get("other_items") or [], start=1):
        out[f"{work}:item{at}"] = entry
    return out


def _labels(item: WorkItem) -> dict[str, Any]:
    vendor = list(item.vendor.labels)
    architect = [label for drawing in item.architect for label in drawing.labels]
    return {
        "vendor": len(vendor),
        "architect": len(architect),
        "vendor_by_route": dict(Counter(str(label.route or "other") for label in vendor)),
        "architect_by_route": dict(Counter(str(label.route or "other") for label in architect)),
        "vendor_ids": [label.ref for label in vendor],
    }


def _answer(outcome: CompareOutcome | None, check: Verification | None) -> dict[str, Any]:
    if outcome is None or outcome.result is None:
        return {}
    result = outcome.result
    verdicts = Counter(pair.verdict for pair in result.pairs)
    kinds = Counter(entry.kind for entry in result.other_items)
    never = list(check.missing_labels) if check is not None else []
    unknown = list(check.unknown_labels) if check is not None else []
    return {
        "pairs": len(result.pairs),
        "paired_with_architect": sum(1 for p in result.pairs if p.architect_label),
        "verdicts": {
            name: verdicts.get(name, 0)
            for name in ("same", "different", "different_points", "unsure", "no_architect_value")
        },
        "other_items": len(result.other_items),
        "other_kinds": dict(kinds),
        "unaccounted": len(result.unaccounted_labels),
        "never_named": len(never),
        "unknown_labels": len(unknown),
        "unaccounted_ids": list(result.unaccounted_labels),
        "never_named_ids": never,
    }


def _verify(first: Sequence[Verified]) -> dict[str, Any]:
    outcomes = Counter(v.outcome for v in first)
    checks: dict[str, Counter[str]] = {}
    for v in first:
        for c in v.checks:
            checks.setdefault(c.name, Counter())[str(c.outcome)] += 1
    return {
        "items": len(first),
        "outcomes": {
            name: outcomes.get(name, 0)
            for name in ("candidate", "right", "not_compared", "listed", "covered")
        },
        "reasons": dict(
            Counter(
                str(v.reason_code or "none")
                for v in first
                if v.outcome not in ("candidate", "right", "covered")
            )
        ),
        "first_failed_check": dict(Counter(f for v in first if (f := _first_failed(v)))),
        "checks": {name: dict(counts) for name, counts in sorted(checks.items())},
        "candidate_ids": [v.key for v in first if v.outcome == "candidate"],
    }


def _resolve_block(last: Sequence[Verified]) -> dict[str, Any]:
    ends = [(v, done) for v in last if (done := _resolve(v)) is not None]
    tally = Counter(end for _v, (end, _why) in ends)
    return {
        "entered": len(ends),
        "right": tally.get("right", 0),
        "candidate": tally.get("candidate", 0),
        "not_settled": tally.get("not_settled", 0),
        "not_finished": tally.get("not_finished", 0),
        "not_settled_by_status": dict(
            Counter(why for _v, (end, why) in ends if end == "not_settled" and why)
        ),
        "entered_ids": [v.key for v, _ in ends],
    }


def _blind_block(last: Sequence[Verified]) -> dict[str, Any]:
    looks = [(v, done) for v in last if (done := _blind(v)) is not None]
    tally = Counter(end for _v, (end, _why) in looks)
    return {
        "candidates": len(looks),
        "asked": sum(1 for _v, (end, _w) in looks if end != "skipped"),
        "agreed": tally.get("agreed", 0),
        "disagreed": tally.get("disagreed", 0),
        "failed": tally.get("failed", 0),
        "skipped": tally.get("skipped", 0),
        "skipped_why": dict(Counter(why for _v, (end, why) in looks if end == "skipped" and why)),
        "agreed_ids": [v.key for v, (end, _w) in looks if end == "agreed"],
        "disagreed_ids": [v.key for v, (end, _w) in looks if end == "disagreed"],
    }


def _final_block(first: Sequence[Verified], last: Sequence[Verified]) -> dict[str, Any]:
    kept = {v.key for v in last}
    ends = Counter(_final(v) for v in last)
    return {
        "red": ends.get("red", 0),
        "right": ends.get("right", 0),
        "not_compared": ends.get("not_compared", 0),
        "covered": ends.get("covered", 0),
        "left_out": sum(1 for v in first if v.key not in kept),
        "red_ids": [v.key for v in last if v.outcome == "red"],
    }


def _trail(
    first: Sequence[Verified], last: Mapping[str, Verified], raws: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for v in first:
        raw = raws.get(v.key) or {}
        said = str(raw.get("verdict") or "") or None
        end = last.get(v.key)
        on_trail = (
            v.kind != "pair"
            or said in _TRAIL_VERDICTS
            or v.outcome == "candidate"
            or (end is not None and end.outcome == "red")
        )
        if not on_trail:
            continue
        resolved = _resolve(end) if end is not None else None
        looked = _blind(end) if end is not None else None
        rows.append(
            {
                "key": v.key,
                "work_item": _work_item(v.key),
                "page": (v.vendor or {}).get("page") or (v.about or {}).get("page"),
                "kind": v.kind,
                "vendor_label": _ref(v.vendor),
                "architect_label": _ref(v.architect),
                "ai": said,
                "verify": v.outcome,
                "failed_check": _first_failed(v),
                "reason_code": v.reason_code,
                "resolve": resolved[0] if resolved else "not_entered",
                "blind": looked[0] if looked else None,
                "final": _final(end) if end is not None else "left_out",
            }
        )
    return rows


def _add(total: dict[str, Any], part: Mapping[str, Any]) -> None:
    """Sums one drawing's counts into the totals (ids are joined; text is left out)."""
    for name, value in part.items():
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            total[name] = total.get(name, 0) + value
        elif isinstance(value, Mapping):
            _add(total.setdefault(name, {}), value)
        elif isinstance(value, list):
            total.setdefault(name, []).extend(value)


def build(
    items: Sequence[WorkItem],
    *,
    compared: Iterable[str],
    skipped: Mapping[str, str],
    outcomes: Mapping[str, CompareOutcome],
    verifications: Mapping[str, Verification],
    final: Iterable[Verified],
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The funnel of one run (see the module). `verifications`: code's verify of each answered
    drawing, before the resolve loop; `final`: every item after the resolve loop and the blind
    look (what the run recorded, before the finding book's own limits); `meta`: the run's ids,
    settings, code version, cost and time, copied as they are."""
    wanted = set(compared)
    last: dict[str, Verified] = {}
    for one in final:
        last.setdefault(one.key, one)
    drawings: dict[str, dict[str, Any]] = {}
    trail: list[dict[str, Any]] = []
    for item in items:
        outcome = outcomes.get(item.id)
        why: str | None
        if item.id in skipped:
            status, why = "skipped", skipped[item.id]
        elif item.id not in wanted:
            status, why = "skipped", "not in this run's work list"
        elif outcome is None:
            status, why = "failed", "not compared (the run ended first)"
        elif outcome.status != "ok":
            status, why = "failed", outcome.reason
        else:
            status, why = "answered", None
        check = verifications.get(item.id)
        first = list(check.items) if check is not None else []
        mine = [one for key, one in last.items() if _work_item(key) == item.id]
        row: dict[str, Any] = {
            "page": item.page,
            "round": item.round,
            "status": status,
            "why": why,
            "model": (outcome.model or outcome.asked_model) if outcome is not None else None,
            "cost_usd": f"{outcome.cost_usd:.4f}" if outcome is not None else "0.0000",
            "labels": _labels(item),
            "ai": _answer(outcome, check),
            "verify": _verify(first),
            "resolve": _resolve_block(mine),
            "blind": _blind_block(mine),
            "final": _final_block(first, mine),
        }
        drawings[item.id] = row
        if check is not None:
            trail += _trail(first, last, _raw(outcome))
    statuses = Counter(row["status"] for row in drawings.values())
    totals: dict[str, Any] = {
        "work_items": {
            "total": len(drawings),
            "answered": statuses.get("answered", 0),
            "failed": statuses.get("failed", 0),
            "skipped": statuses.get("skipped", 0),
            "by_model": dict(
                Counter(
                    str(row["model"]) for row in drawings.values() if row["status"] == "answered"
                )
            ),
        }
    }
    for row in drawings.values():
        _add(
            totals,
            {k: row[k] for k in ("labels", "ai", "verify", "resolve", "blind", "final")},
        )
    return {
        "schema": SCHEMA,
        **dict(meta or {}),
        "totals": totals,
        "drawings": drawings,
        "trail": trail,
    }


def headline(funnel: Mapping[str, Any]) -> dict[str, int]:
    """The funnel's totals in one line (the `work_items` block's `funnel`, shown under "How this
    was checked"): labels read, paired, left unaccounted, verified different, the resolve loop,
    the blind second look, and the end."""
    totals = funnel.get("totals") or {}

    def count(*path: str) -> int:
        at: Any = totals
        for name in path:
            at = at.get(name) if isinstance(at, Mapping) else None
        return int(at) if isinstance(at, int) else 0

    return {
        "drawings": count("work_items", "answered"),
        "labels_read": count("labels", "vendor"),
        "paired": count("ai", "paired_with_architect"),
        "unaccounted": count("ai", "unaccounted") + count("ai", "never_named"),
        "verified_different": count("verify", "outcomes", "candidate"),
        "resolved": count("resolve", "entered"),
        "second_looks": count("blind", "asked"),
        "agreed": count("blind", "agreed"),
        "red": count("final", "red"),
        "right": count("final", "right"),
        "not_compared": count("final", "not_compared"),
    }
