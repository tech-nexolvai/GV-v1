"""Telling a request to decide, a request to judge, and a question apart (#1128). Deterministic.

Three kinds of message, three answers, none of which lets a model decide:

- **A request to decide** ("mark page 7 as passed", "approve it", "sign off", "page 4 is fine,
  approve it", "aprueba la página 4") gets a fixed refusal with a button to the item, and no model
  call. The reviewer decides, in the queue.
- **A request to judge** ("should page 4 pass?", "can I approve page 4?", "is it okay to approve
  page 4?") gets the recorded outcome from the records and the sentence that the decision is the
  reviewer's, also with no model call: a model asked to judge could sound like a decision.
- **Everything else** ("why did page 4 fail?", "sign-off status?", "pass rate?", "flag any page
  with no countertop") is a question about the records.

**How a request is recognised.** The message is split into clauses (at punctuation and at "and",
"then", "but"), and polite or filler openers ("please", "yes", "go ahead", "can you", "I want to")
are taken off each. A clause is a request when it:

- starts with a strong deciding verb (approve and its common misspellings, accept, reject,
  dismiss, sign off, override, waive; Spanish, French and German "approve") not followed by a noun
  such as "status", "rate", "summary", "log", "readiness";
- starts with a weaker verb (mark, pass, fail, set, change, confirm, clear, resolve, decide,
  record, flag, close, release, tick) followed by what it would act on (page 4, it, this, the
  result, everything);
- or the message says "mark/set/change/make/turn … as/to pass/green/approved", "make page 4
  pass", "page 4 should be marked as passed", or "I'd like page 4 accepted".

A wrong refusal costs the reviewer one rephrase; a request answered as if decided costs much more.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "REFUSAL_TEXT",
    "is_decision_request",
    "is_judging_question",
    "page_mentioned",
]

REFUSAL_TEXT: Final = (
    "I can't make, change or record a decision. Only you decide, in the review queue: open the "
    "item there and record your decision."
)

#: Openers that make what follows a request, or that add nothing.
_OPENERS: Final = re.compile(
    r"^(?:please|pls|plz|kindly|yes|yeah|yep|sure|ok|okay|alright|right|so|now|then|just|hey|hi|"
    r"go ahead(?: and)?|go on|do it|can you|could you|would you|will you|can u|could u|"
    r"would you please|you can|you should|you must|i want you to|i'd like you to|"
    r"i would like you to|i need you to|help me|let's|lets|let us|"
    r"(?:i|we) (?:want|would like|'d like|need|am going|'m going|are going|'re going|will|'ll)"
    r"(?: to)?|i|we)\b[\s,]*"
)
#: Verbs that decide on their own. `a+p+r+o+u?v` covers approve, aprove, approuve and doubled
#: letters; the rest are common typos and the Spanish, French and German forms.
_STRONG: Final = re.compile(
    r"^(?:a+p+r+o+u?v(?:e|es|ed|ing|er|ez|ons|a|are)?|appove\w*|aprvo\w*|aprob\w*|"
    r"aprueb\w*|genehmig\w*|freigeb\w*|acept\w*|valid(?:er|ez|e)|"
    r"accept|reject|dismiss|override|overrule|waive|greenlight|green light|submit|ship|unblock|"
    r"lgtm|proceed|"
    r"sign(?: it| this| that| them| the package| everything| all)? off|sign)\b"
)
#: Words after a deciding verb that make it a noun phrase about the records, not a command.
_NOUN_AFTER: Final = re.compile(
    r"^\s*(?:status|readiness|rate|rates|reasons?|summary|log|history|process|button|criteria|"
    r"rules?|meaning|means|mean|date|time|count|list|state|steps?|options?|up|of|for|"
    r"what|which|any|whether|if|how|why|fail|pass|by|blockers?|requirements?|checklist|"
    r"needed|required|items?|or)\b"
)
_WEAK: Final = re.compile(
    r"^(?:pass|fail|set|change|confirm|clear|resolve|decide|record|flag|except|release|"
    r"close out|close|tick|check off|ok|okay|finali[sz]e|complete|update|treat|consider|put|"
    r"push|let|bump|ignore|skip|note|log|save|give|call|move|count|flip)\b"
)
#: What a weak verb must act on to be a command.
_TARGET: Final = re.compile(
    r"^\s*(?:page|sheet|pg|p\d+|it|this|that|these|those|them|everything|all|both|"
    r"the (?:package|item|items|row|rows|result|results|finding|findings|check|checks|"
    r"countertop|countertops|sink|hold|wall|walls|outcome|review|whole|rest|reading)|"
    r"as (?:pass|fail|ok|okay|approved|accepted|done|resolved|correct|right))\b|^\s*\d+\s*$"
)
#: A weak verb alone ("pass", "fail", "close") is a command too.
_BARE_WEAK: Final = frozenset({"pass", "fail", "close", "ok", "okay", "confirm"})
#: "mark it done", "mark page 7 passed": marking as an outcome (plain "mark the walls" annotates).
_MARK_DONE: Final = re.compile(
    r"\bmark\s+(?:it|this|that|them|everything|all|page \d+|sheet \d+|p\d+|\d+)?\s*(?:as\s+)?"
    r"(?:done|pass(?:ed|ing)?|fail(?:ed|ing)?|ok(?:ay)?|approved|accepted|resolved|complete|"
    r"correct|green)\b"
)
#: A weak verb whose object is followed by "up" ("clear this up", "mark up") asks to explain.
#: A question about what a word means or what an action does is never a request.
_ABOUT_A_TERM: Final = re.compile(
    r"\bwhat (?:does|do|would) .*\b(?:mean|do)\b|\bwhat(?:'s| is) the difference\b|"
    r"\bwhat happens (?:when|if)\b|\bmeaning of\b"
)
#: Weaker verbs that act only when an outcome or a decision is named with them.
_NEEDS_OUTCOME: Final = frozenset(
    {
        "treat",
        "consider",
        "count",
        "call",
        "put",
        "give",
        "move",
        "let",
        "push",
        "bump",
        "note",
        "log",
        "save",
        "update",
    }
)
_OUTCOME_WORD: Final = re.compile(
    r"\b(?:pass(?:ed|es)?|fail(?:ed|s)?|approved|accepted|done|ok|okay|green|red|fine|resolved|"
    r"through|correct|right|dismissed|complete|completed|closed|cleared|island|back wall|"
    r"both ends|exception)\b"
)
#: Whole messages that only make sense as a go-ahead to act.
_BARE_COMMANDS: Final = frozenset(
    {
        "confirmed",
        "accepted",
        "approved",
        "do it",
        "yes do it",
        "yes please",
        "go ahead",
        "proceed",
        "okay it",
        "ok it",
        "done",
        "ship it",
        "lgtm",
        "agreed",
    }
)
_OTHER_REQUESTS: Final = re.compile(
    r"^(?:page|sheet|p) ?\d+ (?:is )?(?:approved|accepted|passed|pass|done|green|dismissed|ok|okay)$"
    r"|\b(?:why don'?t you|how about|what about you) (?:approv|accept|pass|mark|sign|dismiss)"
    r"|\bbe (?:approved|accepted|passed|marked|signed off|dismissed) by you\b"
    r"|\bwould you (?:approve|accept|pass|mark|sign off)\b.*\bfor me\b"
    r"|\b(?:freigeben|genehmigen|aprobar|approuver|approvare)\b"
    r"|^mark (?:it|this|that|page \d+|sheet \d+|p\d+)$"
    r"|\b(?:can|may|could) (?:go|get) through\b|\bwave\b.*\bthrough\b|\bgood to go\b|"
    r"\bfine by me\b|\bmy (?:ok|okay|approval|blessing|go ahead)\b|\bde ma part\b|\bgoed\b|"
    r"\bkeur\w*\b|\bmark (?:the |an? |it as an? )?exception\b|^pass on (?:page|sheet|p)\b|"
    r"\bcan be (?:dismissed|approved|accepted|passed|signed off|closed|resolved|cleared)\b|"
    r"\bdo the sign off\b|\bget (?:this|it|the \w+) signed off\b|"
    r"\b(?:finish|wrap up|complete|finali[sz]e|close) (?:the|this) review\b|"
    r"\bsend (?:it |this |them |the \w+ |page \d+ )?(?:back|to the vendor)\b|"
    r"\brequest changes\b|\bflip\b.*\bto (?:pass|fail|green|red)\b|\bun ?hold\b|"
    r"\b(?:choose|pick|use|select) (?:the )?(?:\w+ )?(?:line|row)\b"
)
_PHRASAL_UP: Final = re.compile(r"^\s*(?:\w+\s+)?up\b")
#: "Confirm page 4 needs correction" asks to verify; "confirm the walls are …" answers a question.
_STATEMENT: Final = re.compile(r"\b(?:needs?|is|are|was|were|has|have)\b")
_WALL_WORDS: Final = re.compile(r"\b(?:walls?|layout|island|ends?|back)\b")
_MARK_AS: Final = re.compile(
    r"\b(?:mark|set|change|make|flag|turn|switch|move|record)\b.{0,60}?\b(?:as|to|into)\s+"
    r"(?:a\s+)?(?:pass(?:ed)?|fail(?:ed)?|ok(?:ay)?|approved|accepted|rejected|dismissed|done|"
    r"resolved|correct|right|looks right|green|good|red)\b"
)
_MAKE_IT: Final = re.compile(
    r"\b(?:make|turn|let)\s+(?:page \d+|sheet \d+|p\d+|it|this|that|them|everything|all)\s+"
    r"(?:pass|fail|green|red|ok|okay|approved|accepted|go through)\b"
)
_PASSIVE: Final = re.compile(
    r"\b(?:should|must|shall|needs? to|has to|to) be\s+(?:marked|approved|accepted|passed|"
    r"failed|dismissed|signed off|rejected|confirmed|cleared|resolved)\b"
)
_WANT_DONE: Final = re.compile(
    r"\b(?:i'?d|i would|we'?d|we would|i|we) (?:like|want|need)\s+(?:page|sheet|it|this|that|"
    r"them|everything|all|the)\b.{0,40}?\b(?:accepted|approved|passed|failed|dismissed|"
    r"rejected|signed off|marked|confirmed)\b"
)
_JUDGING: Final = re.compile(
    r"^(?:can|may|could) (?:i|we) (?:approve|accept|pass|fail|reject|dismiss|ok|okay)\b"
    r"|\bis it (?:ok|okay|fine|safe|alright|all right|good) to (?:approve|accept|pass|fail|"
    r"reject|dismiss|sign off)\b"
    r"|\bshould (?:i|we|it|this|that|page \d+|sheet \d+|p\d+) (?:approve|accept|pass|fail|"
    r"reject|dismiss|sign off|be (?:approved|accepted|passed|failed|rejected|dismissed))\b"
    r"|\b(?:do|would) you (?:think|say|recommend|suggest)\b.*\b(?:pass|fail|approve|accept|"
    r"reject|dismiss|approved|accepted)\b"
    r"|\bwould you (?:approve|accept|pass|fail|reject|sign off)\b"
    r"|\bis (?:page \d+|sheet \d+|it|this|that) (?:ok|okay|good|fine|acceptable|safe) to "
    r"(?:approve|accept|pass|sign off)\b"
    r"|\bwhat (?:should|do|can|must) (?:i|we) (?:approve|accept|pass|sign off|decide|look at)\b"
    r"|^is (?:page|sheet|p) ?\d+ (?:ok|okay|fine|good|right|correct|wrong|acceptable|approved|"
    r"accepted|done|signed off|passed|failing|failed|alright)\b"
    r"|\bdo you think\b|\bwould (?:the vendor|the architect|you|they|anyone|a reviewer) "
    r"(?:pass|fail|approve|accept|reject)\b"
    r"|^is (?:the )?sign off (?:blocked|possible|allowed|ready|done)\b"
    r"|^(?:has|have|was|were|is) (?:page \d+|sheet \d+|it|this|the package|the review) "
    r"(?:been )?(?:signed off|approved|accepted|confirmed|dismissed|passed)\b"
    r"|\bshould i (?:worry|be worried)\b"
)
_QUESTION_START: Final = re.compile(
    r"^(?:why|what|which|where|when|who|whose|how|is|are|was|were|did|does|do|has|have|had|"
    r"can (?:i|we)|could (?:i|we)|may (?:i|we)|tell me|show me|explain|list|give me)\b"
)
_PAGE: Final = re.compile(r"\b(?:page|sheet|pg|p)\.?\s*#?\s*(\d+)\b")


def _normalise(text: str) -> str:
    folded = text.casefold().replace("’", "'")
    folded = re.sub(r"sign[\s-]?off", "sign off", folded)
    folded = re.sub(r"(?<=[a-z])/(?=[a-z])", " ", folded)  # "pass/fail" is two words
    folded = re.sub(r"[^\w\s'?.,;:!]", " ", folded)
    return re.sub(r"\s+", " ", folded).strip(" ?")


def _clauses(text: str) -> list[str]:
    parts = re.split(r"[.,;:!?]+|\s(?:and|then|but)\s", text)
    return [part.strip() for part in parts if part.strip()]


def _strip_openers(clause: str) -> str:
    for _ in range(6):
        if _QUESTION_START.match(clause):
            return clause
        stripped = _OPENERS.sub("", clause, count=1)
        if stripped == clause:
            break
        clause = stripped
    return clause


def _clause_is_request(clause: str, message: str = "") -> bool:
    clause = _strip_openers(clause)
    if not clause or _QUESTION_START.match(clause):
        return False
    strong = _STRONG.match(clause)
    if strong is not None:
        return _NOUN_AFTER.match(clause[strong.end() :]) is None
    weak = _WEAK.match(clause)
    if weak is None:
        return False
    rest = clause[weak.end() :]
    if not rest.strip():
        return weak.group(0) in _BARE_WEAK
    if _PHRASAL_UP.match(rest):
        return False
    if weak.group(0) == "confirm":
        if _WALL_WORDS.search(rest):
            return re.search(r"\b(?:is|are|as|to)\b", rest) is not None
        if _STATEMENT.search(rest):
            return False
    if weak.group(0) in _NEEDS_OUTCOME:
        return _TARGET.match(rest) is not None and _OUTCOME_WORD.search(message or rest) is not None
    return _TARGET.match(rest) is not None


def is_judging_question(question: str) -> bool:
    """Whether `question` asks the assistant to judge an outcome or a decision."""
    return _JUDGING.search(_normalise(question)) is not None


def is_decision_request(question: str) -> bool:
    """Whether `question` asks the assistant to decide, mark, approve, sign off or dismiss."""
    text = _normalise(question)
    if _ABOUT_A_TERM.search(text):
        return False
    if text.strip(" .!,") in _BARE_COMMANDS or _OTHER_REQUESTS.search(text):
        return True
    if is_judging_question(question):
        return False
    if (
        _MARK_AS.search(text)
        or _MARK_DONE.search(text)
        or _MAKE_IT.search(text)
        or _PASSIVE.search(text)
        or _WANT_DONE.search(text)
    ):
        return True
    return any(_clause_is_request(clause, text) for clause in _clauses(text))


def page_mentioned(question: str) -> int | None:
    """The first page number a question names ("page 7", "sheet 7", "p7"), if any."""
    match = _PAGE.search(_normalise(question))
    return int(match.group(1)) if match else None
