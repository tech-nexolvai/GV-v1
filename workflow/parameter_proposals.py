"""Point at the passage in a package that states a setting, and never supply the number (#849).

A setting the rules need, such as an overhang or a backsplash thickness, could only be typed. This
searches the package's own words (`retrieval.package_text`) for a passage that states it, checks the
passage, and files a pointer in `parameter_proposals`: the phrase, the runs within it that hold the
number, and the source the passage would make the value. A person confirms the number, by typing it
without seeing it, in step 3.3 of #798, which is still to be built. Nothing here can save a setting.

**Q10, in code: no setting is ever read off the vendor's drawing under review**
(`rules/overrides.py`). One guard, `_check`, runs when a pointer is made and again whenever one is
read back:

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

**The number is read, compared and dropped.** Code reads it exactly, as a `Fraction`, to check it is
one dimension and to compare it with every other passage that states the same setting. It is never
stored and never returned: no public result here carries it.

**Two passages, two values: no proposal, and a REVIEW note.** Which one the architect meant is a
person's question, and choosing either would be the system answering it.

**The span** runs from the first run of the phrase that holds a digit to the last, plus one run
right after it that is only a unit (an inch mark, `in`, `inch`, `inches` or `mm`): a reader that
stores `984` and `mm` as two words still has its millimetres seen. A word between two numbers falls
inside the span and makes it unreadable, which refuses the passage rather than guessing which number
the word belongs to.

**The newest proposal wins.** A proposal that differs from the setting's newest one is appended;
`current_parameter_proposals` reads the newest pointer per setting, and only while it still passes
the guard and the package still states one value for that setting.

**It can never write a setting.** The semgrep rule `gv-proposer-never-writes-a-setting` forbids this
module, the value-hunter and `retrieval/` from building a `ParameterValue` or calling the functions
that store one, and a test keeps all three from importing `app.api`.

Source: issue #849, plan step 3.2 on #798.
Verification: `tests/workflow/test_parameter_proposals.py`,
`tests/workflow/test_proposer_never_writes_a_setting.py`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.sides import ReadingSides, SideRefusal, SideRefusalReason
from app.models.evidence import ObservationCandidate
from app.models.package import PackageRevision
from app.models.package_text import TextPhrase, TextPhraseMember
from app.models.parameter_proposals import ParameterProposal
from retrieval.package_text import search_package_text
from rules.parameter_sources import ALLOWED_SOURCES, citable_sides, citable_sources
from rules.parameters import Provenance
from units.normalise import UnitNormalisationError, normalise_to_inches
from vocabulary.parameter_terms import search_terms
from vocabulary.semantic_types import DocumentRole

__all__ = [
    "PROPOSER",
    "PROPOSER_VERSION",
    "Citation",
    "CitationRefusal",
    "CitationRefusalReason",
    "ProposalOutcome",
    "SettingProposal",
    "check_proposal",
    "current_parameter_proposals",
    "propose_setting",
]

#: Who proposes, as each row records it.
PROPOSER: Final = "workflow.parameter_proposals"

#: The rule for choosing a span and reading it. A change to either is a new version, so every stored
#: pointer says which rule chose its runs.
PROPOSER_VERSION: Final = "1"

_DIGIT: Final = re.compile(r"[0-9]")

#: One number as a drawing writes it: a whole and a fraction (`1 1/2`), a fraction, or a whole or
#: decimal. `2 4` is two numbers, and so is the `3` and `6` of `3'-6"`.
_NUMBER: Final = re.compile(r"[0-9]+\s+[0-9]+/[0-9]+|[0-9]+/[0-9]+|[0-9]+(?:\.[0-9]+)?")

_MILLIMETRES: Final = re.compile(r"[0-9]\s*mm\b", re.IGNORECASE)

#: The inch marks `units.normalise` reads.
_INCH_MARK: Final = re.compile(r'"|\bin\b|\binch(?:es)?\b', re.IGNORECASE)

#: A run that is only a unit, taken into the span when it follows the last number.
_UNIT_ONLY: Final = re.compile(r'"|in|inch|inches|mm', re.IGNORECASE)


class CitationRefusalReason(StrEnum):
    """Why a passage cannot be cited for a setting: a fact about the passage or the setting."""

    NOT_CITABLE = "not_citable"
    """The setting may not come from a package as the claimed source: a company standard, say."""

    OUTSIDE_REVISION = "outside_revision"
    NO_SUCH_RUNS = "no_such_runs"

    NO_SIDE = "no_side"
    """A run has no side: a reviewer's markup, a drawing nobody confirmed, a reading between two
    drawings. `CitationRefusal.side_refusal` says which."""

    WRONG_SIDE = "wrong_side"
    """A run is on a side the source may not cite: the vendor's drawing under review."""

    NO_NUMBER = "no_number"
    MILLIMETRES = "millimetres"
    NOT_ONE_NUMBER = "not_one_number"
    NO_INCH_MARK = "no_inch_mark"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class CitationRefusal:
    """A passage that may not be cited, and why, in words that quote nothing from the drawing."""

    reason: CitationRefusalReason
    detail: str
    side_refusal: SideRefusalReason | None = None


@dataclass(frozen=True, slots=True)
class Citation:
    """A passage that passed the guard: where it is and what it would be, never what it says."""

    setting: str
    phrase_id: UUID
    first_member: int
    last_member: int
    claimed_source: Provenance
    candidate_ids: tuple[UUID, ...]
    """The runs from `first_member` to `last_member`, in order: the number's own runs."""


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


def _runs(session: Session, phrase_id: UUID) -> tuple[ObservationCandidate, ...] | None:
    """The phrase's runs in order, or `None` unless their positions are exactly 0, 1, 2 and so on.

    `build_package_phrases` numbers them so, and a gap would be a row no span can be placed in.
    """
    rows = session.execute(
        select(TextPhraseMember.position, ObservationCandidate)
        .join(ObservationCandidate, ObservationCandidate.id == TextPhraseMember.candidate_id)
        .where(TextPhraseMember.phrase_id == phrase_id)
        .order_by(TextPhraseMember.position)
    ).all()
    if not rows or [position for position, _ in rows] != list(range(len(rows))):
        return None
    return tuple(candidate for _, candidate in rows)


def _span(texts: Sequence[str]) -> tuple[int, int] | None:
    """Where the passage's number is, as run positions, or `None` if no run holds a digit."""
    digits = [position for position, text in enumerate(texts) if _DIGIT.search(text)]
    if not digits:
        return None
    first, last = digits[0], digits[-1]
    if last + 1 < len(texts) and _UNIT_ONLY.fullmatch(texts[last + 1].strip()):
        last += 1
    return first, last


def _read_inch_dimension(text: str) -> Fraction | CitationRefusal:
    """The span's one inch dimension, exactly, or why it is not one.

    Millimetres first, because `normalise_to_inches` would convert them, and a converted number is
    not what the architect wrote.
    """
    if _MILLIMETRES.search(text):
        return CitationRefusal(
            CitationRefusalReason.MILLIMETRES,
            "this number is in millimetres; inches are authoritative, and a millimetre value is "
            "never read as a setting",
        )
    numbers = len(_NUMBER.findall(text))
    if numbers == 0:
        return CitationRefusal(CitationRefusalReason.NO_NUMBER, "this passage states no number")
    if numbers > 1:
        return CitationRefusal(
            CitationRefusalReason.NOT_ONE_NUMBER,
            "this passage holds more than one number where one is expected, so which is the "
            "setting is unknown",
        )
    if not _INCH_MARK.search(text):
        return CitationRefusal(
            CitationRefusalReason.NO_INCH_MARK,
            "this number has no inch mark, so its unit is unknown",
        )
    try:
        return normalise_to_inches(text).exact
    except UnitNormalisationError:
        return CitationRefusal(
            CitationRefusalReason.UNREADABLE,
            "this number cannot be read as one dimension in inches",
        )


def _wrong_side(side: DocumentRole, source: Provenance) -> str:
    if side is DocumentRole.SHOP:
        return (
            "this passage is on the vendor's drawing under review, and no setting is ever read off "
            "the drawing being checked"
        )
    allowed = ", ".join(sorted(role.value for role in citable_sides(source)))
    return f"a {source.value} value is cited only from {allowed}, and this passage is {side.value}"


def _check(
    session: Session,
    sides: ReadingSides,
    *,
    package_revision_id: UUID,
    setting: str,
    phrase_id: UUID,
    first_member: int,
    last_member: int,
    claimed_source: Provenance,
) -> tuple[Citation, Fraction] | CitationRefusal:
    """The guard: the pointer, checked, with the number it names — or why it may not be cited.

    The number goes back only to this module's callers, to be compared (see the module docstring).
    """
    if claimed_source not in citable_sources(setting):
        return CitationRefusal(
            CitationRefusalReason.NOT_CITABLE,
            f"{setting} may not be cited from a package as {claimed_source.value}",
        )
    phrase = session.get(TextPhrase, phrase_id)
    if phrase is None or phrase.package_revision_id != package_revision_id:
        return CitationRefusal(
            CitationRefusalReason.OUTSIDE_REVISION,
            "this passage is not one of this package revision's",
        )
    runs = _runs(session, phrase_id)
    if runs is None or not 0 <= first_member <= last_member < len(runs):
        return CitationRefusal(
            CitationRefusalReason.NO_SUCH_RUNS, "the passage has no runs at those positions"
        )

    allowed = citable_sides(claimed_source)
    # Every run of the passage, not only the number's: a label on the vendor's drawing beside a
    # number on the architect's is not the architect saying what the number is.
    for run in runs:
        side = sides.of(run, confirmed_views_only=True)
        if isinstance(side, SideRefusal):
            return CitationRefusal(CitationRefusalReason.NO_SIDE, side.detail, side.reason)
        if side not in allowed:
            return CitationRefusal(
                CitationRefusalReason.WRONG_SIDE, _wrong_side(side, claimed_source)
            )

    span = runs[first_member : last_member + 1]
    value = _read_inch_dimension(" ".join(run.raw_text for run in span))
    if isinstance(value, CitationRefusal):
        return value
    citation = Citation(
        setting=setting,
        phrase_id=phrase_id,
        first_member=first_member,
        last_member=last_member,
        claimed_source=claimed_source,
        candidate_ids=tuple(run.id for run in span),
    )
    return citation, value


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


def _newest(session: Session, package_revision_id: UUID, setting: str) -> ParameterProposal | None:
    """The setting's newest pointer: by `created_at`, then by id, so a tie orders the same way."""
    return session.execute(
        select(ParameterProposal)
        .where(
            ParameterProposal.package_revision_id == package_revision_id,
            ParameterProposal.setting_name == setting,
        )
        .order_by(ParameterProposal.created_at.desc(), ParameterProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()


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


def check_proposal(
    session: Session, row: ParameterProposal, *, sides: ReadingSides | None = None
) -> Citation | CitationRefusal:
    """A stored pointer, put through the guard again now: the sides as they stand today."""
    checked = _check(
        session,
        ReadingSides(session) if sides is None else sides,
        package_revision_id=row.package_revision_id,
        setting=row.setting_name,
        phrase_id=row.phrase_id,
        first_member=row.first_member,
        last_member=row.last_member,
        claimed_source=Provenance(row.claimed_source),
    )
    return checked if isinstance(checked, CitationRefusal) else checked[0]


def current_parameter_proposals(
    session: Session, package_revision_id: UUID
) -> Mapping[str, ParameterProposal]:
    """Each setting's newest pointer, for the settings whose newest pointer still holds today.

    It holds when it passes the guard again, and the package's current phrases still state exactly
    one value for the setting, the one it names. A drawing confirmed as the vendor's after the
    pointer was filed, or a second passage that disagrees, withdraws it. An older pointer never
    takes its place: the newest is what the proposer last found.
    """
    rows = session.execute(
        select(ParameterProposal)
        .where(ParameterProposal.package_revision_id == package_revision_id)
        .order_by(
            ParameterProposal.setting_name,
            ParameterProposal.created_at.desc(),
            ParameterProposal.id.desc(),
        )
    ).scalars()
    newest: dict[str, ParameterProposal] = {}
    for row in rows:
        newest.setdefault(row.setting_name, row)

    sides = ReadingSides(session)
    current: dict[str, ParameterProposal] = {}
    for setting, row in sorted(newest.items()):
        checked = _check(
            session,
            sides,
            package_revision_id=package_revision_id,
            setting=setting,
            phrase_id=row.phrase_id,
            first_member=row.first_member,
            last_member=row.last_member,
            claimed_source=Provenance(row.claimed_source),
        )
        if isinstance(checked, CitationRefusal):
            continue
        decision = _decide(session, sides, package_revision_id, setting)
        if decision.outcome is ProposalOutcome.PROPOSED and decision.value == checked[1]:
            current[setting] = row
    return current
