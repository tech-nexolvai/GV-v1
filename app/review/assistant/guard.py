"""The deterministic check every assistant answer passes before a reviewer sees it (#1128).

**The model never writes a number, an outcome or a decision. Code writes all of them.** An answer is
a template: every fact is a placeholder (`{C1.outcome}`, `{C1.printed}`, `{count.needs_you}`,
`{signoff.status}`, see `placeholders.py`) that code fills from the records and cites. The guard
checks that the model's own words cannot carry, twist or frame a fact. It sorts sentences into two
classes:

**Fact sentences** (any sentence with a placeholder). The model's own words may only be glue from
a fixed allow-list (articles, "the countertop on", "its", "because", "with", "from", "and", "is",
"was", "has", the parts of a countertop and of the drawing, directional wall words, "shows",
"reads", "lists", "open", "see", "queue" …): no negation, hedge, contrast, evaluation or framing
word can sit beside a fact. Also:

- one record per sentence, with that record's own page; a page note (`{P3.no_countertop}`,
  `{P9.second_row}`) is a record of its own; counts and sign-off stand in sentences of their own;
- an outcome is said of its own subject: "the countertop on {C1.page}", "{C1.label}", "the
  {F1.check} check", or "the countertop" / "it" / "which" in a sentence that names it; a bare
  "{C1.page} {C1.outcome}" only when that page has nothing else on it;
- a list line under a header ("The countertop on {C1.page}:") speaks of the header's record.

**Explanation sentences** (no placeholder). Ordinary words are fine (difference, total, more,
less, held, decision …), but not: a yes/no interjection; a readiness or finished claim; a
recommendation to decide or act; a judgement about a result; a first-person action; a count.

**In all model text:** no digit in any script, no number word, no record label in its own words
(a label comes only through `{Cn.label}`), no link, markup, marker or brace, no hidden or
non-Latin character, only plain punctuation, no "!", "...", "e.g.", "i.e.", "vs.", "approx.",
"etc.". Every placeholder must be one the records hold; evidence and actions must name records
that exist, and the queue button only a record that still needs the reviewer.

Code-written answers (records-only, fallback, judging) pass the same structural checks; their
fixed wording is code's, so the vocabulary rules apply to model text only. Anything refused here
is replaced by the records-only answer. Nothing here calls a model, reads the database or changes
an outcome.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from app.review.assistant.contract import Draft
from app.review.assistant.placeholders import (
    CONNECTOR,
    PLACEHOLDER,
    Slot,
    UnknownPlaceholder,
    dropped_sentence,
    slots,
    split_sentences,
    value_of,
)
from app.review.assistant.records import CountertopRecord, FindingRecord, ReviewSnapshot

__all__ = [
    "GROUPS",
    "GuardRejected",
    "check",
    "explanation_words",
    "fact_words",
    "forbidden_words",
    "placeholder_keys",
]

GROUPS: Final = ("blockers", "no_countertop_pages", "rows_not_checked")

_RECORD_ID: Final = re.compile(r"^[CF][0-9]+$")
_PAGE_ID: Final = re.compile(r"^P([0-9]+)$")
_LINKS: Final = re.compile(
    r"https?:|ftp:|javascript:|www\.|\][(]|<[^>]*>|&[a-z]+;|\b[a-z]{2,}\.[a-z]{2,}\b",
    re.IGNORECASE,
)
_ABBREVIATIONS: Final = re.compile(
    r"!|\.\.\.|…|\b(?:e\.g|i\.e|vs|approx|etc|cf|viz|mr|mrs|ms|dr|no)\.", re.IGNORECASE
)
#: Punctuation a plain answer needs; anything else (markup, maths, %, &, symbols) is refused.
_PUNCTUATION: Final = frozenset(" \n.,;:?'\"()-–—’‘“”/")
#: Three or more single letters in a row ("p-a-s-s"): a word spelled out to slip past the lists.
_SPELLED: Final = re.compile(r"\b[a-z](?:[\s\-.'_]+[a-z]\b){2,}")
_WORDS: Final = re.compile(r"[a-z]+(?:'[a-z]+)?")

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
        "handful",
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

#: The only words a fact sentence may use around its placeholders.
_GLUE: Final = frozenset(
    [
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "there",
        "here",
        "what",
        "which",
        "where",
        "who",
        "whose",
        "on",
        "of",
        "for",
        "and",
        "with",
        "from",
        "because",
        "so",
        "as",
        "at",
        "in",
        "to",
        "by",
        "into",
        "onto",
        "is",
        "was",
        "were",
        "are",
        "be",
        "been",
        "has",
        "have",
        "had",
        "page",
        "pages",
        "sheet",
        "row",
        "rows",
        "run",
        "wall",
        "walls",
        "end",
        "ends",
        "side",
        "sides",
        "piece",
        "pieces",
        "overall",
        "reading",
        "readings",
        "read",
        "reads",
        "reader",
        "readers",
        "ai",
        "ais",
        "drawing",
        "vendor",
        "vendor's",
        "architect",
        "architect's",
        "reviewer",
        "reviewer's",
        "countertop",
        "countertop's",
        "cabinet",
        "cabinets",
        "filler",
        "fillers",
        "return",
        "returns",
        "edge",
        "edges",
        "corner",
        "corners",
        "line",
        "lines",
        "field",
        "cut",
        "cuts",
        "check",
        "checks",
        "result",
        "results",
        "value",
        "values",
        "width",
        "length",
        "sink",
        "top",
        "shows",
        "show",
        "shown",
        "lists",
        "listed",
        "open",
        "see",
        "look",
        "view",
        "queue",
        "start",
        "then",
        "example",
        "called",
        "named",
        "also",
        "together",
        "tell",
        "app",
        "records",
        "detail",
        "details",
        "below",
        "above",
        "where",
        "called",
        "uses",
        "use",
        "using",
        "sits",
        "sit",
        "between",
        "beside",
        "next",
        "review",
        "package",
        "rulebook",
        "rule",
        "against",
    ]
)
#: Further words a sentence that only names a record (its page or label, no fact) may use:
#: navigation and plain description, never an outcome.
_SUBJECT_ONLY_EXTRA: Final = frozenset(
    [
        "chose",
        "picked",
        "different",
        "disagreed",
        "nothing",
        "no",
        "measured",
        "measure",
        "try",
        "opening",
        "go",
        "compare",
        "comparing",
        "others",
        "other",
        "start",
        "starts",
        "about",
        "you",
        "your",
        "can't",
        "cannot",
        "find",
        "there",
    ]
)
_SUBJECT_FIELDS: Final = frozenset({"page", "label", "check"})
#: Glue phrases removed before the word check: harmless navigation idiom.
_GLUE_PHRASES: Final = re.compile(r"\ba good place to start\b|\bfor example\b")

#: Words an explanation sentence may not use (judgements, readiness, recommendations, decisions,
#: first-person actions, interjections). Ordinary words (difference, total, held, decision …) pass.
_EXPLAIN_WORDS: Final = frozenset(
    [
        "ok",
        "okay",
        "okayed",
        "lgtm",
        "fine",
        "good",
        "bad",
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
        "untrue",
        "wrong",
        "wrongly",
        "mistake",
        "mistaken",
        "mistyped",
        "misread",
        "misreading",
        "slip",
        "flawed",
        "faulty",
        "minor",
        "negligible",
        "trivial",
        "tiny",
        "small",
        "cosmetic",
        "common",
        "usual",
        "usually",
        "typical",
        "typically",
        "clean",
        "tidy",
        "decent",
        "spotless",
        "happy",
        "worry",
        "worries",
        "trouble",
        "fixed",
        "handled",
        "ready",
        "done",
        "finished",
        "complete",
        "completed",
        "wrapped",
        "green",
        "red",
        "pass",
        "passes",
        "passed",
        "passing",
        "fail",
        "fails",
        "failed",
        "failing",
        "correct",
        "correctly",
        "incorrect",
        "accurate",
        "inaccurate",
        "valid",
        "invalid",
        "compliant",
        "noncompliant",
        "spec",
        "specs",
        "off",
        "approvable",
        "approve",
        "approved",
        "approves",
        "approving",
        "approval",
        "accept",
        "accepted",
        "accepting",
        "reject",
        "rejected",
        "rejecting",
        "dismiss",
        "dismissed",
        "confirm",
        "confirmed",
        "override",
        "overridden",
        "overrule",
        "waive",
        "waived",
        "release",
        "released",
        "sign",
        "signed",
        "signing",
        "submit",
        "submitted",
        "ship",
        "shipped",
        "yes",
        "nope",
        "yeah",
        "yep",
        "agreed",
        "absolutely",
        "certainly",
        "sure",
        "indeed",
        "exactly",
        "recommend",
        "recommended",
        "recommending",
        "should",
        "must",
        "ought",
        "probably",
        "likely",
        "unlikely",
        "perhaps",
        "maybe",
        "supposedly",
        "allegedly",
        "apparently",
        "stored",
        "unheld",
        "lifted",
        "marked",
        "recorded",
        "logged",
        "noted",
        "mistakes",
        "redrawn",
        "redrew",
        "addressed",
        "already",
        "earlier",
        "previously",
        "forgot",
        "forget",
        "forgotten",
        "drafting",
        "saved",
        "cleared",
        "resolved",
        "settled",
        "closed",
        "skipped",
        "ignored",
        "treated",
        "considered",
        "updated",
        "changed",
        "trimmed",
        "trim",
        "trims",
        "outstanding",
        "pending",
        "awaiting",
        "blocked",
        "unblocked",
        "blocking",
        "clear",
        "right",
        "matches",
        "match",
        "matched",
        "mismatch",
        "fits",
        "agrees",
        "error",
        "errors",
        "issue",
        "issues",
        "problem",
        "problems",
        "discrepancy",
        "discrepancies",
        "blocker",
        "blockers",
        "free",
        "broken",
        "rework",
        "redone",
    ]
)
_EXPLAIN_PHRASES: Final = re.compile(
    r"\b(?:wrap(?:ped)? up|nothing stands|nothing else|last item|all set|in the clear|good to go|"
    r"go for it|go ahead|thumbs up|let (?:it|them|the \w+|this) through|wave[ds]? \w* ?through|"
    r"(?:is|are|goes|go|put|push|pushed|gets?) through|you can approve|nothing for you|"
    r"no (?:action|further|need)|nothing is missing|since then|last time|used to|"
    r"you may|move on|taken care|in order|checks? out|lines? up|spot on|ship shape|"
    r"good shape|i have|i've|i picked|i chose|i set|i sent|i asked|i would|i recommend|from me|"
    r"to me|a handful|ask the vendor|"
    r"have the vendor|out of your hands|no longer|any ?more|nothing to|nothing (?:is |else )?left|"
    r"nothing remains|nothing more|nothing needs|nothing (?:is |was )?(?:waiting|open|pending|"
    r"outstanding|blocking)|no (?:more |other )?(?:items?|decisions?|blockers?|findings?) "
    r"(?:left|remain|need|are)|(?:no|nothing)\b[^.]*\bleft)\b"
)
_INTERJECTION: Final = re.compile(
    r"^(?:yes|no|not yet|nope|yeah|yep|sure|agreed|absolutely|certainly|of course|indeed|"
    r"exactly|right|correct|ok|okay|got it|understood|done|handled|thanks?)\b"
)

_DIRECTIONAL_WORD: Final = frozenset({"right", "left", "short", "long"})
_PARTS: Final = (
    r"(?:end|side|wall|hand|edge|return|corner|run|piece|leg|angle|panel|filler|cabinet|splash|"
    r"overhang|depth|length|dimension)s?"
)
_DIRECTIONAL: Final = re.compile(
    r"\b(?:left|right)[- ]hand\b"
    r"|\b(?:on|to|at|from|towards?|along|by) the (?:far |near )?(?:left|right)\b"
    rf"|\b(?:left|right) and (?:left|right)(?=[\s-]+{_PARTS}\b)"
    r"|\bfrom (?:left|right) to (?:left|right)\b"
    rf"|\b(?:left|right|short|long)(?:[- ]?most)?(?=[\s-]+{_PARTS}\b)"
)
#: What an outcome may follow directly: its own subject (code puts the record id in for `X`).
_SUBJECTS: Final = (
    "the countertop on {X.page} ({X.label})",
    "the countertop on {X.page}",
    "a countertop on {X.page}",
    "{X.label} on {X.page}",
    "{X.label}",
    "the {X.check} check on {X.page}",
    "the {X.check} check",
)
#: What an outcome may follow when its subject is named earlier in the sentence.
_PRONOUNS: Final = ("the countertop", "it", "which", "that", "this countertop")

#: Labels share these words with ordinary prose about countertops.
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


#: Words that point back to an earlier sentence's subject: a fact sentence that does not name its
#: own record may not use them ("Here is page 9. Its values: {C1.printed}").
_BACK_REFERENCE: Final = re.compile(r"\b(?:it|its|this|that|they|their|them|these|those|here)\b")


class GuardRejected(Exception):
    """The answer is not backed by the records. The message is a code, never the answer's text."""


def placeholder_keys(text: str) -> tuple[str, ...]:
    """The record ids a template's placeholders name, in order (for the fallback)."""
    return tuple(dict.fromkeys(slot.key for slot in slots(text) if slot.is_record))


# ---- words ---------------------------------------------------------------------------------------


def _plain(free: str) -> str:
    folded = unicodedata.normalize("NFKC", free).casefold().replace("’", "'")
    plain = "".join(
        character
        for character in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", plain)


def _number_words(plain: str) -> list[str]:
    return [word for word in _WORDS.findall(plain) if word.split("'")[0] in _NUMBER_WORDS]


def fact_words(free: str, *, subject_only: bool = False) -> tuple[str, ...]:
    """The words around placeholders that are not on the glue allow-list (a sentence that only
    names a record, with no fact placeholder, may also use a few plain navigation words)."""
    plain = _GLUE_PHRASES.sub(" ", _DIRECTIONAL.sub(" ", _plain(free)))
    allowed = _GLUE | _SUBJECT_ONLY_EXTRA if subject_only else _GLUE
    found: list[str] = []
    if _SPELLED.search(plain):
        found.append("spelled-out")
    for word in _WORDS.findall(plain):
        if word not in allowed and word.split("'")[0] not in allowed:
            found.append(word)
    return tuple(found)


def explanation_words(free: str) -> tuple[str, ...]:
    """The words in an explanation sentence that judge, claim readiness, recommend or decide."""
    plain = re.sub(r"\b(?:minus|plus|negative|positive) signs?\b", " ", _plain(free)).strip()
    found: list[str] = []
    if _SPELLED.search(plain):
        found.append("spelled-out")
    if _INTERJECTION.match(plain):
        found.append("interjection")
    found.extend(match.group(0) for match in _EXPLAIN_PHRASES.finditer(plain))
    found.extend(_number_words(plain))
    neutral = _DIRECTIONAL.sub(" ", plain)
    for word in _WORDS.findall(neutral):
        base = word.split("'")[0]
        if base in _EXPLAIN_WORDS or (base in _DIRECTIONAL_WORD and base != "left"):
            found.append(word)
    return tuple(found)


def forbidden_words(free: str) -> tuple[str, ...]:
    """Explanation-class findings plus number words (kept for callers that check one text)."""
    return explanation_words(free)


def _label_leaks(free: str, snapshot: ReviewSnapshot) -> bool:
    """Whether model text names a countertop by its label in its own words ("Run A is …")."""
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


# ---- structure -----------------------------------------------------------------------------------


def _pages_of(record: CountertopRecord | FindingRecord) -> set[int]:
    return {record.page_number} if isinstance(record, CountertopRecord) else set(record.pages)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ,;:-").casefold()


def _outcome_has_subject(sentence: str, slot: Slot, snapshot: ReviewSnapshot) -> bool:
    key = slot.key
    before = _norm(sentence[: slot.start])
    for subject in _SUBJECTS:
        if before.endswith(subject.replace("X", key).casefold()):
            return True
    named = any(f"{{{key}.{field}}}".casefold() in before for field in ("page", "label", "check"))
    if named and any(re.search(rf"(?:^|\W){pronoun}$", before) for pronoun in _PRONOUNS):
        return True
    if before.endswith(f"{{{key}.page}}".casefold()):
        record = snapshot.record(key)
        if record is None:
            return False
        pages = _pages_of(record)
        alone = all(
            len(snapshot.on_page(page)) == 1
            and not any(
                note.page_number == page
                for note in (*snapshot.pages_without_countertop, *snapshot.rows_not_checked)
            )
            for page in pages
        )
        return alone and before == f"{{{key}.page}}".casefold()
    return False


def _check_structure(template: str, snapshot: ReviewSnapshot) -> None:
    header_keys: set[str] | None = None
    previous_keys: set[str] = set()
    for line in template.split("\n"):
        stripped = line.strip()
        if not stripped:
            header_keys = None
            previous_keys = set()
            continue
        is_item = stripped.startswith("- ")
        for sentence in split_sentences(stripped):
            in_sentence = slots(sentence)
            records = {slot.key for slot in in_sentence if _RECORD_ID.match(slot.key)}
            page_slots = [slot for slot in in_sentence if _PAGE_ID.match(slot.key)]
            pages = {int(slot.key[1:]) for slot in page_slots}
            notes = [slot for slot in page_slots if slot.field != "page"]
            package = any(slot.key in ("count", "signoff") for slot in in_sentence)
            if len(records) > 1:
                raise GuardRejected("two-records-in-one-sentence")
            if package and (records or pages):
                raise GuardRejected("package-fact-beside-a-record")
            if notes and (records or len(pages) > 1):
                raise GuardRejected("page-note-beside-another-record")
            if records and page_slots:
                raise GuardRejected("another-page-beside-a-record")
            if len(pages) > 1:
                raise GuardRejected("two-pages-in-one-sentence")
            for slot in in_sentence:
                if slot.field == "outcome" and not _outcome_has_subject(sentence, slot, snapshot):
                    raise GuardRejected("outcome-without-its-subject")
            if records and not (is_item and header_keys):
                key = next(iter(records))
                named = any(
                    slot.key == key and slot.field in ("page", "label", "check")
                    for slot in in_sentence
                )
                if not named and CONNECTOR.match(sentence) and previous_keys != {key}:
                    # "Also, {C1.needs_you}" continues the sentence before; it must be the same
                    # record's, since code puts no subject in front of a connector.
                    raise GuardRejected("connector-without-its-subject")
                free = _plain(PLACEHOLDER.sub(" ", sentence))
                pointing_away = previous_keys and key not in previous_keys
                if not named and pointing_away and _BACK_REFERENCE.search(free):
                    raise GuardRejected("fact-pointing-back-to-another-sentence")
            keys_here = {slot.key for slot in in_sentence if slot.is_record}
            # A sentence code will leave out (a repeated queue fill) is no anchor for the next.
            if keys_here and not dropped_sentence(sentence, template, snapshot):
                previous_keys = keys_here
            if is_item and header_keys and (records - header_keys):
                raise GuardRejected("list-item-of-another-record")
        if not is_item:
            keys = {slot.key for slot in slots(stripped) if slot.is_record}
            header_keys = keys if stripped.rstrip().endswith(":") and keys else None


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


def _walls_set_by_reviewer(in_sentence: list[Slot], snapshot: ReviewSnapshot) -> bool:
    """ "You set the walls" is the record's own fact when the walls placeholder's source is you."""
    for slot in in_sentence:
        record = snapshot.record(slot.key) if slot.field == "walls" else None
        if isinstance(record, CountertopRecord) and record.wall_source == "reviewer":
            return True
    return False


def _check_vocabulary(template: str, snapshot: ReviewSnapshot) -> None:
    free_all = PLACEHOLDER.sub(" ", template)
    _check_characters(free_all)
    if _ABBREVIATIONS.search(free_all):
        raise GuardRejected("abbreviation-or-exclamation")
    if _label_leaks(free_all, snapshot):
        raise GuardRejected("label-in-own-words")
    for line in template.split("\n"):
        for sentence in split_sentences(line.strip()):
            free = PLACEHOLDER.sub(" ", sentence)
            in_sentence = slots(sentence)
            if in_sentence:
                subject_only = all(slot.field in _SUBJECT_FIELDS for slot in in_sentence)
                words = set(fact_words(free, subject_only=subject_only))
                if _walls_set_by_reviewer(in_sentence, snapshot):
                    words -= {"you", "set", "yourself"}
                if words:
                    raise GuardRejected("word-not-allowed-beside-a-fact")
                if subject_only and explanation_words(free):
                    raise GuardRejected("word-not-allowed-in-an-explanation")
            elif explanation_words(free):
                raise GuardRejected("word-not-allowed-in-an-explanation")


def check(draft: Draft, snapshot: ReviewSnapshot, *, by_model: bool = True) -> None:
    """Raise `GuardRejected` unless every fact in `draft` is a placeholder the records fill and
    the words around it cannot change what it says."""
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
        _check_vocabulary(text, snapshot)
    _check_structure(text, snapshot)
    _check_references(draft, snapshot)
