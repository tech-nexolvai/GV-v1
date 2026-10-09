"""The deterministic check every assistant answer passes before a reviewer sees it (#1128).

**The model never writes a number, an outcome or a decision. Code writes all of them.** An answer is
a template: every fact is a placeholder (`{C1.outcome}`, `{C1.printed}`, `{count.needs_you}`,
`{signoff.status}`, see `placeholders.py`) that code fills from the records, with the citation
marker code puts after it. The guard therefore does not try to understand prose; it checks that
the prose *cannot* carry a fact:

- every placeholder names a record and a field the records hold;
- one sentence (or list line) speaks of one record at most, with that record's own page, and the
  package's counts and sign-off stand in sentences of their own, so a fact cannot be put beside
  another record's page;
- the words outside the placeholders contain no digit in any script, no fraction, no number word
  ("one" to "hundred", "dozen", "half", "both", "all", "every", "each", "first" …), no outcome or
  evaluation word (pass, fail, right, wrong, correct, fine, ok, good, match, tolerance, short,
  long, issue, error, held, pending, ready, complete, done, need …), no decision or action word
  (approve, accept, reject, dismiss, mark, record, confirm, override, sign, decide, update, change,
  set, submit, close, skip, ignore, treat, consider, log, note, save, clear …), no link, markup,
  marker or brace, no zero-width, bidirectional or control character, no letter outside the
  Latin script, and only plain punctuation. Wall words stay usable: "right end", "left-hand wall",
  "on the right", "short return", "long side" are directions, not outcomes;
- evidence and actions name records that exist, and "open in the queue" only a record that still
  needs the reviewer.

Code-written answers (records-only, fallback, judging) are templates too and pass the same
placeholder, sentence and digit checks; their fixed wording is code's, so the vocabulary rules
apply to model text only.

Anything refused here is replaced by the records-only answer. Nothing here calls a model, reads
the database or changes an outcome.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from app.review.assistant.contract import Draft
from app.review.assistant.placeholders import (
    PLACEHOLDER,
    UnknownPlaceholder,
    slots,
    split_sentences,
    value_of,
)
from app.review.assistant.records import CountertopRecord, FindingRecord, ReviewSnapshot

__all__ = ["GROUPS", "GuardRejected", "check", "forbidden_words", "placeholder_keys"]

GROUPS: Final = ("blockers", "no_countertop_pages", "rows_not_checked")

_RECORD_ID: Final = re.compile(r"^[CF][0-9]+$")
_PAGE_ID: Final = re.compile(r"^P([0-9]+)$")
_LINKS: Final = re.compile(
    r"https?:|ftp:|javascript:|www\.|\][(]|<[^>]*>|&[a-z]+;|\b[a-z]{2,}\.[a-z]{2,}\b",
    re.IGNORECASE,
)
#: Punctuation a plain answer needs; anything else (markup, maths, %, &, symbols) is refused.
_PUNCTUATION: Final = frozenset(" \n.,;:!?'\"()-–—’‘“”/")
#: Three or more single letters in a row ("p-a-s-s"): a word spelled out to slip past the list.
_SPELLED: Final = re.compile(r"\b[a-z](?:[\s\-.'_]+[a-z]\b){2,}")

_NUMBER_WORDS: Final = frozenset(
    [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "hundreds",
        "thousand",
        "million",
        "dozen",
        "dozens",
        "half",
        "halves",
        "quarter",
        "quarters",
        "single",
        "double",
        "triple",
        "both",
        "all",
        "none",
        "every",
        "each",
        "several",
        "few",
        "most",
        "once",
        "twice",
        "thrice",
        "couple",
        "pair",
        "many",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "everything",
    ]
)
_OUTCOME_WORDS: Final = frozenset(
    [
        "ok",
        "okay",
        "okayed",
        "lgtm",
        "fine",
        "good",
        "bad",
        "spec",
        "specs",
        "specification",
        "off",
        "green",
        "red",
        "held",
        "hold",
        "holds",
        "ready",
        "done",
        "set",
        "log",
        "logs",
        "logged",
        "note",
        "noted",
        "notes",
        "too",
        "enough",
        "more",
        "less",
        "fewer",
        "extra",
        "over",
        "under",
        "plus",
        "minus",
        "total",
        "totals",
        "sum",
        "add",
        "adds",
        "added",
        "adding",
        "same",
        "great",
        "perfect",
        "excellent",
        "nice",
        "solid",
        "safe",
        "alright",
        "legit",
        "proper",
        "properly",
        "appropriate",
        "acceptable",
        "unacceptable",
        "satisfactory",
        "sufficient",
        "adequate",
        "true",
        "false",
        "wrong",
        "wrongly",
        "mistake",
        "mistaken",
        "flawed",
        "faulty",
        "should",
        "must",
        "ought",
        "shortfall",
        "excess",
        "broken",
    ]
)
_STEMS: Final = (
    "pass",
    "fail",
    "correct",
    "incorrect",
    "valid",
    "invalid",
    "accura",
    "inaccura",
    "conform",
    "nonconform",
    "compli",
    "noncompli",
    "match",
    "mismatch",
    "toleran",
    "error",
    "issue",
    "problem",
    "discrepan",
    "broke",
    "clear",
    "resolv",
    "fix",
    "rework",
    "pend",
    "await",
    "block",
    "unblock",
    "complet",
    "incomplet",
    "finish",
    "outstand",
    "remain",
    "need",
    "decision",
    "decid",
    "approv",
    "reject",
    "accept",
    "dismiss",
    "mark",
    "record",
    "confirm",
    "overrid",
    "overrul",
    "sign",
    "updat",
    "chang",
    "submit",
    "ship",
    "clos",
    "skip",
    "ignor",
    "treat",
    "consider",
    "sav",
    "releas",
    "waiv",
    "finali",
    "settl",
    "success",
    "succeed",
    "exceed",
    "differ",
    "equal",
    "identic",
    "flag",
    "consisten",
    "inconsisten",
    "defect",
    "concern",
    "reviewed",
)
#: Ordinary words that begin with a stem above but say nothing about an outcome.
_ALLOWED: Final = frozenset(
    ["records", "different", "clearance", "clearances", "fixture", "fixtures", "passage", "signage"]
)
_PHRASES: Final = re.compile(
    r"\b(?:checks? out|lines? up|spot on|in order|in line|on track|good to go|free of|turned? down|"
    r"sent back|send back|green light|go ahead|all set)\b"
)
_DIRECTIONAL_WORD: Final = frozenset({"right", "left", "short", "long"})
_PARTS: Final = (
    r"(?:end|side|wall|hand|edge|return|corner|run|piece|leg|angle|panel|filler|cabinet|splash|"
    r"overhang|depth|length|dimension)s?"
)
_DIRECTIONAL: Final = re.compile(
    r"\b(?:left|right)[- ]hand\b"
    r"|\b(?:on|to|at|from|towards?|along|by) the (?:far |near )?(?:left|right)\b"
    rf"|\b(?:left|right|short|long)(?:[- ]?most)?(?=[\s-]+{_PARTS}\b)"
)


class GuardRejected(Exception):
    """The answer is not backed by the records. The message is a code, never the answer's text."""


def placeholder_keys(text: str) -> tuple[str, ...]:
    """The record ids a template's placeholders name, in order (for the fallback)."""
    return tuple(dict.fromkeys(slot.key for slot in slots(text) if slot.is_record))


def _check_characters(free: str) -> None:
    for character in free:
        if character in "\n ":
            continue
        category = unicodedata.category(character)
        if category[0] == "N":
            raise GuardRejected("number-in-text")
        if category[0] in "CZ":
            raise GuardRejected("hidden-character")
        if category[0] == "L":
            if "LATIN" not in unicodedata.name(character, ""):
                raise GuardRejected("non-latin-letter")
            continue
        if category[0] == "M":
            continue
        if character not in _PUNCTUATION:
            raise GuardRejected("symbol-in-text")


def forbidden_words(free: str) -> tuple[str, ...]:
    """The words in model text that could state a number, an outcome or a decision."""
    folded = unicodedata.normalize("NFKC", free).casefold().replace("’", "'")
    plain = "".join(
        character
        for character in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(character)
    )
    plain = re.sub(r"\s+", " ", plain)
    found: list[str] = []
    if _SPELLED.search(plain):
        found.append("spelled-out")
    found.extend(match.group(0) for match in _PHRASES.finditer(plain))
    neutral = _DIRECTIONAL.sub(" ", plain)
    for word in re.findall(r"[a-z]+(?:'[a-z]+)?", neutral):
        base = word.split("'")[0]
        if word in _ALLOWED or base in _ALLOWED:
            continue
        if (
            base in _NUMBER_WORDS
            or base in _OUTCOME_WORDS
            or base in _DIRECTIONAL_WORD
            or base.startswith(_STEMS)
        ):
            found.append(word)
    return tuple(found)


#: Words a countertop's label may share with ordinary prose about countertops.
_LABEL_COMMON: Final = frozenset(
    [
        "countertop",
        "countertops",
        "counter",
        "top",
        "tops",
        "row",
        "rows",
        "run",
        "runs",
        "line",
        "lines",
        "page",
        "pages",
        "sheet",
        "sheets",
        "stone",
        "the",
        "and",
        "on",
        "of",
        "for",
        "at",
        "in",
        "with",
        "to",
        "wall",
        "walls",
        "back",
        "side",
        "end",
        "ends",
        "piece",
        "pieces",
        "section",
        "part",
    ]
)


def _label_leaks(free: str, snapshot: ReviewSnapshot) -> bool:
    """Whether model text names a countertop by its label in its own words ("Run A is …").

    A label comes only through `{Cn.label}`, which the one-record-per-sentence rule binds to that
    sentence's record. Refused: the whole label; any word of it that ordinary prose about
    countertops would not use; a single capital letter or code from it beside its neighbour word
    ("run A", "A1"), matched with its case so the article "A" stays usable.
    """
    plain = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", free))
    folded = plain.casefold()
    for item in snapshot.countertops:
        label = re.sub(r"\s+", " ", item.label).strip()
        if not label:
            continue
        if label.casefold() in folded:
            return True
        tokens = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", label)
        for index, token in enumerate(tokens):
            lowered = token.casefold()
            if (
                len(token) >= 3
                and token.isalpha()
                and lowered not in _LABEL_COMMON
                and re.search(rf"\b{re.escape(lowered)}\b", folded)
            ):
                return True
            if len(token) <= 2 and token[:1].isupper():
                code = re.escape(token)
                patterns = []
                if index > 0:
                    patterns.append(rf"(?i:\b{re.escape(tokens[index - 1])})\s+{code}\b")
                if index + 1 < len(tokens):
                    patterns.append(rf"\b{code}\s+(?i:{re.escape(tokens[index + 1])}\b)")
                if any(re.search(pattern, plain) for pattern in patterns):
                    return True
    return False


def _pages_of(record: CountertopRecord | FindingRecord) -> set[int]:
    return {record.page_number} if isinstance(record, CountertopRecord) else set(record.pages)


def _check_sentences(template: str, snapshot: ReviewSnapshot) -> None:
    """One record per sentence (with its own page), and package facts in sentences of their own."""
    for sentence in split_sentences(template):
        in_sentence = slots(sentence)
        records = {slot.key for slot in in_sentence if _RECORD_ID.match(slot.key)}
        pages = {int(slot.key[1:]) for slot in in_sentence if _PAGE_ID.match(slot.key)}
        package = any(slot.key in ("count", "signoff") for slot in in_sentence)
        if len(records) > 1:
            raise GuardRejected("two-records-in-one-sentence")
        if package and (records or pages):
            raise GuardRejected("package-fact-beside-a-record")
        if records:
            record = snapshot.record(next(iter(records)))
            if record is None or not pages <= _pages_of(record):
                raise GuardRejected("another-page-beside-a-record")
        elif len(pages) > 1:
            raise GuardRejected("two-pages-in-one-sentence")


def _check_references(draft: Draft, snapshot: ReviewSnapshot) -> None:
    pages = snapshot.pages()
    for evidence in draft.evidence:
        if evidence in GROUPS:
            continue
        if not evidence.startswith("C") or snapshot.record(evidence) is None:
            raise GuardRejected("evidence-unknown-record")
    for kind, target in draft.actions:
        if kind == "open_page":
            page = _PAGE_ID.match(target)
            if page is None or int(page.group(1)) not in pages:
                raise GuardRejected("action-unknown-page")
        elif kind == "open_queue_item":
            record = snapshot.record(target) if _RECORD_ID.match(target) else None
            if record is None:
                raise GuardRejected("action-unknown-record")
            if not record.needs_you:
                raise GuardRejected("action-record-needs-nothing")
        else:
            raise GuardRejected("action-unknown-kind")


def check(draft: Draft, snapshot: ReviewSnapshot, *, by_model: bool = True) -> None:
    """Raise `GuardRejected` unless every fact in `draft` is a placeholder the records fill."""
    text = draft.text
    if not text.strip():
        raise GuardRejected("empty-text")
    for slot in slots(text):
        try:
            value_of(snapshot, slot)
        except UnknownPlaceholder:
            raise GuardRejected("unknown-placeholder") from None
    free = PLACEHOLDER.sub(" ", text)
    if "[[" in free or "]]" in free or "{" in free or "}" in free:
        raise GuardRejected("marker-or-brace-in-text")
    if _LINKS.search(free):
        raise GuardRejected("link-or-markup")
    if any(unicodedata.category(character)[0] == "N" for character in free):
        raise GuardRejected("number-in-text")
    if by_model:
        _check_characters(free)
        if forbidden_words(free):
            raise GuardRejected("forbidden-word")
        if _label_leaks(free, snapshot):
            raise GuardRejected("label-in-own-words")
    _check_sentences(text, snapshot)
    _check_references(draft, snapshot)
