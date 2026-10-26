"""GV's settled formulas as code checks, and the job settings they read (final plan #9). Code, $0.

Some sizes on GV's countertop drawings are not copied from the reference drawing: GV's settled
formulas set them from the reference's other sizes and from a few values entered once per job.
Each formula below is its own **kind of check** (`check_record`), so its track record is kept
apart from "a size differs from the architect's":

| Formula | Kind | Code (where it runs) |
|---|---|---|
| countertop depth = cabinet depth + front overhang | `formula_depth` | `sink_offset` (CT-DEPTH-001 on the vendor's plan) |
| countertop length = the cabinet run + a field cut at each wall end + the end overhang at each finished end | `formula_length` | `top_length` here, from the escalation's like-for-like step |
| back splash length = the top's run from wall to wall | `formula_splash` | `escalation._splash_wall_run` (decided 2026-10-25) |
| sink centre = the sink base's centre | `sink_centre` | `sink_centre` |
| top width = the sum of the reference's chain of sizes | `formula_chain` | `escalation._chain_step` |

**Each runs only when its inputs are known and code proved the ends' kinds** (`dim_ends`): what
each end of both dimensions lands on (a wall, GV's field cut, the overhang past the cabinets, a
cabinet's side, a panel), never on the AI's word. A formula that cannot tell says nothing: the item
keeps its outcome and its reason.

**What a formula says.** The same value is **Right**, with the formula and its numbers shown
("GV's formula: countertop length = ..."). A different value is a **Likely problem** ("please check
and confirm"), never red: a new kind has no keyed cases yet (`check_record`), and the bar is never
lowered for it. A formula whose job value nobody entered is **Not compared** with the formula and
the value to enter (`job_value_rules`, "a job value or GV setting is missing").

**The job settings** (`JOB_SETTINGS`), asked once per job: each is read from the chat's `rules.md`
(the rulebook's default, GV's company standard, the project's value or its sink card, a value typed
for the drawing set: the highest present). A setting with a settled company default uses it; one
with none stays "not entered" and every check that needs it says so plainly. Nothing is guessed.

Pure code: plain values in, plain values out. Verification: `tests/review/assistant/test_formulas.py`
(made-up drawings and values).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final, Literal

from app.review.assistant import dim_ends
from app.review.assistant.ledger import format_inches

__all__ = [
    "CHAIN",
    "DEPTH",
    "END_OVERHANG",
    "FIELD",
    "FORMULAS",
    "JOB_SETTINGS",
    "KINDS",
    "LENGTH",
    "SINK_CENTRE",
    "SPLASH",
    "Formula",
    "JobSetting",
    "Length",
    "formula",
    "kind_of",
    "measures_top_against_run",
    "missing_record",
    "please_check",
    "record",
    "right_how",
    "setting",
    "settings_state",
    "top_length",
]

#: The finding's record of the formula that compared it (`record`).
FIELD: Final = "formula"


@dataclass(frozen=True, slots=True)
class Formula:
    """One of GV's settled formulas and the kind of check it is."""

    id: str
    kind: str
    """Its kind of check in `check_record`."""
    name: str
    words: str
    """The formula in plain words."""


DEPTH: Final = Formula(
    "depth",
    "formula_depth",
    "countertop depth",
    "countertop depth = cabinet depth + front overhang",
)
LENGTH: Final = Formula(
    "length",
    "formula_length",
    "countertop length",
    "countertop length = the cabinet run + a field cut at each wall end + the end overhang at "
    "each finished end",
)
SPLASH: Final = Formula(
    "splash",
    "formula_splash",
    "back splash length",
    "back splash length = the top's run from wall to wall",
)
SINK_CENTRE: Final = Formula(
    "sink_centre",
    "sink_centre",
    "sink centre",
    "sink centre = the sink base's centre",
)
CHAIN: Final = Formula(
    "chain",
    "formula_chain",
    "top width",
    "top width = the sum of the reference's chain of sizes",
)
FORMULAS: Final[tuple[Formula, ...]] = (DEPTH, LENGTH, SPLASH, SINK_CENTRE, CHAIN)
#: Each formula's kind of check (`check_record.KINDS` lists them).
KINDS: Final[Mapping[str, str]] = {formula.kind: formula.words for formula in FORMULAS}
_BY_ID: Final[Mapping[str, Formula]] = {formula.id: formula for formula in FORMULAS}

#: The end overhang over a finished end: no GV setting holds it yet (not settled with GV), so it is
#: never in `rules.md` and every formula that needs it says it is not entered.
END_OVERHANG: Final = "end_overhang"


@dataclass(frozen=True, slots=True)
class JobSetting:
    """One value GV's formulas read, entered once per job."""

    keys: tuple[str, ...]
    """Its `rules.md` keys (the rulebook's parameter names)."""
    words: str
    default: str | None
    """GV's settled company default, as a drawing writes it; `None`: none is settled."""
    settled: str
    """What is settled about it, in plain words."""
    used_by: tuple[str, ...]
    """The formulas and checks that read it."""


#: The job settings GV's formulas read, in the order the pre-flight names them. Only settled
#: defaults are given; the rest stay "not entered" until someone enters them for the job.
JOB_SETTINGS: Final[tuple[JobSetting, ...]] = (
    JobSetting(
        ("countertop_overhang",),
        "front overhang",
        None,
        "set per job (specified by the G.C. or the client); no company value",
        (DEPTH.name,),
    ),
    JobSetting(
        ("cabinet_depth",),
        "cabinet depth (carcass + door)",
        None,
        'set per job: the carcass depth plus the door (3/4" typical); no company value',
        (DEPTH.name,),
    ),
    JobSetting(
        (END_OVERHANG,),
        "end overhang over a finished end",
        None,
        "not settled with GV: no GV setting holds it yet",
        (LENGTH.name,),
    ),
    JobSetting(
        ("field_cut",),
        "field cut per wall end",
        '1"',
        "GV's company standard 1\" per wall end; a project value replaces it",
        (LENGTH.name,),
    ),
    JobSetting(
        ("sink_interior_width", "sink_interior_depth"),
        "sink inside size (or the sink model's cut sheet)",
        None,
        "set per job from the sink's cut sheet (the project's sink card); no company value",
        ("sink cut-out width and depth",),
    ),
    JobSetting(
        ("filler_min", "filler_max"),
        "filler width range",
        '1" to 2"',
        'GV\'s 1" to 2" (a GV note only, never red); a project value replaces it',
        ("filler note",),
    ),
)


def setting(rules_md: str | None, key: str, name: str | None = None) -> Fraction | None:
    """The value in force for `key` in the chat's `rules.md`, in inches; `None` when it is not
    listed or not entered (never a guess)."""
    if key == END_OVERHANG:
        return None
    from app.review.assistant.sink_offset import setting_from

    found = setting_from(rules_md, key, name or key)
    return None if found is None else found.value


def settings_state(rules_md: str | None) -> list[dict[str, Any]]:
    """Each job setting (`JOB_SETTINGS`) as this job has it: `entered` (every key has a value in
    force, default or entered), the values, and what is settled about it."""
    out: list[dict[str, Any]] = []
    for item in JOB_SETTINGS:
        values = {key: setting(rules_md, key) for key in item.keys}
        out.append(
            {
                "setting": item.words,
                "keys": list(item.keys),
                "entered": all(value is not None for value in values.values()),
                "values": {
                    key: None if value is None else format_inches(value)
                    for key, value in values.items()
                },
                "default": item.default,
                "settled": item.settled,
                "used_by": list(item.used_by),
            }
        )
    return out


def record(
    formula: Formula,
    outcome: Literal["same", "different", "missing"],
    why: str,
    *,
    expected: Fraction | None = None,
    labels: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The finding's record (`FIELD`) of the formula that compared it."""
    out: dict[str, Any] = {
        "id": formula.id,
        "kind": formula.kind,
        "formula": formula.words,
        "outcome": outcome,
        "why": why,
    }
    if expected is not None:
        out["expected"] = format_inches(expected)
    if labels:
        out["labels"] = dict(labels)
    return out


def kind_of(finding: Mapping[str, Any]) -> str | None:
    """The formula's kind of check for a finding a formula compared, else `None`."""
    held = finding.get(FIELD)
    if not isinstance(held, Mapping):
        return None
    kind = held.get("kind")
    return str(kind) if kind in KINDS else None


def right_how(formula: Formula, how: str) -> str:
    """A Right's "settled by" words with the formula shown."""
    return f"GV's formula ({formula.words}): {how}"


def please_check(formula: Formula, how: str) -> str:
    """A formula's difference as a Likely problem's line."""
    from app.review.assistant.likely_problems import LETTER_LEAD

    return (
        f"{LETTER_LEAD}: by GV's formula ({formula.words}), {how}. This kind of check is still "
        "being proven, so it is never red yet."
    )


def missing_record(formula: Formula, missing: Sequence[str]) -> dict[str, Any]:
    """The `job_value_rules.FIELD` record of a formula whose job value nobody entered: the item is
    Not compared, "a job value or GV setting is missing", with the formula and what to enter."""
    return {
        "rule": formula.name,
        "name": formula.name,
        "formula": formula.words,
        "missing": list(missing),
    }


# ---- countertop length = the cabinet run + the allowances at its ends --------------------------

#: Kinds that make the reference's end a cabinet's (or a panel's) side: the cabinet run's end.
_CASEWORK: Final = frozenset({"cabinet_face", "end_panel"})
#: The largest end overhang or field cut code adds at one end (inches): a bigger gap is not one.
_MAX_ALLOWANCE_IN: Final = Fraction(3)

type Outcome = Literal["same", "different", "missing"]


@dataclass(frozen=True, slots=True)
class Allowance:
    """What GV's formula adds at one end of the cabinet run."""

    kind: Literal["field_cut", "end_overhang", "none"]
    words: str


@dataclass(frozen=True, slots=True)
class Length:
    """The countertop length formula on one vendor top against one reference cabinet run."""

    outcome: Outcome
    expected: Fraction | None
    words: str
    """How the expected length is made up, or what is missing."""
    missing: tuple[str, ...] = ()


def _allowance(vendor: dim_ends.End, ref: dim_ends.End) -> Allowance | None:
    """The allowance at one end, from what code proved both ends land on; `None` when code cannot
    tell (an unknown end, a top's outline that may be at a wall or free, two kinds that do not
    meet this way)."""
    v, r = vendor.kinds, ref.kinds
    if "wall" in v and "wall" in r and not v & {"field_cut_end", "overhang_end"}:
        return Allowance("none", "both at the wall")
    if "field_cut_end" in v and "wall" in r:
        return Allowance("field_cut", "field cut at the wall end")
    if "overhang_end" in v and "wall" not in v and r & _CASEWORK and "wall" not in r:
        return Allowance("end_overhang", "end overhang at the finished end")
    return None


def _ways(vendor: dim_ends.Ends, ref: dim_ends.Ends) -> list[tuple[Allowance, Allowance]]:
    """The allowances at both ends, each way round the two drawings may be read (a reversed unit
    is drawn mirrored), where code can tell both."""
    out: list[tuple[Allowance, Allowance]] = []
    for first, second in ((ref.low, ref.high), (ref.high, ref.low)):
        one = _allowance(vendor.low, first)
        two = _allowance(vendor.high, second)
        if one is not None and two is not None:
            out.append((one, two))
    return out


_MISSING_WORDS: Final[Mapping[str, str]] = {
    "field_cut": "field cut per wall end",
    "end_overhang": "end overhang over a finished end",
}


#: End kinds that make a vendor dimension the top's own (its field cut, its edge past the cabinets).
_TOP: Final = frozenset({"field_cut_end", "overhang_end", "top_edge", "top_outline"})


def measures_top_against_run(vendor: dim_ends.Ends, ref: dim_ends.Ends) -> bool:
    """Whether the vendor's dimension is a top's (an end on GV's field cut or the top's edge) and
    the reference's a cabinet run's (an end on a cabinet's side or a panel), and code did not
    prove them alike: then GV's length formula relates them (`top_length`)."""
    if not (vendor.low.kinds | vendor.high.kinds) & _TOP:
        return False
    if not (ref.low.kinds | ref.high.kinds) & _CASEWORK:
        return False
    return dim_ends.alike(vendor, ref)[0] != "alike"


def top_length(
    vendor: dim_ends.Ends,
    ref: dim_ends.Ends,
    *,
    field_cut: Fraction | None,
    end_overhang: Fraction | None = None,
) -> Length | None:
    """GV's countertop length formula on a vendor top's length against the reference's cabinet run
    (the escalation's like-for-like step found one measures the top and the other the cabinets,
    `measures_top_against_run`): the run plus, at each end, the field cut where the vendor's
    top has GV's field cut and the reference's run ends at a wall, or the end overhang where the
    vendor's top runs past the cabinets (code-proved overhang) and the reference's run ends at a
    cabinet's side or a panel with no wall. `None` when code cannot tell an end, or the two ways
    round give different answers: then the formula says nothing."""
    if vendor.span.axis != ref.span.axis or not vendor.known or not ref.known:
        return None
    if not (ref.low.kinds | ref.high.kinds) & _CASEWORK:
        return None  # not a cabinet run
    values = {"field_cut": field_cut, "end_overhang": end_overhang}
    run = format_inches(ref.span.inches)
    found: list[Length] = []
    for ends in _ways(vendor, ref):
        parts = [f"the reference's cabinet run {run}"]
        missing: list[str] = []
        total = ref.span.inches
        for allowance in ends:
            if allowance.kind == "none":
                continue
            value = values[allowance.kind]
            if value is None:
                missing.append(_MISSING_WORDS[allowance.kind])
                continue
            if value < 0 or value > _MAX_ALLOWANCE_IN:
                return None
            total += value
            parts.append(f"{format_inches(value)} {allowance.words}")
        if missing:
            names = tuple(sorted(set(missing)))
            found.append(
                Length(
                    "missing",
                    None,
                    f"the reference's cabinet run {run} plus the " + " and the ".join(names),
                    names,
                )
            )
            continue
        outcome: Outcome = "same" if total == vendor.span.inches else "different"
        found.append(Length(outcome, total, f"{' + '.join(parts)} = {format_inches(total)}"))
    if not found:
        return None
    first = found[0]
    if any(
        other.outcome != first.outcome or other.expected != first.expected for other in found[1:]
    ):
        return None  # the two ways round disagree: code cannot tell which end is which
    return first


def formula(formula_id: str) -> Formula | None:
    """The formula with this id."""
    return _BY_ID.get(formula_id)
