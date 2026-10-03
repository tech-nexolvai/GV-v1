"""The guard on a setting's passage, and a typed number held to it (#849, #866).

`workflow/parameter_proposals.py` points at the passage in a package that states a setting (#849).
A reviewer then types the number without being shown it, and the value is saved only if it matches
(step 3.3 of #798, #866). Both ask this module, so the passage a pointer is filed for, the passage
the form shows, and the passage a typed number is held to pass one guard.

**Here rather than in the proposer**, because the API runs it and `DESIGN_PLATFORM.md` §2 keeps
`app/api/` from ever reaching `retrieval/` (`tests/api/test_no_heavy_work.py`). The proposer searches
the package's words through `retrieval.package_text`; nothing here searches.

**Q10, in code: no setting is ever read off the vendor's drawing under review**
(`rules/overrides.py`). The guard, `_check`:

- the setting must allow a source that may cite a package at all. GV's company standards, the field
  cut and the fabricator's clearance may cite nothing (`rules.parameter_sources.CITABLE_SIDES`), so
  no passage anywhere can be cited for one;
- every run of the passage, its words as well as its number, must be on a side that source may cite.
  The side is decided by `app.evidence.sides.ReadingSides` with `confirmed_views_only`, there and
  then, and never stored, because a drawing's confirmed role can change. The vendor's drawing is
  refused, a reviewer's markup is refused, and so is a drawing nobody has confirmed, even where the
  upload would give it a side;
- the runs the pointer names must read as exactly one inch-marked number, through `units.normalise`.
  Millimetres are refused because inches are authoritative (Q12), and so are a number with no inch
  mark and two numbers in one span.

**The number is read, compared and dropped.** `_check` reads it exactly, as a `Fraction`, for the
proposer to compare passages with each other and for `confirm_typed_value` to compare with what a
person typed. It is never stored and never returned by anything public here, and no refusal's words
state it, so a mismatch cannot be put right by reading the error.

**What a search would add, and the API goes without.** The proposer's `current_parameter_proposals`
also withdraws a pointer when the package's words now state a second, different value for the
setting, which only a search can tell. `live_parameter_proposals` and `confirm_typed_value` hold the
pointer to the guard and to being the setting's newest, and no more: a passage that starts to
disagree is seen when the proposer next runs.

**It can never write a setting.** `gv-proposer-never-writes-a-setting` covers this module as it covers
the proposer, and a test keeps it from importing `app.api`.

Source: issue #849, plan step 3.2 on #798; issue #866, step 3.3.
Verification: `tests/workflow/test_parameter_proposals.py`, `tests/api/test_setting_citations.py`,
`tests/workflow/test_proposer_never_writes_a_setting.py`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.sides import ReadingSides, SideRefusal, SideRefusalReason
from app.models.evidence import ObservationCandidate
from app.models.package_text import TextPhrase, TextPhraseMember
from app.models.parameter_proposals import ParameterProposal
from rules.parameter_sources import citable_sides, citable_sources
from rules.parameters import Provenance
from units.measurement import Measurement, Unit
from units.normalise import UnitNormalisationError, normalise_to_inches
from vocabulary.semantic_types import DocumentRole

__all__ = [
    "Citation",
    "CitationRefusal",
    "CitationRefusalReason",
    "check_proposal",
    "confirm_typed_value",
    "live_parameter_proposals",
]

#: One number as a drawing writes it: a whole and a fraction (`1 1/2`), a fraction, or a whole or
#: decimal. `2 4` is two numbers, and so is the `3` and `6` of `3'-6"`.
_NUMBER: Final = re.compile(r"[0-9]+\s+[0-9]+/[0-9]+|[0-9]+/[0-9]+|[0-9]+(?:\.[0-9]+)?")

_MILLIMETRES: Final = re.compile(r"[0-9]\s*mm\b", re.IGNORECASE)

#: The inch marks `units.normalise` reads.
_INCH_MARK: Final = re.compile(r'"|\bin\b|\binch(?:es)?\b', re.IGNORECASE)


class CitationRefusalReason(StrEnum):
    """Why a passage cannot be cited for a setting, or cannot confirm the number a person typed.

    The members up to `UNREADABLE` are facts about the passage or the setting; the last four are
    what `confirm_typed_value` adds about the pointer and the typed number.
    """

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

    NO_SUCH_PROPOSAL = "no_such_proposal"
    """The citation names no pointer this package revision filed for this setting."""

    WITHDRAWN = "withdrawn"
    """The pointer passes the guard, but a newer pointer for the setting has replaced it."""

    TYPED_MILLIMETRES = "typed_millimetres"
    """The person typed millimetres. The passage is in inches, and a converted number is not one
    read off it."""

    MISMATCH = "mismatch"
    """The number a person typed is not the passage's number."""


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

    The number goes back only to the proposer and to `confirm_typed_value`, to be compared (see the
    module docstring).
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


def _check_row(
    session: Session, sides: ReadingSides, row: ParameterProposal
) -> tuple[Citation, Fraction] | CitationRefusal:
    """`_check` on a stored pointer, against the revision and the source it names."""
    return _check(
        session,
        sides,
        package_revision_id=row.package_revision_id,
        setting=row.setting_name,
        phrase_id=row.phrase_id,
        first_member=row.first_member,
        last_member=row.last_member,
        claimed_source=Provenance(row.claimed_source),
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


def _newest_by_setting(session: Session, package_revision_id: UUID) -> dict[str, ParameterProposal]:
    """Each setting's newest pointer in this revision, in the order `_newest` uses."""
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
    return newest


def check_proposal(
    session: Session, row: ParameterProposal, *, sides: ReadingSides | None = None
) -> Citation | CitationRefusal:
    """A stored pointer, put through the guard again now: the sides as they stand today."""
    checked = _check_row(session, ReadingSides(session) if sides is None else sides, row)
    return checked if isinstance(checked, CitationRefusal) else checked[0]


def live_parameter_proposals(
    session: Session, package_revision_id: UUID
) -> Mapping[str, ParameterProposal]:
    """Each setting's newest pointer, for the settings whose newest pointer passes the guard now.

    What the form may offer a reviewer (#866). A drawing confirmed as the vendor's after the pointer
    was filed withdraws it, and an older pointer never takes its place. Unlike the proposer's
    `current_parameter_proposals`, this does not search the package for a second passage that
    disagrees (see the module docstring).
    """
    sides = ReadingSides(session)
    return {
        setting: row
        for setting, row in sorted(_newest_by_setting(session, package_revision_id).items())
        if not isinstance(_check_row(session, sides, row), CitationRefusal)
    }


def confirm_typed_value(
    session: Session,
    *,
    package_revision_id: UUID,
    proposal_id: UUID,
    setting: str,
    typed: Measurement,
) -> Citation | CitationRefusal:
    """Hold a number a person typed to the passage they were pointed at; the citation on a match.

    Step 3.3 of #798 (#866): a reviewer is shown the passage and never the number, types what they
    see, and the value is saved only if it matches. This is the comparison. It saves nothing and
    returns no number: the API stores the typed value when a `Citation` comes back.

    Refused, in this order:

    - a pointer that is not this revision's, or not for `setting`;
    - one a newer pointer for the setting has replaced, as `live_parameter_proposals` would not
      offer it;
    - one that fails the guard now: the vendor's drawing, a drawing nobody confirmed, a reviewer's
      markup, or anything but one inch-marked number;
    - a typed value in millimetres: the passage is in inches (Q12), and a converted number is not
      one read off it;
    - a typed value that is not the passage's number, compared exactly as two `Fraction`s.
    """
    row = session.get(ParameterProposal, proposal_id)
    if row is None or (row.package_revision_id, row.setting_name) != (package_revision_id, setting):
        return CitationRefusal(
            CitationRefusalReason.NO_SUCH_PROPOSAL,
            f"no passage stating {setting} was found in this package under that citation",
        )
    newest = _newest(session, package_revision_id, setting)
    if newest is None or newest.id != row.id:
        return CitationRefusal(
            CitationRefusalReason.WITHDRAWN,
            "a newer passage has replaced this one for this setting; reload the form",
        )
    checked = _check_row(session, ReadingSides(session), row)
    if isinstance(checked, CitationRefusal):
        return checked
    citation, value = checked
    # The unit as well as the token: `normalise_to_inches` returns inches for `38 mm`, and a
    # measurement built in millimetres would otherwise be compared as if its number were inches.
    if typed.unit is not Unit.INCH or (
        typed.raw_text is not None and _MILLIMETRES.search(typed.raw_text)
    ):
        return CitationRefusal(
            CitationRefusalReason.TYPED_MILLIMETRES,
            "type the number in inches, as the architect's drawing writes it; a millimetre value "
            "is never compared with the passage",
        )
    if typed.exact != value:
        return CitationRefusal(
            CitationRefusalReason.MISMATCH,
            "the number typed is not the number in the passage",
        )
    return citation
