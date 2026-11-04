"""Code's maths on the AI's own numbers (flag mode, Anant 2026-11-04), $0, no AI.

In flag mode the AI reads every page, in any vendor's style, and code only does the maths on what
it read: string sums, exact vendor-against-architect inches and GV's rules. Before, an item the AI
found was red only when code read its numbers again from the drawing's own lines, the
vendor-specific part that keeps breaking (on a real set, "the string 3" + 47 1/8" (EQ) + 3" adds up
to 53 1/8", but the overall is 53"" stayed a please-check because code read another label at the
box; values made up here). Here code redoes the maths on the numbers the AI wrote down, and never
reads the drawing:

- **A string** (`parts` and `printed_total`: the finding's own, or the page reviewer's plain fields,
  `NUMBERS`): code adds the parts in inches and compares the sum with the printed overall exactly.
  A value printed millimetres first with its inches in brackets counts by its inch figures (settled
  with GV: inches count, millimetres are ignored); a string written in millimetres only is
  unreadable. When the AI gave no fields, its sentence "A + B + C = X, but the overall is Y" is read
  instead (`_sentence_claims`), only where it is plain (see there).
- **Two sizes** (a helper's `vendor` and `architect`; the page reviewer's `vendor_value` and
  `other_value`): compared exactly in inches (1/8" apart is a difference). A side that says the
  thing is not there is a missing-item claim, not maths.
- **GV's rules** (the page reviewer's `gv_rule`), applied by code with the job's values (`rules.md`,
  else GV's company standard): the top's length is the run it covers plus the field cut at each
  wall end (1" per wall end, `field_cut.per_end_from`); the sink's front offset from the cabinets'
  front (4", `sink_offset.setting_from`); the sink cut-out is the sink's inside size less the
  clearance each side (1/4", `sink_cutout_clearance`). The AI gives the printed numbers; code never
  takes the rule's own value from the AI.
- **Never inches:** a sheet or revision number, a callout, a tag, an item number or a date
  (`text_values.identifier`, or an item that names one and no size) is text: unreadable here.

The record (`FIELD`) is `{"result": "differs" | "same" | "unreadable", "words": "<one plain sentence
with the sum>", "kind": "string" | "sizes" | "rule" | "identifier", "read_from": "fields" |
"sentence"}`, or none when the finding states no maths (a construction, finish or scope claim; a
missing piece). Several claims on one finding: `differs` when any differs, `same` only when every
one is the same, else `unreadable`.

**`same` means the AI's own numbers show no difference** (its sum was wrong, or the two values are
equal): in flag mode (`assistant_flag_mode`) the result builder drops such a claim from Problems;
`differs` marks the flag as code-checked; `unreadable` leaves it as the AI flagged it. This module
only writes the record, in both modes, and changes no outcome.

Pure code: no I/O, no AI call. Verification: `tests/review/assistant/test_ai_maths.py` (made-up
values).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final, Literal

from app.review.assistant import likely_problems, text_values
from app.review.assistant.finding_checks import to_inches
from app.review.assistant.ledger import bare_millimetres, format_inches
from units.normalise import plain_marks

__all__ = [
    "FIELD",
    "NUMBERS",
    "RULES",
    "Claim",
    "check",
    "claims_of",
    "mark_answer",
    "numbers_of",
]

_log = logging.getLogger(__name__)

type Result = Literal["differs", "same", "unreadable"]
type Kind = Literal["string", "sizes", "rule", "identifier"]

#: The finding's field for code's maths on the AI's numbers.
FIELD: Final = "maths"
#: The page reviewer's mark (`likely_problems.FIELD`): the AI's numbers as plain fields
#: (`numbers_of`).
NUMBERS: Final = "numbers"
#: GV's rules code applies on the AI's numbers (`gv_rule.rule`).
RULES: Final = ("field_cut", "sink_front_offset", "sink_cutout")
#: GV's company standards, when the chat's `rules.md` sets no value (settled with the client: sink front
#: offset 4", clearance 1/4" each side; the field cut's 1" is `field_cut.DEFAULT_PER_END`).
FRONT_OFFSET: Final = Fraction(4)
CLEARANCE: Final = Fraction(1, 4)

#: A GV rule's plain fields (`gv_rule`), and how long each value may be.
_RULE_FIELDS: Final = ("rule", "printed", "run", "wall_ends", "sink_inside", "overhang")
_VALUE_CHARS: Final = 60
_MOST_PARTS: Final = 40
#: The sources whose two sides are a vendor size and the size it is compared with. A string's
#: sides are its total and code's sum (or the wrong part and what it should be); a GV rule's are
#: code's own reading of the rule (`sink_offset`: printed from the top's edge, not the cabinets').
_TWO_SIZES: Final = frozenset({"architect", "brand_prototype", "gv_review", "cross_view"})
#: A value that is not one exact size: a minimum, a maximum, a nominal or rough value, a size to
#: verify on site.
_NOT_EXACT: Final = re.compile(
    r"\b(?:MIN(?:IMUM)?|MAX(?:IMUM)?|NOM(?:INAL)?|APPROX\w*|VIF|VERIFY|HOLD|NTS|TBD|TBV|UP\s+TO|"
    r"ABOUT)\b|±|\+/-|~",
    re.IGNORECASE,
)
#: Millimetres first with the inches in brackets (`398.5 [15 3/4]`, `36 mm [1 1/2]`), or the inches
#: in brackets alone (`[129]`): the inches count.
_BRACKETED: Final = re.compile(
    r"^(?:\d+(?:\.\d+)?\s*(?:mm\.?)?\s*)?\[\s*([^\]]+?)\s*\]$", re.IGNORECASE
)
#: A side that says the thing is not there, or points elsewhere: no size of its own.
_ABSENT: Final = re.compile(
    r"^\(?\s*(?:not\b|no\b(?!\.)|none\b|missing\b|absent\b|n/?a\b|see\b|tbd\b|unread\w*|"
    r"illegible\b|-+$|$)",
    re.IGNORECASE,
)
#: An item that names an identifier and no size (a tag code or a number read as a size proves
#: nothing): the words `text_values` reads, kept short here.
_IDENTIFIER_ITEM: Final = re.compile(
    r"\btags?\b|\brev(?:ision)?s?\b|\bsheet\b|\bdwg\b|\bcallouts?\b|\bkey\s*notes?\b|\bdates?\b|"
    r"\b(?:item|room|unit|cabinet|detail|section)\s*(?:no\b\.?|number|#)",
    re.IGNORECASE,
)
_SIZE_ITEM: Final = re.compile(
    r"\b(?:widths?|wide|depths?|deep|heights?|high|tall|lengths?|long|offsets?|sizes?|overhangs?|"
    r"clearances?|strings?|spans?|dimensions?|thickness|run|overall|field\s*cut|cut-?outs?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Claim:
    """One piece of maths on one finding: what it is, code's result and its plain words."""

    kind: Kind
    result: Result
    words: str
    read_from: Literal["fields", "sentence"] = "fields"


# ---- the AI's plain number fields ----------------------------------------------------------------


def _value(raw: object) -> str | None:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    text = " ".join(str(raw).split())[:_VALUE_CHARS]
    return text or None


def numbers_of(given: Mapping[str, Any]) -> dict[str, Any] | None:
    """The plain number fields of one page-reviewer report (`report_issue`: a string's `parts` and
    `printed_total`, two sizes `vendor_value` and `other_value`, a GV rule's `gv_rule`), as given,
    for its mark (`NUMBERS`); `None` when it gave none."""
    out: dict[str, Any] = {}
    parts = given.get("parts")
    if isinstance(parts, list):
        kept = [v for v in (_value(p) for p in parts[:_MOST_PARTS]) if v is not None]
        if len(kept) >= 2:
            out["parts"] = kept
    for name in ("printed_total", "vendor_value", "other_value"):
        value = _value(given.get(name))
        if value is not None:
            out[name] = value
    rule = given.get("gv_rule")
    if isinstance(rule, Mapping) and rule.get("rule") in RULES:
        kept_rule: dict[str, Any] = {"rule": rule["rule"]}
        for name in _RULE_FIELDS[1:]:
            if name == "wall_ends":
                ends = rule.get(name)
                if isinstance(ends, int) and not isinstance(ends, bool) and 0 <= ends <= 2:
                    kept_rule[name] = ends
                continue
            value = _value(rule.get(name))
            if value is not None:
                kept_rule[name] = value
        out["gv_rule"] = kept_rule
    return out or None


# ---- one value in inches ---------------------------------------------------------------------------


def _inches(text: object) -> Decimal | None:
    """One printed size in exact inches, or `None`: a value that is not one exact size (a
    minimum, two values, millimetres only, words). Millimetres first with the inches in brackets
    count by the inch figures (settled with GV)."""
    if not isinstance(text, str):
        return None
    token = " ".join(plain_marks(text).split())
    if not token or _NOT_EXACT.search(token):
        return None
    bracketed = _BRACKETED.match(token)
    if bracketed is not None:
        token = bracketed.group(1)
    return to_inches(token)


def _say(value: Decimal | Fraction) -> str:
    return format_inches(value)


def _apart(a: Decimal, b: Decimal) -> str:
    return f"{_say(abs(a - b))} apart"


def _absent(text: object) -> bool:
    return isinstance(text, str) and _ABSENT.match(" ".join(text.split())) is not None


def _quoted(text: object) -> str:
    return f'"{" ".join(str(text or "").split())[:_VALUE_CHARS]}"'


# ---- a string ------------------------------------------------------------------------------------


def _string(parts: Sequence[object], total: object, said: Decimal | None = None) -> Claim:
    """A string's parts added up in inches against its printed overall (`said`: the AI's own sum,
    named when code's differs from it)."""
    texts = [str(p) for p in parts]
    if bare_millimetres(texts, str(total or "")):
        return Claim(
            "string",
            "unreadable",
            "The string is written in millimetres only; GV counts inches, so code does not add it.",
        )
    values = [_inches(p) for p in texts]
    printed = _inches(total)
    unread = [t for t, v in zip(texts, values, strict=True) if v is None]
    if printed is None:
        unread.append(str(total or ""))
    if unread:
        return Claim(
            "string",
            "unreadable",
            f"Code cannot read {', '.join(_quoted(t) for t in unread[:3])} as one size in inches, "
            "so it cannot add the string.",
        )
    assert printed is not None
    summed = sum((v for v in values if v is not None), Decimal(0))
    sum_words = f"Code adds {' + '.join(_say(v) for v in values if v is not None)} = {_say(summed)}"
    slip = (
        f" (the AI's own sum, {_say(said)}, was wrong)"
        if said is not None and said != summed
        else ""
    )
    if summed == printed:
        return Claim(
            "string",
            "same",
            f"{sum_words}, the same as the printed overall {_say(printed)}: the string adds up"
            f"{slip}.",
        )
    return Claim(
        "string",
        "differs",
        f"{sum_words}; the printed overall is {_say(printed)}: {_apart(summed, printed)}{slip}.",
    )


# ---- two sizes -----------------------------------------------------------------------------------


def _sizes(vendor: object, other: object, other_name: str) -> Claim | None:
    """The vendor's size against the size it should match, exactly in inches; `None` when a side
    says the thing is not there (a missing-item claim) or neither side is a size."""
    if _absent(vendor) or _absent(other):
        return None
    a, b = _inches(vendor), _inches(other)
    if a is None and b is None:
        return None  # words or tags on both sides: no inch maths
    if a is None or b is None:
        unread = vendor if a is None else other
        return Claim(
            "sizes",
            "unreadable",
            f"Code cannot read {_quoted(unread)} as one size in inches, so it cannot compare "
            f"the vendor's {_quoted(vendor)} with {other_name} {_quoted(other)}.",
        )
    if a == b:
        return Claim(
            "sizes",
            "same",
            f"The vendor's {_say(a)} and {other_name} {_say(b)} are the same in inches: no "
            "difference.",
        )
    return Claim(
        "sizes",
        "differs",
        f"The vendor's {_say(a)} against {other_name} {_say(b)}: {_apart(a, b)}.",
    )


# ---- GV's rules ----------------------------------------------------------------------------------


def _setting(rules_md: str | None, key: str, name: str, standard: Fraction) -> tuple[Fraction, str]:
    """A rule's value in force: the chat's `rules.md` (a project or drawing-set value), else GV's
    company standard."""
    from app.review.assistant.sink_offset import setting_from

    found = setting_from(rules_md, key, name)
    if found is None:
        return standard, "GV standard"
    return found.value, found.origin


def _rule(rule: Mapping[str, Any], rules_md: str | None) -> Claim:
    """GV's rule applied by code on the printed numbers the AI read (see the module)."""
    name = rule.get("rule")
    printed = _inches(rule.get("printed"))
    if printed is None:
        return Claim(
            "rule",
            "unreadable",
            f"Code cannot read {_quoted(rule.get('printed'))} as one size in inches, so it cannot "
            "apply GV's rule.",
        )
    if name == "field_cut":
        from app.review.assistant.field_cut import per_end_from

        run, ends = _inches(rule.get("run")), rule.get("wall_ends")
        if run is None or not isinstance(ends, int):
            return Claim(
                "rule",
                "unreadable",
                "GV's field cut needs the run the top covers and how many of its ends meet a "
                "wall; the AI did not give both.",
            )
        per_end = per_end_from(rules_md)
        expected = Fraction(run) + per_end * ends
        how = f"{_say(run)} + {ends} x {_say(per_end)} field cut = {_say(expected)}"
        what = "the top's length"
    elif name == "sink_front_offset":
        value, origin = _setting(
            rules_md, "front_offset_required", "sink front offset", FRONT_OFFSET
        )
        overhang = _inches(rule.get("overhang")) if rule.get("overhang") is not None else None
        if rule.get("overhang") is not None and overhang is None:
            return Claim(
                "rule",
                "unreadable",
                f"Code cannot read the overhang {_quoted(rule.get('overhang'))} as one size in "
                "inches, so it cannot apply GV's sink front offset.",
            )
        if overhang is not None:
            expected = value + Fraction(overhang)
            how = f"{_say(value)} ({origin}) + {_say(overhang)} overhang = {_say(expected)}"
            what = "the front offset from the top's front edge"
        else:
            expected = value
            how = f"{_say(value)} ({origin})"
            what = "the front offset from the cabinets' front"
    elif name == "sink_cutout":
        inside = _inches(rule.get("sink_inside"))
        if inside is None:
            return Claim(
                "rule",
                "unreadable",
                "GV's sink cut-out rule needs the sink's inside size; the AI did not give one "
                "code can read.",
            )
        clearance, origin = _setting(rules_md, "sink_cutout_clearance", "clearance", CLEARANCE)
        expected = Fraction(inside) - 2 * clearance
        how = f"{_say(inside)} - 2 x {_say(clearance)} clearance ({origin}) = {_say(expected)}"
        what = "the cut-out"
    else:
        return Claim("rule", "unreadable", "Not one of GV's rules code applies.")
    exact = Fraction(printed)
    if exact == expected:
        return Claim(
            "rule",
            "same",
            f"GV's rule: {how}; the vendor prints {_say(printed)} for {what}: the same.",
        )
    return Claim(
        "rule",
        "differs",
        f"GV's rule: {how}; the vendor prints {_say(printed)} for {what}: "
        f"{format_inches(abs(exact - expected))} apart.",
    )


# ---- the sentence fallback -----------------------------------------------------------------------

#: One size as a sentence writes it: millimetres with the inches in brackets (`36 [1 1/2]`), the
#: inches in brackets alone (`[129]`), feet and inches, a whole number and a fraction, a fraction, a
#: number; with its unit mark, and an equal-split or other short note in words after it.
_SIZE: Final = (
    r"(?:\d+(?:\.\d+)?\s*(?:mm\.?\s*)?\[[^\]\[]{1,16}\]"
    r"|\[[^\]\[]{1,16}\]"
    r"|\d+\s*'\s*-?\s*\d+(?:\s+\d+/\d+)?\s*\"?"
    r"|\d+\s+\d+/\d+\s*\"?"
    r"|\d+/\d+\s*\"?"
    r"|\d+(?:\.\d+)?(?:\s*mm\b|\s*\")?)"
)
_NOTE: Final = r"(?:\s*\((?=[^()]*[A-Za-z])[^()]{1,40}\))?"
_SUMS_TO: Final = (
    r"(?:=|\badds?\s+(?:up\s+)?to\b|\badd\s+(?:up\s+)?to\b|\bcomes?\s+to\b|\btotals?\b"
    r"|\bsums?\s+to\b|\bequals?\b|\bis\b)"
)
#: "A + B + C = X", "A + B + C adds up to X", "A + B (X)": the parts, and the AI's own sum.
_CHAIN: Final = re.compile(
    rf"(?P<parts>{_SIZE}{_NOTE}(?:\s*\+\s*{_SIZE}{_NOTE})+)"
    rf"(?:\s*{_SUMS_TO}\s*(?P<said>{_SIZE})|\s*\(\s*(?P<said2>{_SIZE})\s*\))?",
    re.IGNORECASE,
)
_PART: Final = re.compile(rf"({_SIZE}){_NOTE}", re.IGNORECASE)
#: Where a sentence names the printed overall a string should add up to.
_OVERALL: Final = re.compile(
    rf"(?:\boverall\b|\btotal\b|\bO\.A\.)[^.;:+]{{0,80}}?\b(?:is|are|prints?|printed|reads?|"
    rf"shows?|of|at|as|gives?)\s+(?:printed\s+(?:as\s+)?|as\s+)?(?P<a>{_SIZE})"
    rf"|(?:\boverall\b|\bO\.A\.)\s+(?P<e>{_SIZE})"
    rf"|(?P<b>{_SIZE})\s*(?:printed\s+)?(?:\boverall\b|\bO\.A\.|\bprinted\b)"
    rf"|(?:\bprints?\b|\bprinted\b|\bagainst\b|\bvs\.?|\bversus\b|≠|\bnot\b)\s+(?:the\s+|a\s+|its\s+)?"
    rf"(?:printed\s+)?(?:overall\s+)?(?P<c>{_SIZE})"
    rf"|(?P<d>{_SIZE})\s+(?:does|do)\s*(?:not|n't)\s+(?:equal|match|add)",
    re.IGNORECASE,
)
#: Words that the AI says the string and the overall differ.
_MISMATCH: Final = re.compile(
    r"\bnot\b|n't\b|≠|\bbut\b|\bagainst\b|\bshort\b|\bmore than\b|\bless than\b|\boff\b|"
    r"\binstead\b|\bdiffer\w*|\bmismatch\w*",
    re.IGNORECASE,
)
#: A bare number this big in a sentence is millimetres, never an overall in inches (as
#: `ledger.bare_millimetres` takes a part).
_MM_AT_LEAST: Final = Decimal(400)
#: A multiplier beside a chain ("2 x 1"", "6x6", "1" at each wall end"): code does not read it.
_TIMES: Final = re.compile(r"(?:^|\s)(?:x|×|\*|@)\s*\d|\d\s*(?:x|×|\*|@)(?:\s|\d|$)", re.IGNORECASE)
_TIMES_WORDS: Final = re.compile(r"\b(?:each|per|both|times|twice)\b", re.IGNORECASE)
_METRIC_WORDS: Final = re.compile(r"\bmm\b|\bmillimet\w*|\bmetric\b", re.IGNORECASE)
_SENTENCE_END: Final = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(\[\d\"'])")
#: What a page reviewer's or code's words add after the AI's own sentence.
_TAILS: Final = (" Read: ", " The reading: ")


def _bare(token: str) -> bool:
    return re.search(r"[\"'/\[]", token) is None


def _mm_token(token: str) -> bool:
    return re.search(r"\bmm\b", token, re.IGNORECASE) is not None and "[" not in token


def _millimetres(tokens: Sequence[str], sentence: str) -> bool:
    """A chain written in millimetres: a value marked mm, or bare numbers only that are too big
    for inches (`ledger.bare_millimetres`) or in a sentence about millimetres."""
    if any(_mm_token(t) for t in tokens):
        return True
    if not all(_bare(t) for t in tokens):
        return False
    return bare_millimetres(list(tokens[:-1]), tokens[-1]) or bool(_METRIC_WORDS.search(sentence))


def _overalls(text: str) -> list[Decimal]:
    """The printed overalls a sentence names (bare millimetres left out)."""
    found: list[Decimal] = []
    metric = bool(_METRIC_WORDS.search(text))
    for match in _OVERALL.finditer(text):
        token = next(g for g in match.groups() if g is not None)
        value = None if _mm_token(token) else _inches(token)
        if value is None or (_bare(token) and (metric or value >= _MM_AT_LEAST)):
            continue  # millimetres: a bare number beside them, or too big for inches
        found.append(value)
    return found


def _text_of(finding: Mapping[str, Any]) -> str:
    """The AI's own words about the finding, longest first, without what code added after them."""
    proof = finding.get("proof")
    mark = finding.get(likely_problems.FIELD)
    texts = [
        finding.get("why"),
        mark.get("why") if isinstance(mark, Mapping) else None,
        (proof.get("likely_problem") or {}).get("reason") if isinstance(proof, Mapping) else None,
    ]
    best = ""
    for raw in texts:
        text = str(raw or "")
        if text.startswith("Code could not confirm") and " The reading: " in text:
            text = text.split(" The reading: ", 1)[1]
        for tail in _TAILS:
            text = text.split(tail, 1)[0]
        text = text.replace("(page review)", "").strip()
        if len(text) > len(best):
            best = text
    return best


def _sentence_claims(text: str) -> list[Claim]:
    """The strings the AI's sentence states ("A + B + C = X, but the overall is Y"), each against
    the printed overall its sentence names (else the one overall the whole text names). Only what
    is plain: a chain in millimetres, beside a multiplier ("2 x 1"", "at each wall end") or with
    no overall named is left out; a chain the AI says adds up to its overall is no claim; a chain
    with no sum of the AI's own counts only when the words say it differs."""
    flat = " ".join(plain_marks(text).split())
    sentences = [s for s in _SENTENCE_END.split(flat) if s]
    chains: list[tuple[str, re.Match[str]]] = [
        (sentence, match) for sentence in sentences for match in _CHAIN.finditer(sentence)
    ]
    if not chains:
        return []
    blanked = flat
    for _sentence, match in chains:
        blanked = blanked.replace(match.group(0), " § ")
    everywhere = set(_overalls(blanked))
    claims: list[Claim] = []
    for sentence, match in chains:
        said_text = match.group("said") or match.group("said2")
        tokens = [m.group(1) for m in _PART.finditer(match.group("parts"))]
        span = match.group(0)
        around = sentence[max(0, match.start() - 12) : match.end() + 12]
        if _millimetres([*tokens, said_text or tokens[-1]], sentence):
            continue
        if (
            _TIMES.search(around)
            or _TIMES_WORDS.search(span)
            or _TIMES_WORDS.search(sentence[match.end() : match.end() + 24])
        ):
            claims.append(
                Claim(
                    "string",
                    "unreadable",
                    f"The AI's sum {_quoted(span)} has a multiplier code does not read.",
                    "sentence",
                )
            )
            continue
        rest = sentence.replace(span, " § ")
        here = set(_overalls(rest))
        overalls = here or everywhere
        said = _inches(said_text) if said_text else None
        if len(overalls) != 1:
            if overalls and said is not None:
                claims.append(
                    Claim(
                        "string",
                        "unreadable",
                        f"The AI's words name more than one overall for {_quoted(span)}.",
                        "sentence",
                    )
                )
            continue
        total = next(iter(overalls))
        if said is not None and said == total:
            continue  # the AI says this string adds up: its claim is elsewhere
        if said is None and not _MISMATCH.search(sentence if here else flat):
            continue
        claim = _string(tokens, format_inches(total), said)
        if claim.result == "same" and (said is None or said == _inches(format_inches(total))):
            # Read from a sentence, "no difference" needs the AI's own sum to be the slip: its
            # parts add up to the overall it names, though it wrote another sum. Else the claim may
            # be about something else the sentence says (a rule, another view).
            claim = Claim(
                "string",
                "unreadable",
                f"{claim.words.rstrip('.')}; the AI wrote no sum of its own, so code cannot tell "
                "what else its words claim.",
            )
        claims.append(Claim(claim.kind, claim.result, claim.words, "sentence"))
    return claims


# ---- one finding ---------------------------------------------------------------------------------


def _identifier(finding: Mapping[str, Any]) -> bool:
    if text_values.identifier(finding):
        return True
    item = " ".join(str(finding.get("item") or "").split())
    return bool(_IDENTIFIER_ITEM.search(item)) and not _SIZE_ITEM.search(item)


def _ai_numbers(finding: Mapping[str, Any]) -> Mapping[str, Any]:
    mark = finding.get(likely_problems.FIELD)
    numbers = mark.get(NUMBERS) if isinstance(mark, Mapping) else None
    return numbers if isinstance(numbers, Mapping) else {}


def _kind_of(finding: Mapping[str, Any]) -> str | None:
    """The likely problem's kind (size, construction...), from its mark or its proof."""
    mark = finding.get(likely_problems.FIELD)
    if isinstance(mark, Mapping) and mark.get("kind"):
        return str(mark["kind"])
    proof = finding.get("proof")
    likely = proof.get("likely_problem") if isinstance(proof, Mapping) else None
    return str(likely["kind"]) if isinstance(likely, Mapping) and likely.get("kind") else None


def claims_of(finding: Mapping[str, Any], rules_md: str | None = None) -> list[Claim]:
    """Every piece of maths the finding states (see the module), fields first; its sentence only
    when it gave no fields."""
    numbers = _ai_numbers(finding)
    claims: list[Claim] = []
    parts = finding.get("parts") or numbers.get("parts")
    total = finding.get("printed_total") or numbers.get("printed_total")
    if isinstance(parts, list) and len(parts) >= 2 and total:
        claims.append(_string(parts, total))
    if numbers.get("vendor_value") is not None and numbers.get("other_value") is not None:
        found = _sizes(numbers["vendor_value"], numbers["other_value"], "the size it should match")
        if found is not None:
            claims.append(found)
    rule = numbers.get("gv_rule")
    if isinstance(rule, Mapping):
        claims.append(_rule(rule, rules_md))
    vendor, other = finding.get("vendor"), finding.get("architect")
    two = finding.get("source") in _TWO_SIZES and not (_absent(vendor) or _absent(other))
    if not claims and two:
        if _identifier(finding):
            if _inches(vendor) is None and _inches(other) is None:
                return []  # words on both sides (a tag): no number code could take for inches
            return [
                Claim(
                    "identifier",
                    "unreadable",
                    "A sheet or revision number, tag, callout or date is text, never a size: code "
                    "does no inch maths on it.",
                )
            ]
        found = _sizes(vendor, other, "the architect's")
        if found is not None:
            claims.append(found)
    if not claims and _kind_of(finding) in (None, "size"):
        claims = _sentence_claims(_text_of(finding))
    return claims


def check(finding: Mapping[str, Any], rules_md: str | None = None) -> dict[str, Any] | None:
    """The maths record for one finding (`FIELD`), or `None` when it states no maths."""
    claims = claims_of(finding, rules_md)
    if not claims:
        return None
    results = {c.result for c in claims}
    result: Result = (
        "differs" if "differs" in results else "same" if results == {"same"} else "unreadable"
    )
    shown = [c for c in claims if c.result == result] or claims
    return {
        "result": result,
        "words": " ".join(c.words for c in shown)[:600],
        "kind": shown[0].kind,
        "read_from": "fields" if any(c.read_from == "fields" for c in shown) else "sentence",
    }


def _left_alone(finding: Mapping[str, Any]) -> bool:
    """A finding the maths leaves as it is: the reviewer decided it (the reviewer is its witness),
    or code compared both printed values itself and found them the same (code's Right)."""
    decision = finding.get("decision")
    if isinstance(decision, Mapping) and decision.get("decision"):
        return True
    return finding.get("status") == "same" and finding.get("compared") is True


def mark_answer(
    live: Sequence[dict[str, Any]],
    result: Any,
    rules_md: str | None = None,
    *,
    stop: Callable[[], bool] | None = None,
) -> Counter[str]:
    """Code's maths on every live finding of an answer (the escalation's last marks): each record
    that changed is written on the finding and listed in `result.changed`; folded findings
    (`result.absorbed`) and those left alone (`_left_alone`) are skipped. After a Stop only the
    findings already changed get it (a Continue marks the rest). Returns the counts, logged."""
    counts: Counter[str] = Counter()
    absorbed = getattr(result, "absorbed", {}) or {}
    tried = {str(f.get("id")) for f in result.changed} if stop is not None and stop() else None
    for finding in live:
        finding_id = str(finding.get("id"))
        if finding_id in absorbed or _left_alone(finding):
            continue
        if tried is not None and finding_id not in tried:
            continue
        try:
            record = check(finding, rules_md)
        except Exception:  # noqa: BLE001 - no record: the finding stays as the AI flagged it
            _log.exception("ai maths: failed on %s", finding.get("id"))
            continue
        counts[record["result"] if record is not None else "none"] += 1
        if record is not None and record["result"] == "same" and finding.get("status") != "same":
            counts["no_difference"] += 1  # the AI said a problem; its own numbers show none
        if finding.get(FIELD) == record:
            continue
        if record is None:
            finding.pop(FIELD, None)
        else:
            finding[FIELD] = record
        result.changed.append(finding)
    _log.info(
        "ai maths: %d differs, %d same (%d of them claimed problems the AI's own numbers do not "
        "show), %d unreadable, %d with no maths",
        counts["differs"],
        counts["same"],
        counts["no_difference"],
        counts["unreadable"],
        counts["none"],
    )
    return counts
