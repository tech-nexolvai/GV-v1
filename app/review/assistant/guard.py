"""The deterministic check every assistant answer passes before a reviewer sees it (#1128).

**Chat-assistant style (owner, 2026-10-16).** The model talks freely about the drawings: it reads
numbers off the pictures, counts things, says what a dimension measures, compares the architect
with the vendor and answers any question in its own words. The screen marks an answer that looked
at the drawings as "AI reading of the drawing, not a decision". Only these hard rules stay, checked
here on the model's own words:

- **No action, ever.** The assistant has no write tools, so its words may not claim that anything
  was approved, signed off, confirmed, fixed, agreed, marked, clouded or recorded ("I approved
  page 4", "page 4 has been signed off", "it is now confirmed"), and may not advise deciding ("you
  can safely approve it", "it is safe to sign off", "I recommend approving it"). A request to
  decide never reaches the model (`intent.py`): it is refused and pointed to the item on screen.
- **A recorded result is quoted as recorded.** A pass or fail in the model's own words is refused
  ("passes", "failed", "PASS", "within tolerance"): a result comes only through its placeholder
  (`{C1.outcome}`), which code fills from the record, and a placeholder outcome may not be put on
  a page that is not its record's ("the countertop on page 7 {C1.outcome}" when C1 is page 4).
- No citation marker, brace, link or markup and no hidden character (the panel adds markers).
- Every placeholder must be one the records hold; evidence and actions must name records that
  exist, the queue button only a record that still needs the reviewer, and the review-list
  button only a list entry that is still open. No instruction or approval may sit beside the AI's
  best guess or a printed value placeholder ("Use {R1.guess}").

GV's markup being described as GV's (never as the vendor's numbers) is the prompt's rule; the
pictures the model sees have GV's markup removed (`page_pictures.py`).

Code-written answers (records-only, fallback, judging) keep the stricter structural checks they
were written to: no digit outside a placeholder, one record per sentence, an outcome right after
its own subject. Anything refused here is replaced by the records-only answer. Nothing here calls
a model, reads the database or changes an outcome.
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
    "placeholder_keys",
]

GROUPS: Final = ("blockers", "no_countertop_pages", "rows_not_checked")

_RECORD_ID: Final = re.compile(r"^[CFR][0-9]+$")
_QUEUE_ID: Final = re.compile(r"^[CF][0-9]+$")
_REVIEW_ID: Final = re.compile(r"^R[0-9]+$")
_PAGE_ID: Final = re.compile(r"^P([0-9]+)$")
_LINKS: Final = re.compile(
    r"https?:|ftp:|javascript:|www\.|\][(]|<[^>]*>|&[a-z]+;|\b[a-z]{2,}\.[a-z]{2,}\b",
    re.IGNORECASE,
)
_WORDS: Final = re.compile(r"[a-z]+(?:'[a-z]+)?")

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

#: Words that point back to an earlier sentence's subject: a fact sentence that does not name its
#: own record may not use them ("Here is page 9. Its values: {C1.printed}").
_BACK_REFERENCE: Final = re.compile(r"\b(?:it|its|this|that|they|their|them|these|those|here)\b")


#: Fields that fill a number the reviewer may be asked to accept: the AI's best guess (#1352) and
#: the printed overall. No instruction or approval may sit next to one, and no verdict on it.
_VALUE_FIELDS: Final = frozenset({"guess", "printed"})
#: An instruction or approval verb (and its forms); "go with", "lock in" … are two words.
_TAKE_VERB: Final = re.compile(
    r"\b(?:use[sd]?|using|take[sn]?|taking|took|keep[s]?|keeping|kept|pick[s]?|picking|"
    r"accept(?:s|ed|ing)?|confirm(?:s|ed|ing)?|enter(?:s|ed|ing)?|typ(?:e|es|ed|ing)|"
    r"appl(?:y|ies|ied|ying)|choos(?:e|es|ing)|chose|select(?:s|ed|ing)?|trust(?:s|ed|ing)?|"
    r"approv(?:e|es|ed|ing)|adopt(?:s|ed|ing)?|go(?:es|ing)? with|went with|stick with|"
    r"lock(?:s|ed|ing)? in|agree(?:s|d)? (?:with|to|on))\b"
)
#: A verdict on a value: "{R1.guess} is right", "… looks correct".
_VALUE_VERDICT: Final = re.compile(
    r"^\W*(?:\w+\W+){0,2}?(?:is|are|was|looks?|seems?|reads? as)\W+(?:\w+\W+)?"
    r"(?:right|correct|safe|fine|good|ok|okay|valid|accurate|true|acceptable|reliable)\b"
)


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


# ---- the model's own words: the hard rules -------------------------------------------------------


def _forms(*verbs: str) -> str:
    """Every simple form of each verb ("approve" -> approve, approves, approved, approving)."""
    out: set[str] = set()
    for verb in verbs:
        stem = verb.removesuffix("e")
        out.update((verb, f"{verb}s", f"{verb}es", f"{stem}ed", f"{stem}ing"))
    return "|".join(sorted(out, key=len, reverse=True))


#: What a reviewer does on the screen, and the assistant never does.
_DECIDING: Final = _forms(
    "approve",
    "sign",
    "confirm",
    "fix",
    "agree",
    "mark",
    "accept",
    "reject",
    "record",
    "clear",
    "resolve",
    "cloud",
    "dismiss",
    "tick",
    "close",
    "correct",
    "settle",
    "submit",
    "override",
    "waive",
    "update",
    "change",
    "edit",
    "rerun",
    "send",
)
_DONE_STATES: Final = (
    r"approved|signed(?: off)?|confirmed|fixed|agreed|marked|accepted|rejected|recorded|cleared|"
    r"resolved|clouded|dismissed|ticked|closed|corrected|settled|submitted|overridden|waived|"
    r"reviewed|updated|changed|sent back"
)
#: "I approved …", "I've marked …", "I will sign …", "let me confirm …", "page 4 has been
#: approved", "it is now confirmed".
_ACTION_CLAIM: Final = re.compile(
    rf"\bi(?:'ve|'d|'ll|'m going to|\s+(?:have|had|just|already|now|also|will|went ahead and|"
    rf"can|could|am going to|then))*\s+(?:{_DECIDING})\b"
    rf"|\blet me\s+(?:{_DECIDING})\b"
    rf"|\b(?:has|have|had)\s+(?:now\s+|just\s+|already\s+)?been\s+(?:{_DONE_STATES})\b"
    rf"|\b(?:is|are|was|were)\s+(?:now|already)\s+(?:{_DONE_STATES}|done)\b"
)
#: Advice to decide: "you can safely approve it", "safe to sign off", "I recommend approving",
#: "I would let it through", "send it back to the vendor".
_ADVICE: Final = re.compile(
    r"\byou\s+(?:can|could|may|should|might want to|ought to|are free to|are cleared to)\s+"
    r"(?:safely\s+|now\s+|just\s+|go ahead and\s+)*(?:approve|sign|accept|reject|release)\b"
    r"|\b(?:safe|ok|okay|fine|good|ready|free|cleared|clear)\s+(?:to|for)\s+(?:approv\w*|sign\w*|"
    r"accept\w*|confirm\w*|agree\w*|pass|let\b|release\w*)"
    r"|\bgo ahead and\s+(?:approve|sign|accept|confirm|agree|reject)\b"
    r"|\b(?:i|we)\s+(?:would\s+|'d\s+)?(?:recommend|suggest|advise)\s+(?:that you\s+|you\s+)?"
    r"(?:approv|sign|accept|confirm|agree|reject|let|send|releas|pass)\w*"
    r"|\b(?:i|we)\s+(?:would|'d)\s+(?:approve|sign|accept|reject|let|send|pass|release)\b"
    r"|\blet\s+(?:it|them|this|that|the \w+)\s+through\b|\bwave[ds]?\s+(?:\w+\s+)?through\b"
    r"|\bsen(?:d|ds|t|ding)\s+(?:it|them|this|that|the \w+)\s+back\b"
    r"|\bshould be\s+(?:sent back|approved|accepted|rejected|signed|released|passed)\b"
    r"|(?:^|[.?!:;]\s+|\n\s*)(?:please\s+|just\s+|now\s+)?(?:approve|sign|accept|reject|release)\b"
    r"|\byou can (?:safely |now )?move on\b"
)
#: A pass or fail, or the app's own result words, in the model's own words: a result comes only
#: through `{C1.outcome}`, and sign-off readiness only through `{signoff.status}`.
_OUTCOME_WORDS: Final = re.compile(
    r"\b(?:pass(?:es|ed|ing)?|fail(?:s|ed|ing|ure|ures)?|out of (?:tolerance|spec)|"
    r"(?:with)?in (?:the )?(?:tolerance|allowance|limits?|spec)|in spec|inside the allowed|"
    r"looks? right|needs? correction|(?:non-?)?compliant|acceptable|approvable|"
    r"(?:is|are) (?:accurate|valid|invalid)|nothing (?:is )?wrong)\b"
)
_READINESS: Final = re.compile(
    r"\bsign[- ]?off is (?:now )?(?:unblocked|available|possible|clear|allowed|open)\b"
    r"|\bcan (?:now )?(?:sign|be signed|be released|be approved|go ahead)\b"
    r"|\b(?:review|package|job|work)(?: here)? is (?:now )?(?:complete|finished|done|ready)\b"
    r"|\bno (?:further )?(?:action|attention) (?:is )?(?:required|needed)\b|\brequires? no further\b"
    r"|\b(?:the )?last item\b"
    r"|\bnothing (?:else |more )?(?:needs|is waiting|is pending|is outstanding|stands|left|"
    r"remains|to do)\b"
    r"|\bno (?:more |other )?(?:items?|decisions?|blockers?|findings?) (?:left|remain|need|are "
    r"(?:left|outstanding|pending|open))\b|\bno blockers?\b"
    r"|\b(?:all|every) (?:items?|results?|countertops?|findings?) (?:has|have|are|is|look)\b"
    r"|\bwrap(?:ped)? up\b|\bgo ahead\b|\bgo for it\b|\bthumbs up\b|\bgood to go\b|\ball set\b"
    r"|\bin the clear\b|\byours to give\b|\bchecks? out\b"
    r"|\blooks? (?:good|fine|great|clean|ok|okay|correct|perfect)\b"
    r"|\b(?:is|are) (?:fine|ok|okay|good|perfect|spot on)\b"
)
#: Words that twist a recorded result in its own sentence ("it is untrue that … {C1.outcome}").
_TWIST: Final = re.compile(
    r"\b(?:not|no longer|never|untrue|isn't|aren't|doesn't|don't|wasn't|didn't|nobody|"
    r"doubtful|supposedly|allegedly|perhaps|probably|maybe|likely|unlikely|used to|previously|"
    r"earlier|but|although|though|however|instead|rather|unlike|misread|wrong|ignore\w*|"
    r"disregard\w*|really)\b|\?"
)
#: Three or more single letters in a row ("p-a-s-s"): a word spelled out to slip past the rules.
_SPELLED: Final = re.compile(r"\b[a-z](?:[\s\-.'_]+[a-z]\b){2,}")
#: A page named in the model's own words ("page 7", "sheet 7", "p. 7").
_PAGE_WORDS: Final = re.compile(r"\b(?:page|sheet|pg|p)\.?\s*#?\s*([0-9]+)\b")


def _check_hidden(free: str) -> None:
    for character in free:
        if character in "\n\t":
            continue
        category = unicodedata.category(character)
        if category[0] == "C" or category in ("Zl", "Zp"):
            raise GuardRejected("hidden-character")
        if category[0] == "L" and "LATIN" not in unicodedata.name(character, ""):
            # A look-alike letter ("lооks" with Cyrillic o) would slip past the word rules.
            raise GuardRejected("non-latin-letter")


#: Result placeholders that must name their own record in their sentence ("the countertop on
#: {C1.page} {C1.outcome}" or "on page 4, {C1.outcome}"), so one cannot be read as another's.
_SUBJECT_NEEDED: Final = frozenset({"outcome"})
_NAMING_FIELDS: Final = frozenset({"page", "label", "check", "what"})


def _names_itself(
    in_sentence: list[Slot], key: str, named: set[int], pages: set[int] | None
) -> bool:
    if any(slot.key == key and slot.field in _NAMING_FIELDS for slot in in_sentence):
        return True
    return pages is not None and bool(named) and named <= pages


def _only_its_page_names_it(in_sentence: list[Slot], key: str, snapshot: ReviewSnapshot) -> bool:
    """A countertop's result named only by its page, on a page with more than one countertop."""
    record = snapshot.record(key)
    if not isinstance(record, CountertopRecord):
        return False
    if any(slot.key == key and slot.field in ("label", "check", "what") for slot in in_sentence):
        return False
    return sum(1 for item in snapshot.countertops if item.page_number == record.page_number) > 1


#: Placeholders that state a recorded result or sign-off state.
_RESULT_FIELDS: Final = frozenset({"outcome", "decision", "status", "needs_you"})


def _record_pages(snapshot: ReviewSnapshot, key: str) -> set[int] | None:
    """The pages a record is on, or `None` when it names none (a package-wide finding)."""
    page = _PAGE_ID.match(key)
    if page is not None:
        return {int(page.group(1))}
    record = snapshot.record(key)
    if record is not None:
        pages = _pages_of(record)
        return pages or None
    if key.startswith("R"):
        item = snapshot.review_item(key)
        if item is not None and item.page_number is not None:
            return {item.page_number}
    return None


def _check_model_words(template: str, snapshot: ReviewSnapshot) -> None:
    """The hard rules on the model's own words (see the module docstring)."""
    free_all = PLACEHOLDER.sub(" ", template)
    _check_hidden(free_all)
    plain = _plain(free_all)
    if _SPELLED.search(plain):
        raise GuardRejected("spelled-out")
    if _ACTION_CLAIM.search(plain):
        raise GuardRejected("action-claim")
    if _ADVICE.search(plain):
        raise GuardRejected("advice-to-decide")
    if _OUTCOME_WORDS.search(plain):
        raise GuardRejected("outcome-in-own-words")
    if _READINESS.search(plain):
        raise GuardRejected("readiness-in-own-words")
    for line in template.split("\n"):
        previous_keys: set[str] = set()
        for sentence in split_sentences(line.strip()):
            in_sentence = slots(sentence)
            keys_here = {slot.key for slot in in_sentence if slot.is_record}
            if len(keys_here) == 1 and CONNECTOR.match(sentence):
                # "Also, {C2.hold_reason}" after a sentence about C1 would read as C1's (code puts
                # no subject in front of a connector).
                (key,) = keys_here
                names_it = any(
                    slot.key == key and slot.field in _NAMING_FIELDS for slot in in_sentence
                )
                if not names_it and previous_keys != {key}:
                    raise GuardRejected("connector-without-its-subject")
            if keys_here and not dropped_sentence(sentence, template, snapshot):
                previous_keys = keys_here
            facts = [
                slot for slot in in_sentence if slot.is_record and slot.field not in _NAMING_FIELDS
            ]
            free = _plain(PLACEHOLDER.sub(" ", sentence))
            stated = [
                slot
                for slot in in_sentence
                if slot.field in _RESULT_FIELDS or slot.key in ("count", "signoff")
            ]
            if stated and _TWIST.search(free):
                raise GuardRejected("outcome-twisted")
            if not facts:
                continue
            outcomes = [slot for slot in facts if slot.field in _RESULT_FIELDS]
            named = {int(page) for page in _PAGE_WORDS.findall(free)}
            for slot in in_sentence:
                if slot.field == "page":
                    named |= _record_pages(snapshot, slot.key) or set()
            for slot in facts:
                pages = _record_pages(snapshot, slot.key)
                if pages is not None and named - pages:
                    raise GuardRejected("fact-on-another-page")
            for slot in outcomes:
                pages = _record_pages(snapshot, slot.key)
                if slot.field in _SUBJECT_NEEDED and not _names_itself(
                    in_sentence, slot.key, named, pages
                ):
                    raise GuardRejected("outcome-without-its-subject")
                if slot.field in _SUBJECT_NEEDED and _only_its_page_names_it(
                    in_sentence, slot.key, snapshot
                ):
                    raise GuardRejected("outcome-subject-ambiguous")


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


def _check_value_framing(template: str) -> None:
    """No instruction or approval right before or after a value the reviewer may accept, and no
    verdict on it: "Use {R1.guess}." would read as advice to accept the AI's number (#1354)."""
    for line in template.split("\n"):
        for sentence in split_sentences(line.strip()):
            for slot in slots(sentence):
                if slot.field not in _VALUE_FIELDS:
                    continue
                before = _plain(PLACEHOLDER.sub(" ", sentence[: slot.start]))
                after = _plain(PLACEHOLDER.sub(" ", sentence[slot.end :]))
                near_before = " ".join(_WORDS.findall(before)[-4:])
                near_after = " ".join(_WORDS.findall(after)[:4])
                if _TAKE_VERB.search(near_before) or _TAKE_VERB.search(near_after):
                    raise GuardRejected("instruction-beside-a-value")
                if _VALUE_VERDICT.match(after):
                    raise GuardRejected("verdict-on-a-value")


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
            record = snapshot.record(target) if _QUEUE_ID.match(target) else None
            if record is None:
                raise GuardRejected("action-unknown-record")
            if not record.needs_you:
                raise GuardRejected("action-record-needs-nothing")
        elif kind == "open_review_item":
            item = snapshot.review_item(target) if _REVIEW_ID.match(target) else None
            if item is None:
                raise GuardRejected("action-unknown-review-item")
            if not item.needs_you:
                raise GuardRejected("action-review-item-needs-nothing")
        else:
            raise GuardRejected("action-unknown-kind")


def check(draft: Draft, snapshot: ReviewSnapshot, *, by_model: bool = True) -> None:
    """Raise `GuardRejected` unless `draft` keeps the hard rules: every placeholder is one the
    records fill, nothing claims or advises a decision, a result is quoted only as recorded, and
    every evidence id and action names a record that exists (and still needs the reviewer)."""
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
    if by_model:
        _check_model_words(text, snapshot)
    else:
        # Code's own wording: no digit outside a placeholder, and the one-record structure.
        if any(unicodedata.category(character)[0] == "N" for character in free):
            raise GuardRejected("number-in-text")
        _check_structure(text, snapshot)
    _check_value_framing(text)
    _check_references(draft, snapshot)
