"""Where a confirmed setting was read: the passage a person typed it from, blind (#866).

Step 3.3 of the plan on #798. The app points at the passage in the architect's drawing that states a
setting (`parameter_proposals`, #849); a reviewer types the number without being shown it, and the
server saves it only if it equals the passage's number. One row here records which passage that
was, for the stored value it confirmed: the document version and the sha256 of its bytes, the page,
and the runs that hold the number.

**Copied out of the pointer, not only linked to it.** A pointer is decided again every time it is
read, and can stop holding once a drawing's role is corrected. What the reviewer typed against is a
fact about the moment they typed it, so the bytes, page and runs are written down here as they were.
The pointer is kept too, so a citation can be traced to the code that proposed the passage.

**One row per stored value, and a carried value carries its citation.** A save that changes another
setting copies every earlier value into the new version (#799), as a new `parameter_values` row; the
citation is copied with it (`app/api/measurements.py:_store`), so the value the checks read still
says where it came from.

**Append-only, and not readable by the verdict role**, whose allowlist in `app/db/roles.py` names
neither table. The value itself is in `parameter_values`, which is how a check reads it.

Source: issue #866, plan step 3.3 on #798. Verification: `tests/api/test_setting_citations.py`.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID

__all__ = ["ParameterValueCitation", "ParameterValueCitationRun"]

_SHA256_PATTERN: Final = "^[0-9a-f]{64}$"


class ParameterValueCitation(Base, TimestampedUUID, Immutable):
    """The passage one stored setting value was typed from, and matched."""

    __tablename__ = "parameter_value_citations"

    parameter_value_id: Mapped[UUID] = mapped_column(
        ForeignKey("parameter_values.id", ondelete="RESTRICT")
    )
    """The stored value. Unique: a value was typed against one passage."""

    parameter_proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("parameter_proposals.id", ondelete="RESTRICT"), index=True
    )
    """The pointer the reviewer was shown, so the citation can be traced to the code that found the
    passage."""

    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    document_sha256: Mapped[str] = mapped_column(String(64))
    """The bytes of the drawing the passage is in, as `document_versions.sha256` had them."""

    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"), index=True)

    __table_args__ = (
        UniqueConstraint("parameter_value_id", name="uq_parameter_value_citations_value"),
        CheckConstraint(
            f"document_sha256 ~ '{_SHA256_PATTERN}'", name="parameter_value_citation_sha256"
        ),
    )


class ParameterValueCitationRun(Base, TimestampedUUID, Immutable):
    """One run of the passage that holds the number, in reading order."""

    __tablename__ = "parameter_value_citation_runs"

    citation_id: Mapped[UUID] = mapped_column(
        ForeignKey("parameter_value_citations.id", ondelete="RESTRICT"), index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), index=True
    )
    """The run itself; its text, polygon and crop stay on the candidate."""

    position: Mapped[int]
    """`0` upward, left to right along the passage."""

    __table_args__ = (
        CheckConstraint("position >= 0", name="parameter_value_citation_run_position"),
        UniqueConstraint("citation_id", "position", name="uq_parameter_value_citation_runs_slot"),
        UniqueConstraint(
            "citation_id", "candidate_id", name="uq_parameter_value_citation_runs_candidate"
        ),
    )
