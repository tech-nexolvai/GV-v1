"""Point at the passage in a package that states a setting, and never supply the number (#849).

A setting the rules need, such as an overhang or a backsplash thickness, could only be typed. This
searches the package's own words (`retrieval.package_text`) for a passage that states it, checks the
passage, and files a pointer in `parameter_proposals`: the phrase, the runs within it that hold the
number, and the source the passage would make the value. A person confirms the number by typing it
without seeing it (step 3.3 of #798, #866), and the API saves it only if it matches. Nothing here can
save a setting.

**Q10, in code: no setting is ever read off the vendor's drawing under review**
(`rules/overrides.py`). One guard, `workflow.parameter_citations._check`, runs when a pointer is made
and again whenever one is read back. It lives there because the API runs it too, and the API may
never reach `retrieval/`, which this module searches through:

- the setting must allow a source that may cite a package at all. GV's company standards, the field
  cut and the fabricator's clearance may cite nothing (`rules.parameter_sources.CITABLE_SIDES`), so
  no passage anywhere can propose one;
- every run of the passage, its words as well as its number, must be on a side that source may cite.
  The side is decided by `app.evidence.sides.ReadingSides` with `confirmed_views_only`, there and
  then, and never stored, because a drawing's confirmed role can change. The vendor's drawing is
  refused, a reviewer's markup is refused, and so is a drawing nobody has confirmed, even where the
  upload would give it a side;
- the runs the pointer names must read as exactly one inch-marked number, through `units.normalise`.
  Millimetres are refused because inches are authoritative (Q12), and so are a number with no inch
  mark and two numbers in one span.

**The number is read, compared and dropped.** The guard reads it exactly, as a `Fraction`, to check
it is one dimension, and this module compares it with every other passage that states the same
setting. It is never stored and never returned: no public result here carries it.

**Two passages, two values: no proposal, and a REVIEW note.** Which one the architect meant is a
person's question, and choosing either would be the system answering it.

**The span** runs from the first run of the phrase that holds a digit to the last, plus one run
right after it that is only a unit (an inch mark, `in`, `inch`, `inches` or `mm`): a reader that
stores `984` and `mm` as two words still has its millimetres seen. A word between two numbers falls
inside the span and makes it unreadable, which refuses the passage rather than guessing which number
the word belongs to.

**The newest proposal wins.** A proposal that differs from the setting's newest one is appended;
`current_parameter_proposals` reads the newest pointer per setting, and only while it still passes
the guard and the package still states one value for that setting. The form offers, and a typed
number is held to, the pointers `workflow.parameter_citations.live_parameter_proposals` reads: the
same, without the search for a second value.

**One passage at a time, for a caller that found it.** `propose_passage` files a pointer to the
passage its caller names, the value-hunter's way in (#881), and only where `propose_setting` would
cite the same value: the setting's own words must find the passage, the passage must pass the guard,
and every other passage that states the setting must agree with it.

**It can never write a setting.** The semgrep rule `gv-proposer-never-writes-a-setting` forbids this
module, the value-hunter and `retrieval/` from building a `ParameterValue` or calling the functions
that store one, and a test keeps all three from importing `app.api`.

Source: issue #849, plan step 3.2 on #798; the guard moved out by #866; `propose_passage` by #881.
Verification: `tests/workflow/test_parameter_proposals.py`,
`tests/workflow/test_proposer_never_writes_a_setting.py`, `tests/workflow/test_value_hunter.py`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from app.evidence.sides import ReadingSides
from app.models.package import PackageRevision
from app.models.parameter_proposals import ParameterProposal
from retrieval.package_text import search_package_text
from rules.parameter_sources import ALLOWED_SOURCES, citable_sources
from vocabulary.parameter_terms import search_terms
from workflow.parameter_citations import (
    Citation,
    CitationRefusal,
    CitationRefusalReason,
    _check,
    _check_row,
    _newest,
    _newest_by_setting,
    _runs,
)

__all__ = [
    "PROPOSER",
    "PROPOSER_VERSION",
    "ProposalOutcome",
    "SettingProposal",
    "current_parameter_proposals",
    "propose_passage",
    "propose_setting",
]

#: Who proposes, as each row records it.
PROPOSER: Final = "workflow.parameter_proposals"

#: The rule for choosing a span and reading it. A change to either is a new version, so every stored
#: pointer says which rule chose its runs.
PROPOSER_VERSION: Final = "1"

_DIGIT: Final = re.compile(r"[0-9]")

#: A run that is only a unit, taken into the span when it follows the last number.
_UNIT_ONLY: Final = re.compile(r'"|in|inch|inches|mm', re.IGNORECASE)


class ProposalOutcome(StrEnum):
    """What a search for one setting ended in."""

    PROPOSED = "proposed"
    NOT_FOUND = "not_found"
    REVIEW = "review"
    """Passages that may be cited state different values. Nothing is proposed; a person decides."""

    REFUSED = "refused"
    """The setting is one no passage may ever propose."""


@dataclass(frozen=True, slots=True)
class SettingProposal:
    """The answer for one setting: ids, an outcome, and words quoting nothing from the drawing."""

    setting: str
    outcome: ProposalOutcome
    note: str
    proposal_id: UUID | None = None
    """The stored pointer, for `PROPOSED` only."""

    phrase_ids: tuple[UUID, ...] = ()
    """The passage proposed, or for `REVIEW` the passages that disagree, in the order found."""

    refusals: tuple[CitationRefusal, ...] = ()
    """Why each passage the search found could not be cited."""


@dataclass(frozen=True, slots=True)
class _Decision:
    outcome: ProposalOutcome
    note: str
    citation: Citation | None = None
    value: Fraction | None = None
    """Read to compare with a stored pointer's, and never stored or returned."""

    phrase_ids: tuple[UUID, ...] = ()
    refusals: tuple[CitationRefusal, ...] = ()


def _span(texts: Sequence[str]) -> tuple[int, int] | None:
    """Where the passage's number is, as run positions, or `None` if no run holds a digit."""
    digits = [position for position, text in enumerate(texts) if _DIGIT.search(text)]
    if not digits:
        return None
    first, last = digits[0], digits[-1]
    if last + 1 < len(texts) and _UNIT_ONLY.fullmatch(texts[last + 1].strip()):
        last += 1
    return first, last


def _decide(
    session: Session, sides: ReadingSides, package_revision_id: UUID, setting: str
) -> _Decision:
    """What the package's current phrases say about `setting`, without writing anything."""
    if setting not in ALLOWED_SOURCES:
        return _Decision(
            ProposalOutcome.REFUSED, f"{setting} is not a setting any published rule uses"
        )
    sources = citable_sources(setting)
    if not sources:
        named = " or ".join(source.value for source in ALLOWED_SOURCES[setting])
        return _Decision(
            ProposalOutcome.REFUSED,
            f"{setting} comes from {named}, which is never read off a package",
        )
    if len(sources) > 1:
        return _Decision(
            ProposalOutcome.REFUSED,
            f"{setting} may be cited as more than one source, and a passage cannot say which",
        )
    (source,) = sources
    terms = search_terms(setting)
    if not terms:
        return _Decision(
            ProposalOutcome.NOT_FOUND,
            f"no search words are written for {setting} (vocabulary/parameter_terms.py)",
        )

    found: list[tuple[Citation, Fraction]] = []
    refusals: list[CitationRefusal] = []
    seen: set[UUID] = set()
    for term in terms:
        for hit in search_package_text(session, package_revision_id, term):
            if hit.phrase_id in seen:
                continue
            seen.add(hit.phrase_id)
            runs = _runs(session, hit.phrase_id)
            span = None if runs is None else _span([run.raw_text for run in runs])
            if span is None:
                refusals.append(
                    CitationRefusal(
                        CitationRefusalReason.NO_NUMBER, "this passage states no number"
                    )
                )
                continue
            checked = _check(
                session,
                sides,
                package_revision_id=package_revision_id,
                setting=setting,
                phrase_id=hit.phrase_id,
                first_member=span[0],
                last_member=span[1],
                claimed_source=source,
            )
            if isinstance(checked, CitationRefusal):
                refusals.append(checked)
            else:
                found.append(checked)

    if not found:
        return _Decision(
            ProposalOutcome.NOT_FOUND,
            f"nothing this package may be cited from states {setting}"
            + (f"; {len(refusals)} passage(s) found and refused" if refusals else ""),
            refusals=tuple(refusals),
        )
    if len({value for _, value in found}) > 1:
        return _Decision(
            ProposalOutcome.REVIEW,
            f"{len(found)} passages state {setting}, and they do not all agree; a reviewer decides "
            "which, if any, the architect meant",
            phrase_ids=tuple(citation.phrase_id for citation, _ in found),
            refusals=tuple(refusals),
        )
    citation, value = found[0]
    return _Decision(
        ProposalOutcome.PROPOSED,
        f"{setting} is stated in a passage a {source.value} value may be cited from; a person "
        "confirms the number",
        citation=citation,
        value=value,
        phrase_ids=(citation.phrase_id,),
        refusals=tuple(refusals),
    )


def _record(session: Session, package_revision_id: UUID, citation: Citation) -> ParameterProposal:
    """File the pointer, unless the setting's newest pointer already says exactly this."""
    newest = _newest(session, package_revision_id, citation.setting)
    if newest is not None and (
        newest.phrase_id,
        newest.first_member,
        newest.last_member,
        newest.claimed_source,
        newest.proposer,
        newest.proposer_version,
    ) == (
        citation.phrase_id,
        citation.first_member,
        citation.last_member,
        citation.claimed_source.value,
        PROPOSER,
        PROPOSER_VERSION,
    ):
        return newest
    row = ParameterProposal(
        package_revision_id=package_revision_id,
        setting_name=citation.setting,
        phrase_id=citation.phrase_id,
        first_member=citation.first_member,
        last_member=citation.last_member,
        claimed_source=citation.claimed_source.value,
        proposer=PROPOSER,
        proposer_version=PROPOSER_VERSION,
    )
    session.add(row)
    session.flush()
    return row


def propose_setting(session: Session, package_revision_id: UUID, setting: str) -> SettingProposal:
    """Search this revision's phrases for `setting`; file a pointer if exactly one value is cited.

    Searches the revision's current phrase build with each of the setting's terms, in order, and
    keeps each passage once. A revision whose phrases were never built finds nothing.
    """
    if not isinstance(package_revision_id, UUID):
        raise TypeError("package_revision_id must be a UUID")
    if session.get(PackageRevision, package_revision_id) is None:
        raise ValueError(f"no package revision {package_revision_id}")
    decision = _decide(session, ReadingSides(session), package_revision_id, setting)
    if decision.citation is None:
        return SettingProposal(
            setting=setting,
            outcome=decision.outcome,
            note=decision.note,
            phrase_ids=decision.phrase_ids,
            refusals=decision.refusals,
        )
    row = _record(session, package_revision_id, decision.citation)
    return SettingProposal(
        setting=setting,
        outcome=decision.outcome,
        note=decision.note,
        proposal_id=row.id,
        phrase_ids=decision.phrase_ids,
        refusals=decision.refusals,
    )


def _not_filed(setting: str, refusal: CitationRefusal, phrase_id: UUID) -> SettingProposal:
    return SettingProposal(
        setting=setting,
        outcome=ProposalOutcome.NOT_FOUND,
        note=refusal.detail,
        phrase_ids=(phrase_id,),
        refusals=(refusal,),
    )


def propose_passage(
    session: Session, package_revision_id: UUID, setting: str, phrase_id: UUID
) -> SettingProposal:
    """File a pointer to this one passage for `setting`, if `propose_setting` would cite its value.

    For a caller that names the passage itself: the value-hunter (`workflow/value_hunter.py`, #881)
    tries the passages its searches found one at a time. Naming a passage gets it nothing
    `propose_setting` would not give it:

    - a setting no passage may propose is refused, and one with no search words is not found, as
      `propose_setting` says of them;
    - the passage must be one the setting's own search words find in the revision's current phrases;
    - its span is chosen by `_span`, and it must pass the guard, `_check`;
    - every passage those words find is then decided as `propose_setting` decides them, so a second
      value anywhere in the package gives REVIEW and no pointer, whichever passage was named.

    PROPOSED files the pointer through `_record`, so an unchanged one is not filed twice. Anything
    else files nothing and says why in `note`; where the span or the guard refused this passage,
    that refusal is in `refusals`.
    """
    if not isinstance(package_revision_id, UUID):
        raise TypeError("package_revision_id must be a UUID")
    if not isinstance(phrase_id, UUID):
        raise TypeError("phrase_id must be a UUID")
    if session.get(PackageRevision, package_revision_id) is None:
        raise ValueError(f"no package revision {package_revision_id}")
    sides = ReadingSides(session)
    sources = citable_sources(setting)
    terms = search_terms(setting)
    if len(sources) != 1 or not terms:
        # `_decide` answers these before it searches anything.
        decision = _decide(session, sides, package_revision_id, setting)
        return SettingProposal(setting=setting, outcome=decision.outcome, note=decision.note)
    (source,) = sources

    if not any(
        hit.phrase_id == phrase_id
        for term in terms
        for hit in search_package_text(session, package_revision_id, term)
    ):
        return SettingProposal(
            setting=setting,
            outcome=ProposalOutcome.NOT_FOUND,
            note=f"this passage is not one the search words for {setting} find in this package's "
            "current phrases",
            phrase_ids=(phrase_id,),
        )
    runs = _runs(session, phrase_id)
    span = None if runs is None else _span([run.raw_text for run in runs])
    if span is None:
        return _not_filed(
            setting,
            CitationRefusal(CitationRefusalReason.NO_NUMBER, "this passage states no number"),
            phrase_id,
        )
    checked = _check(
        session,
        sides,
        package_revision_id=package_revision_id,
        setting=setting,
        phrase_id=phrase_id,
        first_member=span[0],
        last_member=span[1],
        claimed_source=source,
    )
    if isinstance(checked, CitationRefusal):
        return _not_filed(setting, checked, phrase_id)
    citation, value = checked

    decision = _decide(session, sides, package_revision_id, setting)
    if decision.outcome is ProposalOutcome.PROPOSED and decision.value == value:
        row = _record(session, package_revision_id, citation)
        return SettingProposal(
            setting=setting,
            outcome=ProposalOutcome.PROPOSED,
            note=decision.note,
            proposal_id=row.id,
            phrase_ids=(phrase_id,),
            refusals=decision.refusals,
        )
    if decision.outcome is ProposalOutcome.REVIEW:
        return SettingProposal(
            setting=setting,
            outcome=ProposalOutcome.REVIEW,
            note=decision.note,
            phrase_ids=decision.phrase_ids,
            refusals=decision.refusals,
        )
    # The passage passed and the search finds it, so its own value is among those decided. Anything
    # else means the package changed between the two reads, a rebuild of its phrases or a drawing's
    # role confirmed, and nothing is filed.
    return SettingProposal(
        setting=setting,
        outcome=ProposalOutcome.NOT_FOUND,
        note=f"this package changed while the passage was checked; nothing is filed for {setting}",
        phrase_ids=(phrase_id,),
    )


def current_parameter_proposals(
    session: Session, package_revision_id: UUID
) -> Mapping[str, ParameterProposal]:
    """Each setting's newest pointer, for the settings whose newest pointer still holds today.

    It holds when it passes the guard again, and the package's current phrases still state exactly
    one value for the setting, the one it names. A drawing confirmed as the vendor's after the
    pointer was filed, or a second passage that disagrees, withdraws it. An older pointer never
    takes its place: the newest is what the proposer last found.
    """
    sides = ReadingSides(session)
    current: dict[str, ParameterProposal] = {}
    for setting, row in sorted(_newest_by_setting(session, package_revision_id).items()):
        checked = _check_row(session, sides, row)
        if isinstance(checked, CitationRefusal):
            continue
        decision = _decide(session, sides, package_revision_id, setting)
        if decision.outcome is ProposalOutcome.PROPOSED and decision.value == checked[1]:
            current[setting] = row
    return current
