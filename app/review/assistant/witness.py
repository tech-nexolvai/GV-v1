"""Which checks on a chat finding are NOT an AI reading (chat accuracy, P4): the trust rule.

A finding is the AI's claim. What backs it is one of three kinds of evidence:

- **observed**: code read both values inside the finding's boxes: the page's text prints them
  (`printed`), or the vendor's drawn ink reads as them to two OCR engines (`ocr_ink`, #1531); no AI
  involved;
- **derived**: code worked it out from values: GV's rule check, the vendor's own sum adding up (or
  not), the reading ledger (a dimension string that adds up, other views of the item agreeing), or
  the millimetres and inches printed together agreeing;
- **inferred**: only AI readings back it (the lead's own reading, its re-check, a second read).

**Trust rule.** Confirmed needs at least one non-AI witness (observed or derived) AND an AI witness
that agrees: the read (the second read, or the third read's majority when one settled it, #1498),
the lead's own check on the drawing, or the critic passing it. Two AI witnesses agreeing with no
non-AI witness is Likely at best; a non-AI witness alone is Likely. Any check that disagrees makes
it To check (the read, the critic objecting, GV's rule, code's arithmetic, the ledger, mm/inch, or
the drawing printing another value at the spot). A side code cannot read is neutral: it neither
backs nor contradicts.

**The drawn-ink witness (`ocr_ink`, #1531) is not AI, but it is a reading by OCR**: it measured one
slip in 136 planted wrong values (a hand-lettered `7` read as `1`). So it gives Confirmed only
together with the independent read agreeing (the second read, or the third read's majority), never
with the lead's own check or the critic alone (#756 D2: never alone, only with a second independent
witness); with them it gives Likely.

Pure code, no imports beyond the standard library, so both `finding_checks` and `chat_trust` use it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

__all__ = [
    "EVIDENCE_KINDS",
    "WITNESS_WORDS",
    "EvidenceKind",
    "ai_witnesses",
    "critic_passed",
    "disagreement",
    "evidence_kind",
    "printed_elsewhere",
    "read_result",
    "trust_level",
    "witnesses",
]

type EvidenceKind = Literal["observed", "derived", "inferred"]
EVIDENCE_KINDS: Final = ("observed", "derived", "inferred")

#: What each non-AI witness means, in plain words (the screen's tooltip and the accuracy table).
WITNESS_WORDS: Final = {
    "printed": "both values printed at their spots on the drawing",
    "rule": "GV's own rule check agrees",
    "arithmetic": "code added up the vendor's dimension string",
    "ledger": "code's reading ledger backs it (a string that adds up, or other views agree)",
    "units": "the millimetres and inches printed together agree",
    "ocr_ink": "both values read from the vendor's drawn ink by two OCR engines",
}

#: Each side's reading by `verify_value` in a finding's checks (#1531).
_VERIFY_KEYS: Final = ("verify_architect", "verify_vendor")


def _arithmetic_backs(checks: Mapping[str, Any], status: object) -> bool | None:
    """Code's sum backs the finding (True), contradicts it (False), or says nothing (None).

    A sum that does not add up contradicts a "same"; one that adds up contradicts a "different".
    Otherwise (and with no status given) the sum code made backs the finding: its claim is about
    the sum."""
    arithmetic = checks.get("arithmetic")
    if arithmetic == "does_not_add_up":
        return status != "same"
    if arithmetic == "adds_up":
        return status != "different"
    return None


def read_result(checks: Mapping[str, Any]) -> object:
    """The read's result: the third read's majority when one was made (#1498), else the second
    read's. A third read that ends unsure leaves the second read's result as it was."""
    adjudication = checks.get("adjudication")
    if adjudication == "agrees_after_adjudication":
        return "agrees"
    if adjudication == "disagrees":
        return "disagrees"
    return checks.get("second_read")


def critic_passed(checks: Mapping[str, Any]) -> bool:
    """The adversarial critic passed it (an AI witness, never a non-AI one)."""
    return checks.get("critic") == "passed"


def ai_witnesses(checks: Mapping[str, Any]) -> list[str]:
    """The AI readings that agree with the finding: `read`, `lead`, `critic`."""
    out: list[str] = []
    if read_result(checks) == "agrees":
        out.append("read")
    if checks.get("lead_checked") is True:
        out.append("lead")
    if critic_passed(checks):
        out.append("critic")
    return out


def _verified(checks: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    found = checks.get(key)
    return found if isinstance(found, Mapping) else None


def _ink_backs(checks: Mapping[str, Any]) -> bool:
    """Both sides read at their spots by code, at least one from the vendor's drawn ink."""
    sides = [_verified(checks, key) for key in _VERIFY_KEYS]
    printed = (checks.get("printed_architect"), checks.get("printed_vendor"))
    read = [
        side is not None and side.get("result") == "match" or text == "match"
        for side, text in zip(sides, printed, strict=True)
    ]
    drawn = any(
        side is not None and side.get("result") == "match" and side.get("route") == "drawn"
        for side in sides
    )
    return all(read) and drawn


def printed_elsewhere(checks: Mapping[str, Any]) -> str | None:
    """ "the drawing prints X here": a side whose spot prints another value (`verify_value`
    mismatch), in plain words; `None` when none does."""
    for key in _VERIFY_KEYS:
        side = _verified(checks, key)
        if side is not None and side.get("result") == "mismatch":
            return f"the drawing prints {side.get('read')} here, not {side.get('value')}"
    return None


def witnesses(checks: Mapping[str, Any], status: object = None) -> list[str]:
    """The non-AI witnesses that back the finding, in a fixed order."""
    out: list[str] = []
    if checks.get("printed_architect") == "match" and checks.get("printed_vendor") == "match":
        out.append("printed")
    elif _ink_backs(checks):
        out.append("ocr_ink")
    if checks.get("rule") == "agrees":
        out.append("rule")
    if _arithmetic_backs(checks, status) is True:
        out.append("arithmetic")
    if checks.get("ledger") == "agrees":
        out.append("ledger")
    if checks.get("units") == "agrees":
        out.append("units")
    return out


def disagreement(checks: Mapping[str, Any], status: object = None) -> str | None:
    """Which check disagrees with the finding, in plain words; `None` when none does."""
    if checks.get("adjudication") == "disagrees":
        return "the third read sides with the second read against it"
    if read_result(checks) == "disagrees":
        return "the second read disagrees"
    if checks.get("critic") == "objects":
        return "the critic objects"
    if checks.get("rule") == "disagrees":
        return "GV's recorded check disagrees"
    if _arithmetic_backs(checks, status) is False:
        return "code's arithmetic disagrees"
    if checks.get("ledger") == "disagrees":
        return "code's reading ledger disagrees"
    if checks.get("units") == "disagrees":
        return "the millimetres and inches printed together disagree"
    return printed_elsewhere(checks)


def evidence_kind(checks: Mapping[str, Any], status: object = None) -> EvidenceKind:
    """`observed` when the text layer prints both values at their spots, `derived` when code worked
    it out, `inferred` when only AI readings back it."""
    found = witnesses(checks, status)
    if "printed" in found or "ocr_ink" in found:
        return "observed"
    return "derived" if found else "inferred"


def trust_level(checks: Mapping[str, Any], status: object = None) -> str:
    """Confirmed / Likely / To check (see the module's trust rule)."""
    if disagreement(checks, status) is not None:
        return "to_check"
    found = witnesses(checks, status)
    non_ai = bool(found)
    ai = len(ai_witnesses(checks))
    # The drawn-ink witness confirms only with the independent read (see the module's rule).
    full = [w for w in found if w != "ocr_ink"]
    if full and ai:
        return "confirmed"
    if "ocr_ink" in found and read_result(checks) == "agrees":
        return "confirmed"
    if non_ai or ai >= 2:
        return "likely"
    return "to_check"
