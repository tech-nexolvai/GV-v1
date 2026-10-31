"""What GV's reviewer's Agree / Not a problem clicks teach the page reviewer (plan "Why not all 50",
Phase 4, row M13c; Anant 2026-11-01 "build what's left").

**What the app already stores.** Every ✓ Agree / ✗ Not a problem click on a finding is one row of
the append-only `assistant_finding_decisions` table (#1521, migrations 0108 and 0112: chat, answer,
finding id, `agree` / `not_a_problem` / `undo`, an optional note and "should be", who and when);
the newest row per (answer, finding) counts (`chat_decisions.latest_decisions_of`). The card
itself is in its answer's stored blocks, at its newest correction (`chat_corrections`), and the
chat's drawing set names its project. Nothing new needs storing: this module only reads.

**1. The learned list: proposed, never applied by itself.** Each please-check card has a TYPE: a
stable key from its kind letter and its normalised words (`type_key`, e.g. "D: metal finish tag
missing", "B: support missing", "E: bin who supplies missing"). Across projects code counts, per
type, how often GV's reviewer accepted it and how often dismissed it (`tally`). A type dismissed at
least `MIN_DISMISSED` times and never accepted becomes a PROPOSED suppression (`proposals`), as
Greptile suppresses a comment type ignored 3+ times. A proposal does nothing: only a type Anant
APPROVED (`approve`, a person on the local confirmers list; the decision log: suppression rules need
Anant's approval, learning is deliberate and human-checked, never automatic) applies. Then the page
reviewer is told not to raise it (`reviewer_blocks`) and any please-check card of that type left at
the end of a full check is dropped with its reason (`drop_suppressed`).

Never suppressible (`NeverSuppress`, and `learned_never_suppress.json`): a red or anything code
proved (only an open please-check, `is_please_check`, is ever typed for suppression); sizes (kind A:
the client settled exact match and "flag everything"); cards made by code's own checks of the
client's rules; and types whose words name one of the rulebook's settled rules (fillers, sink,
cut-out, offsets, depth, field cut...).

**2. The top-1 past wording.** An ACCEPTED card is kept as an example: (type, item kind, view kind,
the final words: its own, with the reviewer's "should be" when given). The page reviewer's first
message gets, per type present on its page, the single best past wording (top-1: RARe found more
examples hurt; retrieval shapes wording, not recall), never from the job being checked
(`same_project`: its project, or a project named in an upload's name), so a test never sees its own
answers. Wording only: it never makes or keeps an issue.

**Where the files are.** A local folder named by `GV_ASSISTANT_LEARNED_DIR` (unset: all off),
outside any repository, because the examples hold GV's words about client drawings:
`examples.json` and `proposals.json` (made by `scripts/learned_list.py mine` from a database, on a
slow cadence like Qodo's rule miner), `approved.json` (written only by `scripts/learned_list.py
approve` / `revoke`, append-only events, the newest per type counts). Only decisions by names on the
local real-reviewer list count (Smriti's rule, decided 2026-10-15).

Code, $0, no AI call. Verification: `tests/review/assistant/test_learned.py` (made-up data).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from app.review.assistant.untrusted import fence

_log = logging.getLogger(__name__)

__all__ = [
    "ENV_DIR",
    "MIN_DISMISSED",
    "DecidedCard",
    "Example",
    "Learned",
    "LearnedRefused",
    "NeverSuppress",
    "TypeCount",
    "approve",
    "approved_types",
    "cards_of_answer",
    "current",
    "drop_suppressed",
    "examples_of",
    "files_of",
    "is_please_check",
    "item_kind",
    "letter_of",
    "load_cards",
    "load_never",
    "proposals",
    "read_folder",
    "reviewer_blocks",
    "same_project",
    "tally",
    "top_example",
    "type_key",
    "view_kind",
]

#: The local folder of the learned files (unset or empty: learning is off).
ENV_DIR: Final = "GV_ASSISTANT_LEARNED_DIR"
EXAMPLES_FILE: Final = "examples.json"
PROPOSALS_FILE: Final = "proposals.json"
APPROVED_FILE: Final = "approved.json"
EXAMPLES_FORMAT: Final = "gv-learned-examples/1"
PROPOSALS_FORMAT: Final = "gv-learned-proposals/1"
APPROVED_FORMAT: Final = "gv-learned-approved/1"
NEVER_FORMAT: Final = "gv-learned-never-suppress/1"
NEVER_FILE: Final = Path(__file__).with_name("learned_never_suppress.json")

#: A type dismissed at least this many times (by real reviewers) and never accepted is proposed
#: (Greptile: a comment type ignored 3+ times).
MIN_DISMISSED: Final = 3
#: Past wordings in one page's first message, at most (one per type).
MAX_EXAMPLES: Final = 6
#: Approved types listed to the reviewer, at most.
MAX_LISTED_SUPPRESSED: Final = 20
#: An example's words, at most (characters).
WORDS_CHARS: Final = 300

AGREE: Final = "agree"
DISMISS: Final = "not_a_problem"

#: The finding's field the please-check mark lives in (`likely_problems.FIELD`).
_MARK: Final = "whole_look"
#: The card lead every please-check's words start with (`likely_problems.LETTER_LEAD`).
_LEAD: Final = "Please check and confirm"

#: A kind in words (the page reviewer's and the whole-drawing look's) -> its letter.
LETTER_OF_KIND: Final[Mapping[str, str]] = {
    "size": "A",
    "construction": "B",
    "completeness": "C",
    "missing_piece": "C",
    "extra_piece": "C",
    "count": "C",
    "layout": "C",
    "finish": "D",
    "scope": "E",
    "labels": "F",
}
_LETTERS: Final = frozenset("ABCDEF")


class LearnedRefused(ValueError):
    """An approval or a file the learned list refuses (the message says why, in plain words)."""


# ---- a card's type -------------------------------------------------------------------------------


def _parts(*pairs: tuple[str, str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((name, re.compile(rf"\b(?:{pattern})\b")) for name, pattern in pairs)


#: The parts a card is about, most specific first (matched on lower-case words).
PARTS: Final = _parts(
    ("side splash", r"side\s*-?\s*splash(?:es)?"),
    ("splash", r"(?:back\s*-?\s*)?splash(?:es)?|backsplash(?:es)?"),
    ("toe kick", r"toe\s*-?\s*kicks?"),
    ("end panel", r"end\s*panels?|finished\s*ends?|side\s*panels?"),
    ("filler", r"fillers?|scribes?"),
    ("grain", r"grain(?:\s*(?:direction|arrows?))?"),
    ("support", r"supports?|legs?|brackets?|corbels?"),
    ("seam", r"seams?|joints?"),
    ("cut-out", r"cut\s*-?\s*outs?|cutouts?"),
    ("sink", r"sinks?"),
    ("faucet", r"faucets?"),
    ("grommet", r"grommets?"),
    ("outlet", r"outlets?|receptacles?"),
    ("appliance", r"appliances?|fridge|refrigerators?|dishwashers?|microwaves?|cooktops?"),
    ("bin", r"bins?|trash|recycling"),
    ("blocking", r"blocking|backing"),
    ("valance", r"valances?"),
    ("shelf", r"shelf|shelves"),
    ("drawer", r"drawers?"),
    ("door", r"doors?"),
    ("item number", r"item\s*(?:cells?|numbers?|no\.?|#)"),
    ("description", r"descriptions?"),
    ("date", r"dates?"),
    ("title block", r"title\s*blocks?"),
    ("material table", r"(?:material|finish)\s*(?:tables?|legends?|schedules?|lists?)"),
    ("view title", r"view\s*titles?"),
    ("section marker", r"(?:section|detail)\s*(?:markers?|bubbles?|callouts?)"),
    ("leader", r"leaders?|pointers?"),
    ("panel", r"panels?"),
    ("top", r"countertops?|counter\s*-?\s*tops?|(?:stone\s*|quartz\s*)?tops?"),
    ("height", r"heights?"),
    ("depth", r"depths?"),
    ("width", r"widths?|lengths?"),
    ("dimension", r"dimensions?|dims?"),
)
_PART_PATTERN: Final[Mapping[str, re.Pattern[str]]] = dict(PARTS)

#: A finish's material, from its words or its tag family (MT-01 is metal).
_MATERIALS: Final = _parts(
    ("metal", r"metal|steel|stainless|aluminu?m|brass|bronze|mt-?\d+\w*"),
    ("stone", r"stone|quartz|granite|marble|st-?\d+\w*|qz-?\d+\w*"),
    ("wood", r"wood|veneer|walnut|oak|maple|wd-?\d+\w*|wv-?\d+\w*"),
    ("laminate", r"laminate|plam|plam-?\d+\w*|pl-?\d+\w*|la-?\d+\w*"),
)
_MATERIAL_PATTERN: Final[Mapping[str, re.Pattern[str]]] = dict(_MATERIALS)
#: What is wrong, in three words; the first that matches (in this order) names it.
_PROBLEMS: Final = _parts(
    ("not wanted", r"remove|delete|extra|stray|duplicated?|not needed|unnecessary|should not"),
    (
        "missing",
        (
            r"missing|not shown|not tagged|untagged|no|none|without|lacks?|absent|blank|left out|"
            r"omit\w*|not called out|not dimensioned|not given|not said|not stated|does not say|"
            r"provide|show|add"
        ),
    ),
    (
        "differs",
        (
            r"differs?|different|mismatch\w*|disagree\w*|inconsistent|conflict\w*|instead of|"
            r"wrong|only this sheet|does not match|do not match|not the same|other drawing|"
            r"other sheets?"
        ),
    ),
)
_TAG_WORDS: Final = re.compile(r"\b(?:tags?|tagged|untagged|finish codes?|codes?)\b")
#: A printed tag or item number (`ST-03`, `MT-001.C`, `BOH-801T`): never part of a type.
_TAG: Final = re.compile(r"\b[a-z]{1,5}-?\d{1,4}[a-z]?(?:\.[a-z]{1,3})?\b")
#: Where a card is ("at the sink wall", "in the front view"): never part of a type.
_PLACE: Final = re.compile(
    r"\s(?:at|near|beside|next to|along|in the|on the|over the|above the|below the)\s[^:,;.]*"
)
_STOP: Final = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "on",
        "in",
        "at",
        "to",
        "for",
        "is",
        "are",
        "be",
        "it",
        "its",
        "this",
        "that",
        "with",
        "by",
        "as",
        "from",
        "not",
        "has",
        "have",
        "page",
        "sheet",
        "view",
        "please",
        "check",
        "confirm",
        "should",
    }
)


def _norm(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def _first(pairs: Sequence[tuple[str, re.Pattern[str]]], text: str) -> str | None:
    return next((name for name, pattern in pairs if pattern.search(text)), None)


def _mark(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    mark = finding.get(_MARK)
    return mark if isinstance(mark, Mapping) else None


def letter_of(finding: Mapping[str, Any]) -> str | None:
    """The card's kind letter (A sizes ... F labels), or `None` when it has no please-check mark."""
    mark = _mark(finding)
    if mark is None:
        return None
    letter = str(mark.get("letter") or "").strip().upper()
    if letter in _LETTERS:
        return letter
    return LETTER_OF_KIND.get(str(mark.get("kind") or "").strip().lower())


def _why(finding: Mapping[str, Any]) -> str:
    mark = _mark(finding) or {}
    return str(mark.get("why") or finding.get("why") or "")


def topic(letter: str, item: object, wrong: object) -> str:
    """The type's words: the part (else, on a finish card, the material), "finish tag", "who
    supplies" (scope) and what is wrong (missing / not wanted / differs); printed tags, numbers
    and places never."""
    raw_item, raw_wrong = _norm(item), _norm(wrong)
    both = f"{raw_item} {raw_wrong}"
    material = _first(_MATERIALS, both) if letter == "D" else None
    item_words = _PLACE.sub(" ", _TAG.sub(" ", raw_item))
    wrong_words = _PLACE.sub(" ", _TAG.sub(" ", raw_wrong))
    plain = f"{item_words} {wrong_words}"
    part = _first(PARTS, item_words) or _first(PARTS, wrong_words)
    # The material names a finish card only when no part does (a toe kick's tag is one type
    # whatever wood or laminate it is).
    words: list[str] = [part] if part else ([material] if material else [])
    if letter == "D" and part != "grain" and _TAG_WORDS.search(both):
        words.append("finish tag")
    if not words:
        content = [
            w
            for w in re.findall(r"[a-z][a-z'-]+", item_words)
            if len(w) > 2 and w not in _STOP and not any(c.isdigit() for c in w)
        ]
        words += content[:3]
    if letter == "E":
        words.append("who supplies")
    problem = _first(_PROBLEMS, plain)
    if problem:
        words.append(problem)
    return " ".join(words) or "other"


def type_key(finding: Mapping[str, Any]) -> str | None:
    """A please-check card's type, e.g. "D: metal finish tag missing"; `None` for a card with no
    please-check mark (a red, a code item, an unsized-ends note)."""
    letter = letter_of(finding)
    if letter is None:
        return None
    return f"{letter}: {topic(letter, finding.get('item'), _why(finding))}"


def is_please_check(finding: Mapping[str, Any]) -> bool:
    """An open please-check: never red, never code-proved (a Likely problem whose proof is
    "likely only", status check, not verified, not claimed wrong)."""
    mark = _mark(finding)
    return (
        mark is not None
        and finding.get("status") == "check"
        and mark.get("proof") == "likely_only"
        and mark.get("claim") != "wrong"
        and finding.get("verified") is not True
    )


def item_kind(text: object) -> str:
    """cabinet, countertop, vanity or other, from words."""
    words = _norm(text)
    if re.search(r"\bvanit(?:y|ies)\b", words):
        return "vanity"
    if re.search(
        r"\b(?:countertops?|counter\s*-?\s*tops?|stone|quartz|granite|tops?|splash\w*|"
        r"cut\s*-?\s*outs?|cutouts?|sinks?)\b",
        words,
    ):
        return "countertop"
    if re.search(
        r"\b(?:cabinets?|base|drawers?|doors?|toe\s*-?\s*kicks?|fillers?|end\s*panels?|"
        r"shel(?:f|ves))\b",
        words,
    ):
        return "cabinet"
    return "other"


_VIEWS: Final = _parts(
    ("title block", r"title\s*blocks?|item\s*cells?"),
    ("material table", r"(?:material|finish)\s*(?:tables?|legends?|schedules?)"),
    ("section", r"sections?|sect\.?"),
    ("elevation", r"elevations?|elev\.?|front\s*views?"),
    ("plan", r"plans?|top\s*views?"),
    ("detail", r"details?"),
)


def view_kind(text: object) -> str:
    """The view a card is about (title block, material table, section, elevation, plan, detail),
    else "any"."""
    return _first(_VIEWS, _norm(text)) or "any"


# ---- the never-suppress list ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettledRule:
    id: str
    name: str
    words: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NeverSuppress:
    """What may never be suppressed (`learned_never_suppress.json`)."""

    letters: Mapping[str, str]
    makers: Mapping[str, str]
    rules: tuple[SettledRule, ...]

    def why(self, key: str, maker: str | None = None) -> str | None:
        """Why this type (made by `maker`) can never be suppressed, or `None` when it can."""
        letter, _, words = key.partition(": ")
        if letter in self.letters:
            return f"kind {letter}: {self.letters[letter]}"
        if maker and maker in self.makers:
            return f"made by {self.makers[maker]}"
        for rule in self.rules:
            for word in rule.words:
                if re.search(rf"(?<![a-z-]){re.escape(word.lower())}(?![a-z-])", words.lower()):
                    return f"tied to GV's settled rule {rule.id} ({rule.name})"
        return None


def load_never(path: Path = NEVER_FILE) -> NeverSuppress:
    """The never-suppress list; raises `LearnedRefused` when it is not in its format (nothing is
    suppressed then: `current` keeps the approvals off)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LearnedRefused(f"the never-suppress list could not be read: {error}") from None
    if not isinstance(raw, Mapping) or raw.get("format") != NEVER_FORMAT:
        raise LearnedRefused(f"the never-suppress list is not {NEVER_FORMAT}")
    rules: list[SettledRule] = []
    for entry in raw.get("rules") or []:
        words = tuple(str(w).strip() for w in entry.get("words") or [] if str(w).strip())
        if not entry.get("id") or not words:
            raise LearnedRefused("each settled rule needs an id and its words")
        rules.append(SettledRule(str(entry["id"]), str(entry.get("name") or ""), words))
    letters, makers = raw.get("letters") or {}, raw.get("makers") or {}
    if not isinstance(letters, Mapping) or not isinstance(makers, Mapping):
        raise LearnedRefused("letters and makers must be objects")
    return NeverSuppress(
        letters={str(k): str(v) for k, v in letters.items()},
        makers={str(k): str(v) for k, v in makers.items()},
        rules=tuple(rules),
    )


# ---- decided cards, counts and proposals ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecidedCard:
    """One please-check card GV's reviewer decided."""

    type: str
    letter: str
    decision: str
    """agree or not_a_problem."""
    project: str
    by: str
    at: str
    words: str
    """Its final words (the card's, with the reviewer's "should be" when given)."""
    item_kind: str
    view_kind: str
    maker: str


def _final_words(finding: Mapping[str, Any], decision: Mapping[str, Any]) -> str:
    mark = _mark(finding) or {}
    words = " ".join(str(mark.get("words") or "").split())
    if not words:
        item = " ".join(str(finding.get("item") or "").split())
        words = f"{item}: {' '.join(_why(finding).split())}".strip(": ")
    if words.lower().startswith(_LEAD.lower()):
        words = words[len(_LEAD) :].lstrip(" :")
    should_be = " ".join(str(decision.get("should_be") or "").split())
    if should_be:
        words = re.split(r"\s*It should be:", words, maxsplit=1)[0].rstrip(" .")
        words = f"{words}. It should be: {should_be}."
    return words[:WORDS_CHARS]


def cards_of_answer(
    project: str,
    findings: Iterable[Mapping[str, Any]],
    decided: Mapping[str, Mapping[str, Any]],
) -> list[DecidedCard]:
    """The please-check cards of one stored answer that have a latest Agree / Not a problem
    (`decided`: finding id -> `chat_decisions.decision_out`). A red, a code item or an undecided
    card is left out."""
    out: list[DecidedCard] = []
    for finding in findings:
        decision = decided.get(str(finding.get("id")))
        if not decision or decision.get("decision") not in (AGREE, DISMISS):
            continue
        if not is_please_check(finding):
            continue
        key, letter = type_key(finding), letter_of(finding)
        if key is None or letter is None:
            continue
        mark = _mark(finding) or {}
        about = f"{finding.get('item') or ''} {_why(finding)}"
        at = decision.get("at")
        out.append(
            DecidedCard(
                type=key,
                letter=letter,
                decision=str(decision["decision"]),
                project=" ".join(str(project or "").split()),
                by=str(decision.get("by") or ""),
                at=at.isoformat() if isinstance(at, datetime) else str(at or ""),
                words=_final_words(finding, decision),
                item_kind=item_kind(about),
                view_kind=view_kind(about),
                maker=str(mark.get("by") or finding.get("created_by") or ""),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class TypeCount:
    """One type's counts across projects."""

    type: str
    accepted: int
    dismissed: int
    projects: int
    """Projects it was dismissed on."""
    makers: tuple[str, ...]
    protected: str | None
    """Why it can never be suppressed, or `None`."""

    @property
    def status(self) -> str:
        """proposed, protected (would be proposed but may never be), or watch."""
        if self.dismissed >= MIN_DISMISSED and self.accepted == 0:
            return "protected" if self.protected else "proposed"
        return "watch"


def tally(
    cards: Iterable[DecidedCard],
    never: NeverSuppress,
    counts: Callable[[str], bool] = lambda _by: True,
) -> tuple[list[TypeCount], int]:
    """Per type: accepted and dismissed clicks (each card once), the projects it was dismissed on,
    and whether it is protected; only clicks by a name `counts` keeps (the real-reviewer list).
    Also returns how many clicks were not counted."""
    accepted: Counter[str] = Counter()
    dismissed: Counter[str] = Counter()
    projects: dict[str, set[str]] = defaultdict(set)
    makers: dict[str, set[str]] = defaultdict(set)
    skipped = 0
    for card in cards:
        if not counts(card.by):
            skipped += 1
            continue
        makers[card.type].add(card.maker)
        if card.decision == AGREE:
            accepted[card.type] += 1
        else:
            dismissed[card.type] += 1
            projects[card.type].add(card.project.lower())
    out = []
    for key in sorted(set(accepted) | set(dismissed)):
        made_by = tuple(sorted(m for m in makers[key] if m))
        protected = next(
            (why for why in (never.why(key, m) for m in (*made_by, None)) if why), None
        )
        out.append(
            TypeCount(
                type=key,
                accepted=accepted[key],
                dismissed=dismissed[key],
                projects=len(projects[key]),
                makers=made_by,
                protected=protected,
            )
        )
    return out, skipped


def proposals(counts: Sequence[TypeCount]) -> list[dict[str, Any]]:
    """The proposals file's rows: every counted type, proposed ones first (most dismissed first)."""
    order = {"proposed": 0, "protected": 1, "watch": 2}
    rows = sorted(counts, key=lambda c: (order[c.status], -c.dismissed, c.type))
    return [
        {
            "type": c.type,
            "status": c.status,
            "accepted": c.accepted,
            "dismissed": c.dismissed,
            "projects": c.projects,
            "makers": list(c.makers),
            "never_suppress": c.protected,
        }
        for c in rows
    ]


# ---- approvals -----------------------------------------------------------------------------------


def approved_types(events: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """The types whose newest event is an approval."""
    latest: dict[str, str] = {}
    for event in events:
        key = str(event.get("type") or "")
        if key:
            latest[key] = str(event.get("action") or "")
    return frozenset(key for key, action in latest.items() if action == "approve")


def approve(
    events: Sequence[Mapping[str, Any]],
    key: str,
    *,
    by: str,
    proposed: Sequence[Mapping[str, Any]],
    never: NeverSuppress,
    action: str = "approve",
    note: str = "",
    at: datetime | None = None,
) -> list[dict[str, Any]]:
    """The approval events with one more: Anant approves (or revokes) a proposed type. Refused
    (`LearnedRefused`) for a type that is not proposed now, or that is protected; a revoke is
    always allowed for an approved type. The caller checks `by` is on the confirmers list."""
    if action not in ("approve", "revoke"):
        raise LearnedRefused("the action is approve or revoke")
    if not by.strip():
        raise LearnedRefused("say who approves")
    if action == "revoke":
        if key not in approved_types(events):
            raise LearnedRefused(f"{key!r} is not approved")
    else:
        row = next((r for r in proposed if r.get("type") == key), None)
        if row is None or row.get("status") != "proposed":
            raise LearnedRefused(
                f"{key!r} is not proposed now (status: "
                f"{row.get('status') if row else 'not in the proposals'})"
            )
        why = never.why(key)
        if why is not None:
            raise LearnedRefused(f"{key!r} can never be suppressed: {why}")
    when = (at or datetime.now(UTC)).isoformat()
    event = {"type": key, "action": action, "by": by.strip(), "at": when, "note": note.strip()}
    return [*map(dict, events), event]


# ---- examples (top-1 past wording) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Example:
    """One accepted card's wording."""

    type: str
    part: str
    item_kind: str
    view_kind: str
    words: str
    project: str
    at: str


def _part_of(key: str) -> str:
    words = key.partition(": ")[2]
    return next((name for name, _ in PARTS if re.search(rf"\b{re.escape(name)}\b", words)), "")


def examples_of(
    cards: Iterable[DecidedCard], counts: Callable[[str], bool] = lambda _by: True
) -> list[Example]:
    """The accepted cards (by names `counts` keeps), as examples."""
    return [
        Example(
            type=c.type,
            part=_part_of(c.type),
            item_kind=c.item_kind,
            view_kind=c.view_kind,
            words=c.words,
            project=c.project,
            at=c.at,
        )
        for c in cards
        if c.decision == AGREE and counts(c.by) and c.words
    ]


def _fold(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def same_project(example_project: str, project: str, uploads: Sequence[str] = ()) -> bool:
    """The example may come from the job being checked: the same project (either name inside the
    other), a project named in an upload's name, or a project with no name (never proved
    different)."""
    mine, theirs = _fold(project), _fold(example_project)
    if not theirs:
        return True
    if mine and (theirs in mine or mine in theirs):
        return True
    return any(theirs in _fold(name) for name in uploads)


def top_example(
    examples: Sequence[Example],
    key: str,
    *,
    project: str,
    uploads: Sequence[str] = (),
    item: str | None = None,
    views: Collection[str] = (),
) -> Example | None:
    """The single best past wording of `key` from another project: the same item kind, then a
    view this page has, then the wording accepted most often, then the newest."""
    pool = [e for e in examples if e.type == key and not same_project(e.project, project, uploads)]
    if not pool:
        return None
    used = Counter(_fold(e.words) for e in pool)
    return max(
        pool,
        key=lambda e: (
            item is not None and e.item_kind == item,
            e.view_kind in views,
            used[_fold(e.words)],
            e.at,
        ),
    )


# ---- what the app reads at check time ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Learned:
    """The approved types and the examples, as read from the learned folder."""

    approved: frozenset[str] = frozenset()
    examples: tuple[Example, ...] = ()
    never: NeverSuppress | None = None
    """`None`: the never-suppress list could not be read, so nothing is suppressed."""

    def suppressed(self, finding: Mapping[str, Any]) -> str | None:
        """Why this card is dropped (an approved type, an open please-check, not protected), or
        `None` when it stays."""
        if self.never is None or not self.approved or not is_please_check(finding):
            return None
        key = type_key(finding)
        if key is None or key not in self.approved:
            return None
        mark = _mark(finding) or {}
        maker = str(mark.get("by") or finding.get("created_by") or "")
        if self.never.why(key, maker) is not None:
            return None
        return (
            f"GV's reviewer dismissed this kind of please-check ({key}) again and again, and "
            "Anant approved not raising it (learned list)"
        )

    def page_examples(
        self,
        texts: Sequence[str],
        *,
        project: str,
        uploads: Sequence[str] = (),
        views: Collection[str] = (),
        item: str | None = None,
        limit: int = MAX_EXAMPLES,
    ) -> list[Example]:
        """One example per type whose part this page prints (its text or code's questions),
        types accepted most often first, never an approved (suppressed) type."""
        page = _norm(" ".join(texts))
        accepted = Counter(e.type for e in self.examples)
        out: list[Example] = []
        for key, _n in sorted(accepted.items(), key=lambda kv: (-kv[1], kv[0])):
            if len(out) >= limit:
                break
            if key in self.approved:
                continue
            part = next((e.part for e in self.examples if e.type == key), "")
            # A type with no part (a finish card named by its material) is present when the
            # page prints that material.
            material = key.partition(": ")[2].split(" ", 1)[0]
            pattern = _PART_PATTERN.get(part) or _MATERIAL_PATTERN.get(material)
            if pattern is None or not pattern.search(page):
                continue
            best = top_example(
                self.examples, key, project=project, uploads=uploads, item=item, views=views
            )
            if best is not None:
                out.append(best)
        return out


def _read(path: Path, fmt: str) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("format") != fmt:
        raise LearnedRefused(f"{path.name} is not {fmt}")
    return raw


def read_folder(folder: Path) -> Learned:
    """The learned folder's approvals and examples. A damaged approvals file or never-suppress list
    turns suppression off; damaged examples give none (each logged)."""
    try:
        never: NeverSuppress | None = load_never()
    except LearnedRefused:
        _log.warning("learned list: the never-suppress list is damaged; nothing is suppressed")
        never = None
    approved: frozenset[str] = frozenset()
    try:
        raw = _read(folder / APPROVED_FILE, APPROVED_FORMAT)
        if raw is not None:
            approved = approved_types(e for e in raw.get("events") or [] if isinstance(e, Mapping))
    except (OSError, ValueError):
        _log.warning("learned list: %s could not be read; nothing is suppressed", APPROVED_FILE)
        approved = frozenset()
    examples: tuple[Example, ...] = ()
    try:
        raw = _read(folder / EXAMPLES_FILE, EXAMPLES_FORMAT)
        if raw is not None:
            examples = tuple(
                Example(**{k: str(e.get(k) or "") for k in Example.__dataclass_fields__})
                for e in raw.get("examples") or []
                if isinstance(e, Mapping)
            )
    except (OSError, ValueError, TypeError):
        _log.warning("learned list: %s could not be read; no past wording", EXAMPLES_FILE)
    return Learned(approved=approved, examples=examples, never=never)


_CACHE: dict[tuple[str, tuple[int, ...]], Learned] = {}
_CACHE_LOCK = threading.Lock()


def current() -> Learned | None:
    """The learned folder `GV_ASSISTANT_LEARNED_DIR` names, read once per change of its files;
    `None` when the setting is unset (learning off)."""
    configured = os.environ.get(ENV_DIR, "").strip()
    if not configured:
        return None
    folder = Path(configured).expanduser()
    stamps = []
    for name in (APPROVED_FILE, EXAMPLES_FILE):
        try:
            stamps.append((folder / name).stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    key = (str(folder), tuple(stamps))
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE.clear()
            _CACHE[key] = read_folder(folder)
        return _CACHE[key]


# ---- the two hooks -------------------------------------------------------------------------------


def _views_on(texts: Sequence[str]) -> set[str]:
    page = _norm(" ".join(texts))
    return {name for name, pattern in _VIEWS if pattern.search(page)}


def reviewer_blocks(run: Any, learned: Learned | None = None) -> list[dict[str, Any]]:
    """The page reviewer's first-message blocks (`page_reviewer._Run`): the top-1 past wording per
    type on this page, and the approved types it must not raise. Nothing when learning is off or
    anything fails (the review goes on)."""
    try:
        learned = learned if learned is not None else current()
        if learned is None:
            return []
        review, engine = run.review, run.engine
        texts = [str(p.get("text") or "") for p in getattr(review, "found", None) or []]
        texts += [str(getattr(q, "text", "")) for q in getattr(review, "questions", None) or []]
        identity = getattr(getattr(engine, "settings", None), "identity", None)
        project = str(getattr(identity, "project", "") or "")
        try:
            from app.review.assistant.page_reviewer import _upload_names

            uploads = _upload_names(engine)
        except Exception:  # noqa: BLE001 - no upload names: the project name alone excludes
            uploads = []
        page = " ".join(texts)
        found = learned.page_examples(
            texts,
            project=project,
            uploads=uploads,
            views=_views_on(texts),
            item=item_kind(page),
        )
        blocks: list[dict[str, Any]] = []
        if found:
            lines = "\n".join(
                f'- {e.type} ({e.item_kind}, {e.view_kind}): "{e.words}"' for e in found
            )
            blocks.append(
                {
                    "type": "text",
                    "text": "How GV's reviewer worded accepted please-checks of the kinds this "
                    "page has, on other projects (one each). Use them for wording only: report an "
                    "issue only where this page shows it:\n" + fence(lines, "learned_examples"),
                }
            )
        never_listed = sorted(learned.approved)[:MAX_LISTED_SUPPRESSED]
        if never_listed and learned.never is not None:
            blocks.append(
                {
                    "type": "text",
                    "text": "Please-check kinds GV's reviewer dismissed again and again, which "
                    "Anant approved not raising: do not report these (a size or a wrong value is "
                    "never one of them):\n" + fence("\n".join(never_listed), "learned_suppressed"),
                }
            )
        return blocks
    except Exception:  # noqa: BLE001 - learning is advisory: the review goes on without it
        _log.exception("learned list: the page reviewer's blocks were not made")
        return []


def drop_suppressed(engine: Any, learned: Learned | None = None) -> int:
    """A full check's last code step: each open please-check card of an approved type is dropped
    with its reason (shown as dropped). Never a red, a code-proved item or a protected type.
    Returns how many; never raises."""
    try:
        if getattr(engine.settings, "mode", None) != "full_check":
            return 0
        learned = learned if learned is not None else current()
        if learned is None or not learned.approved:
            return 0
        dropped = 0
        with engine.finding_lock:
            for finding in engine.book.live():
                reason = learned.suppressed(finding)
                if reason is None:
                    continue
                finding_id = str(finding.get("id"))
                try:
                    engine.book.drop({"finding_id": finding_id, "reason": reason[:300]})
                except Exception:  # noqa: BLE001 - already gone: nothing to drop
                    _log.debug("learned list: card %s was already gone", finding_id)
                    continue
                engine._show_dropped(finding_id, reason)
                dropped += 1
        if dropped:
            _log.warning("learned list: %d please-check card(s) of approved types dropped", dropped)
        return dropped
    except Exception:  # noqa: BLE001 - the cards stay as they are
        _log.exception("learned list: approved types were not applied")
        return 0


# ---- reading the database (for `scripts/learned_list.py mine`) -----------------------------------


def load_cards(session: Any) -> list[DecidedCard]:
    """Every decided please-check card in the database, at its newest correction and its latest
    decision, with its drawing set's project name. Read only."""
    from sqlalchemy import select

    from app.models import (
        AssistantChat,
        AssistantChatMessage,
        AssistantFindingDecision,
        Package,
        Project,
    )
    from app.review.assistant.chat_corrections import corrections_by_answer, with_corrections
    from app.review.assistant.chat_decisions import latest_decisions_of
    from app.review.assistant.chat_findings import live_findings

    message_ids = list(session.scalars(select(AssistantFindingDecision.message_id).distinct()))
    latest = latest_decisions_of(session, message_ids)
    if not latest:
        return []
    rows = list(
        session.execute(
            select(AssistantChatMessage, Project.name)
            .join(AssistantChat, AssistantChat.id == AssistantChatMessage.chat_id)
            .join(Package, Package.id == AssistantChat.package_id)
            .join(Project, Project.id == Package.project_id)
            .where(AssistantChatMessage.id.in_(list(latest)))
        )
    )
    chats = {message.chat_id for message, _name in rows}
    later = list(
        session.scalars(
            select(AssistantChatMessage)
            .where(
                AssistantChatMessage.chat_id.in_(list(chats)),
                AssistantChatMessage.role == "assistant",
            )
            .order_by(AssistantChatMessage.chat_id, AssistantChatMessage.seq)
        )
    )
    corrections = corrections_by_answer(later)
    cards: list[DecidedCard] = []
    for message, name in rows:
        blocks = with_corrections(message.blocks, corrections.get(str(message.id)))
        cards += cards_of_answer(str(name or ""), live_findings(blocks), latest[message.id])
    return cards


def files_of(
    cards: Sequence[DecidedCard],
    never: NeverSuppress,
    counts: Callable[[str], bool],
    approved: frozenset[str] = frozenset(),
    at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The proposals file and the examples file made from the decided cards (`mine`). An approved
    type that has since been accepted is listed under `stale_approvals` for Anant to look at
    (never revoked by code)."""
    when = (at or datetime.now(UTC)).isoformat()
    counted, skipped = tally(cards, never, counts)
    rows = proposals(counted)
    accepted_now = {c.type for c in counted if c.accepted}
    proposals_file = {
        "format": PROPOSALS_FORMAT,
        "made_at": when,
        "min_dismissed": MIN_DISMISSED,
        "decisions_counted": len(cards) - skipped,
        "decisions_not_counted": skipped,
        "types": rows,
        "stale_approvals": sorted(approved & accepted_now),
    }
    examples_file = {
        "format": EXAMPLES_FORMAT,
        "made_at": when,
        "examples": [asdict(e) for e in examples_of(cards, counts)],
    }
    return proposals_file, examples_file
