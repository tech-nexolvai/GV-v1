"""Turn a stored finding into two facts and read the verdict table over them (Phase 2, 2026-11-04).

`verdict_facts` holds the types and the six rows and knows nothing about a finding. This module is
the only place that knows both, and it is deliberately thin: it **wraps** the fact producers the
review already has rather than rewriting them.

What it calls, and what each one gives:

- `proof.classified` / `classification` — the 25 reasons, the architect-first rules, the label
  reading, "the architect prints nothing", a short value code never read;
- `dim_ends.unlike_reason` and `centre_lines` — code's proof that two dimensions measure different
  things;
- `drafting_notes.softened` — the reference marks its size V.I.F. or +/-;
- `classification.same_item_conflict` — copies of the item pair it with different sizes;
- `text_values.kind` — a tag, a sheet number, a revision number: text, never a size;
- `proof.maths_result` / the stored inches — the maths, which alone decides a number difference;
- `proof.flag_checked`, `proof.flag_notes` — flag mode's rule for which difference code checked.

**The one change in behaviour.** A reason only makes a fact `Missing` when that side really has no
value. A side that holds a value stays `Present` even where the old code had already named a
reason for it: an architect size printed on the sheet is a value, whether or not a job setting is
missing, whether or not code could pair the two dimensions. That is what stops a difference being
filed as Not compared.

**What is unchanged.** Code still decides red: `group_of` keeps flag mode's own `flag_checked` and
`flag_notes`, keeps the reviewer's decisions, the rule note (#1839) and the proof rules' own
problems, and maps the outcome onto the same four screen groups.

Pure: no I/O, no AI call, no database.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.review.assistant import (
    classification,
    dim_ends,
    drafting_notes,
    likely_problems,
    proof,
    right_proof,
    text_values,
)
from app.review.assistant.round_reference import round_only
from app.review.assistant.span_ends import ends_apart
from app.review.assistant.verdict_facts import (
    Comparable,
    Fact,
    Missing,
    Present,
    Verdict,
    same_inches,
    verdict,
)

__all__ = ["Pair", "facts_of", "group_of", "verdict_of"]

#: Reasons that say the **vendor's own value** could not be read. On any other reason the vendor's
#: side keeps its value: a reason about the pair or about the architect never empties it.
VENDOR_ABSENT_REASONS: Final[frozenset[str]] = frozenset(
    {"unreadable", "no_spot", "stroke_words", "gv_sticker"}
)
#: Reasons that say the **reference** gives nothing: no drawing of the area, no printed size, only
#: parts, a job value nobody entered, the architect's own drawings disagreeing with each other.
REFERENCE_ABSENT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "nothing_to_compare",
        "no_sheet",
        "no_overall",
        "ai_unsure",
        "field_verify",
        "architect_disagrees",
        "missing_setting",
        "not_seen",
    }
)
#: Reasons that are about the **pair**: the two facts do not measure the same thing, or the
#: reference is not the architect's. They go on the pair, never on a side.
PAIR_REASONS: Final[frozenset[str]] = frozenset(
    {
        "different_reference",
        "finish_not_comparable",
        "prototype",
        "prototype_only",
        "round_only",
        "architect_outside_gv_rule",
        "outside_gv_rule",
        "internal",
    }
)
#: A side that says there is no value there. `classification.vendor_absent` catches the vendor's
#: own wording ("not shown", "none"); these are the reference's ("not printed", "n/a").
_NO_VALUE: Final[frozenset[str]] = frozenset(
    {"", "-", "--", "n/a", "na", "none", "nothing", "not applicable", "not available"}
)
#: The side says in its own words that the value is not printed there ("no model printed",
#: "pulls shown on drawers; no hardware spec"). The architect describing what it draws is not a
#: value to compare, and reading it as one made please-checks out of answers with nothing in them.
_SAYS_NO_VALUE: Final = re.compile(
    r"\bno\s+(?:\w+[\s/-]+){0,3}(?:printed|given|dimension(?:ed|s)?|spec(?:s|ified|ification)?|"
    r"size[sd]?|model|value|measurement)\b"
    r"|\bnot\s+(?:printed|given|dimensioned|specified|stated|listed|called\s+out)\b",
    re.IGNORECASE,
)
_NO_VALUE_LEADS: Final[tuple[str, ...]] = (
    "not printed",
    "not shown",
    "not given",
    "not dimensioned",
    "not indicated",
    "not provided",
    "not noted",
    "not stated",
    "not called out",
    "no size",
    "no value",
    "no dimension",
    # The reading pointed at the sheet instead of recording a value: there is nothing to compare.
    "see the drawing",
    "see drawing",
    "see the sheet",
)


class Pair:
    """The two facts of one answer and whether they measure the same thing."""

    __slots__ = ("comparable", "reference", "vendor")

    def __init__(self, vendor: Fact, reference: Fact, comparable: Comparable) -> None:
        self.vendor = vendor
        self.reference = reference
        self.comparable = comparable


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


def _has_value(text: str) -> bool:
    """Whether a side's words hold a value at all, rather than saying there is none."""
    words = text.strip().strip(".").lower()
    if words in _NO_VALUE:
        return False
    if any(words.startswith(lead) for lead in _NO_VALUE_LEADS):
        return False
    if _SAYS_NO_VALUE.search(text):
        return False
    return not classification.vendor_absent(text)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _label(finding: Mapping[str, Any], side: str) -> Mapping[str, Any]:
    """The numbered label code built the side from (`dims`), when there is one."""
    return _map(_map(finding.get("dims")).get(side))


def _inches(finding: Mapping[str, Any], side: str) -> Decimal | None:
    """The side's size in inches, from the comparison's own figures.

    Only these: the figures are the pair the comparison itself worked out, in inches. A numbered
    label's own `inches` is the number as it is printed, which on a vendor's sheet is often the
    millimetres ("[3344]"), and the settled rule is that millimetres are ignored. Reading a
    label's raw number as inches invented differences such as 3344 against 34.
    """
    return _decimal(_map(finding.get("inches")).get(side))


def _box(finding: Mapping[str, Any], side: str) -> tuple[float, float, float, float] | None:
    spot = _map(finding.get("vendor_box" if side == "vendor" else "arch_box"))
    corners = [spot.get(edge) for edge in ("x0", "y0", "x1", "y1")]
    if all(isinstance(corner, (int, float)) for corner in corners):
        return (float(corners[0]), float(corners[1]), float(corners[2]), float(corners[3]))  # type: ignore[arg-type]
    return None


#: A value that is a code rather than a sentence: one word, short, carrying a figure (a tag like
#: `AP-001-XY`, a sheet number, a finish code). It never spells the hyphen, so a code printed
#: with a look-alike dash reads the same (`vocabulary.dashes`).
_CODE_LIKE: Final = re.compile(r"^(?=.{1,24}$)(?=.*[\d/])\S+$")


def _unit(text: str, inches: Decimal | None, finding: Mapping[str, Any]) -> str:
    """What the value is measured in, for the maths: a size in inches, a code that is compared as
    text, a description in words, or nothing."""
    if inches is not None:
        return "inches"
    if text_values.identifier(finding) or text_values.kind(finding) or _CODE_LIKE.match(text):
        return "text"
    return "words" if text else "none"


def _trust(finding: Mapping[str, Any], side: str) -> str:
    """How sure the value is: `code` when code read it at its spot, else `ai`."""
    checks = _map(finding.get("checks"))
    printed = checks.get("printed_vendor" if side == "vendor" else "printed_architect")
    verify = _map(checks.get("verify_vendor" if side == "vendor" else "verify_architect"))
    if printed == "match" or verify.get("result") == "match":
        return "code"
    return "ai" if _flat(finding.get(side if side == "vendor" else "architect")) else "unsure"


def _reason_of(finding: Mapping[str, Any]) -> tuple[str | None, str]:
    """The reason the existing rules give for this answer, and its plain line."""
    found = proof.classified(finding)
    return found.reason, found.why or ""


def _fact(finding: Mapping[str, Any], side: str, source: str) -> Fact:
    """One side as a fact. `side` is the finding's own key (`vendor` or `architect`).

    A side is `Missing` only when it really holds no value, and it then carries the reason the
    existing rules already name (one of the 25). A side that holds a value stays `Present`, even
    where a reason was named for the answer as a whole: that is what stops a difference being
    filed as Not compared.
    """
    key = "vendor" if side == "vendor" else "architect"
    text = _flat(finding.get(key))
    reason, why = _reason_of(finding)
    if key == "vendor" and classification.missing_on_vendor(finding):
        # "the vendor does not show it" is the vendor's own answer, not a missing reading: the
        # architect draws the item and the vendor's sheet leaves it out, which is a difference.
        return Present(
            value=text or "not shown",
            unit="words",
            page=_page(finding, key, {}),
            box=_box(finding, key),
            source=source,
            method="ai",
            trust="ai",
        )
    if not _has_value(text):
        return Missing(reason or "nothing_to_compare", why)
    if side == "vendor" and reason in VENDOR_ABSENT_REASONS and reason != "no_spot":
        # The vendor's own value could not be read at its spot: there is no value to compare,
        # whatever words the reading left behind.
        return Missing(reason, why)
    inches = _inches(finding, key)
    label = _label(finding, key)
    return Present(
        value=text,
        unit=_unit(text, inches, finding),  # type: ignore[arg-type]
        inches=inches,
        page=_page(finding, key, label),
        box=_box(finding, key),
        source=source,
        method=str(label.get("route") or ("ai" if not label else "text")),
        trust=_trust(finding, key),  # type: ignore[arg-type]
    )


def _page(finding: Mapping[str, Any], key: str, label: Mapping[str, Any]) -> int | None:
    page = label.get("page") if label else (finding.get("page") if key == "vendor" else None)
    page = page if page is not None else finding.get("vs_page")
    return int(page) if isinstance(page, int) else None


def _vendor_sum(finding: Mapping[str, Any]) -> Present | None:
    """The vendor's own parts added up, when the answer holds its own string against its printed
    overall (`parts`, `parts_sum`): the reference is then the vendor's own drawing, not the
    architect's."""
    parts = finding.get("parts")
    total = finding.get("parts_sum")
    if not isinstance(parts, list) or not parts:
        return None
    text = _flat(total) or _flat(finding.get("architect"))
    if not text:
        return None
    return Present(
        value=text,
        unit="inches",
        inches=_decimal(_map(finding.get("inches")).get("architect")),
        page=_page(finding, "vendor", _label(finding, "vendor")),
        source="vendor_parts",
        method="sum",
        trust="code" if proof.string_verified(finding) else "ai",
    )


def _maths_fact(finding: Mapping[str, Any], side: str, source: str) -> Fact | None:
    """The side as code's own maths step read it (`proof.maths_result`, `ai_maths`).

    When that step ran it had a number for each side — the vendor's parts and its printed overall,
    the two sizes, GV's rule and the value it is worked out on — and its answer is the maths. The
    reading's own words are then only how it described the item, so a page reviewer's "see the
    drawing" is not an absent value here. `None` when the maths step did not run.
    """
    kind = proof.maths_kind(finding)
    if proof.maths_result(finding) is None:
        return None
    key = "vendor" if side == "vendor" else "architect"
    text = _flat(finding.get(key)) or _flat(proof.proof_line(finding))
    return Present(
        value=text,
        unit="text" if kind == "identifier" else "inches",
        inches=_inches(finding, key),
        page=_page(finding, key, _label(finding, key)),
        box=_box(finding, key),
        source=source,
        method="maths",
        trust="code",
    )


def _rule_fact(finding: Mapping[str, Any]) -> Fact | None:
    """The reference when it is **GV's own rule** rather than a drawing (`source: rules`, the sink
    offset, the countertop formula, the filler range).

    GV's rule check is the fact producer here, and its own record says whether it ran
    (`checks.rule`, `witness`: `not_run` when it did not). A rule that did not run has no value,
    whatever standard the words quote: the vendor's 4" from the stone's edge against GV's 4" from
    the cabinet front are two different measurements, and reading the quoted standard as a value
    made that a green tick. `None` when the reference is not a rule, or when the rule did run.
    """
    if finding.get("source") != "rules":
        return None
    if _map(finding.get("checks")).get("rule") != "not_run":
        return None
    reason, why = _reason_of(finding)
    return Missing(reason or "missing_setting", why)


def _reference_source(finding: Mapping[str, Any]) -> str:
    source = str(finding.get("source") or "")
    if source in ("brand_prototype", "gv_review", "completeness"):
        return source
    return "gv_rule" if finding.get("rule_check_id") else "architect"


def _comparable(finding: Mapping[str, Any], vendor: Fact, reference: Fact) -> Comparable:
    """Whether the two facts measure the same thing.

    Only what code **proved** takes a comparison away. "Code could not prove the pairing" is not a
    proof of anything and never hides a difference (that was the leak): it leaves the pair
    comparable and the difference is then a please-check, not a red, because `flag_checked` on the
    verdict decides red, not this.
    """
    if classification.internal(finding):
        return Comparable(False, "internal", "for GV's reviewer only: never shown to the vendor")
    if round_only(finding):
        return Comparable(False, "round_only", "compared with the vendor's own earlier round only")
    if finding.get("source") == "brand_prototype" and not classification.prototype_governs(finding):
        return Comparable(False, "prototype_only", "the brand prototype is reference only")
    softened = drafting_notes.softened(finding)
    if softened is not None:
        return Comparable(False, "field_verify", _flat(softened), proved=True)
    conflict = classification.same_item_conflict(finding)
    if conflict is not None:
        joined = "; ".join(str(line) for line in conflict)
        return Comparable(False, "architect_disagrees", joined, proved=True)
    unlike = dim_ends.unlike_reason(finding)
    if unlike is not None:
        return Comparable(False, "different_reference", _flat(unlike), proved=True)
    if text_values.kind(finding) == "revision":
        return Comparable(
            False, "different_reference", "each drawing has its own revision count", proved=True
        )
    apart = right_proof.different(finding)
    if apart is not None:
        return Comparable(False, "different_reference", _flat(apart))
    spans = _spans_apart(finding)
    if spans is not None:
        return Comparable(False, "different_reference", spans, proved=True)
    if classification.short_unread(finding):
        return Comparable(False, "not_proved", "a short value code did not read at both spots")
    reason, why = _reason_of(finding)
    if reason in ("finish_not_comparable", "architect_outside_gv_rule", "outside_gv_rule"):
        return Comparable(False, reason, why)
    return Comparable(True)


def _spans_apart(finding: Mapping[str, Any]) -> str | None:
    """The two sides run to different points (R8: the underside of the counter against its top),
    as the pairing's own record of each side's ends says (`span_ends.ends_apart`)."""
    reply = _map(_map(finding.get("pairing")).get("advocate"))
    apart = ends_apart(reply.get("vendor_spans"), reply.get("architect_spans"))
    if apart is None:
        return None
    return f"the vendor's dimension runs to {apart[0]}, the architect's to {apart[1]}"


def _differs(finding: Mapping[str, Any], vendor: Fact, reference: Fact) -> bool:
    """The maths result on the two values: code's own arithmetic first, then the inches compared
    exactly, and for words and tags what the reading itself says. Never a judgement of its own."""
    maths = proof.maths_result(finding)
    if maths == "differs":
        return True
    if maths == "same":
        return False
    gap = _decimal(_map(finding.get("inches")).get("difference"))
    if gap is not None:
        return gap != 0  # the comparison's own figures, in inches
    if (
        isinstance(vendor, Present)
        and isinstance(reference, Present)
        and vendor.inches is not None
        and reference.inches is not None
    ):
        return not same_inches(vendor.inches, reference.inches)
    return proof.says_differs(finding)


def _slot_pair(finding: Mapping[str, Any]) -> Pair | None:
    """The facts a slot answer's code already decided (`slot_maths`, Step 4): the AI named marks,
    code read and compared them, and its record holds both facts and the pair. `None` for every
    other answer (loaded only then, so an answer without a slot record reads exactly as before)."""
    record = finding.get("slot")
    if not isinstance(record, Mapping):
        return None
    from app.review.assistant.slot_maths import pair_of

    found = pair_of(record)
    return None if found is None else Pair(*found)


def facts_of(finding: Mapping[str, Any]) -> Pair:
    """The answer's two facts and whether they measure the same thing (a slot answer's own, as
    code decided them, when it has a slot record)."""
    slotted = _slot_pair(finding)
    if slotted is not None:
        return slotted
    vendor = _maths_fact(finding, "vendor", "vendor") or _fact(finding, "vendor", "vendor")
    reference = (
        _maths_fact(finding, "architect", _reference_source(finding))
        or _vendor_sum(finding)
        or _rule_fact(finding)
        or _fact(finding, "architect", _reference_source(finding))
    )
    return Pair(vendor, reference, _comparable(finding, vendor, reference))


def verdict_of(finding: Mapping[str, Any]) -> Verdict:
    """The answer's verdict (`Same`, `Differs` or `NotCompared`) and the row that gave it."""
    pair = facts_of(finding)
    return verdict(
        pair.vendor,
        pair.reference,
        pair.comparable,
        _differs(finding, pair.vendor, pair.reference),
    )


def group_of(finding: Mapping[str, Any]) -> str:
    """The result screen's group for an answer in flag mode, from its verdict.

    The screen is unchanged: `Same` is Right, `Differs` is clouded when code checked it
    (`proof.flag_checked`, which alone decides red) and a please-check otherwise, `NotCompared` is
    Not compared. The reviewer's own decision, the rule note (#1839) and the proof rules' own
    problems are kept exactly as flag mode already keeps them.

    Off flag mode the proof rules decide as they always have: the table is flag mode's, and flag
    mode is the decided way to run (Anant, 2026-11-04).
    """
    if not proof.flag_mode():
        return proof.proof_rules_group(finding)
    if classification.internal(finding):
        return "not_compared"  # GV's reviewer only (#1652): never clouded, whoever clicks
    decided = proof.decided(finding)
    checked = proof.flag_checked(finding)
    found = verdict_of(finding)
    if not checked and found.same:
        return "right"
    if decided == "agree" and proof.stated_requirement(finding) is not None:
        return "clouded"
    if decided == "not_a_problem":
        return "dismissed"
    if likely_problems.unsized_ends(finding) is not None:
        return "likely_problem"
    pair = facts_of(finding).comparable
    if not pair.ok and pair.proved:
        # Code proved the two do not measure the same thing: never red, whatever else says so.
        return _held_back(finding, pinned=True)
    if checked:
        return "likely_problem" if proof.flag_notes(finding) else "clouded"
    if found.differs:
        return "likely_problem"
    return _held_back(finding, pinned=False, reason=found.absent.reason if found.absent else None)


def _held_back(finding: Mapping[str, Any], *, pinned: bool, reason: str | None = None) -> str:
    """What to show for an answer the table did not compare.

    Two things are kept exactly as they were. What the proof rules show as a problem is never
    hidden (a held reading, a typed-over number, unconfirmed ends, a blind pick against code's
    geometry, a track-record hold), and the architect's size that breaks a GV rule stays the
    decided rule note (#1839). Then flag mode's own rule (Anant, 2026-11-04): only a reason that
    says *nothing was compared* (`classification.NO_COMPARISON`) keeps an answer off the problems
    list; on any other reason an answer that says something differs is a please-check. A pair code
    proved unlike is `pinned`: it is never a problem either way.
    """
    without = {key: value for key, value in finding.items() if key != "decision"}
    was = proof.proof_rules_group(without)
    if was in ("clouded", "likely_problem"):
        return "likely_problem"
    if was == "rule_note":
        return "rule_note"
    nothing = pinned or reason in classification.NO_COMPARISON
    if nothing and finding.get("source") != "completeness":
        return "not_compared"
    return "likely_problem" if proof.says_differs(finding) else "not_compared"
