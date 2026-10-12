"""The sink a project uses: its name, entered once and read by every sink check (#1181).

Three checks need the sink's own inside size — the cut-out width, the cut-out depth and, through the
cut-out, the sink cabinet — and that size is on no shop drawing: it is on the sink's spec sheet
(`CLIENT_FACTS` Q7, "the reviewer will input the necessary values"). Until #1181 a reviewer had to
find two run-only fields on Measurements for every package, so on real sets the checks waited.

**The numbers are not stored here.** The inside width and depth (and an optional front offset for
this sink) are written to the project's own parameter layer, exactly as a setting typed on
Measurements is (`app/api/measurements.py:_store`): with who, when, the source and a reference that
names the sink. That is the layer every check already reads, so the sink feeds all of them without a
second path into the arithmetic. This row adds only what a number cannot hold: the sink's name, the
reviewer's note, and which version of the project's settings the entry was saved as.

**Append-only**, like every record table. A different sink is a new row; the latest is the one in use.

Source: issue #1181. Verification: `tests/api/test_waiting_values.py`.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID

__all__ = ["MODEL_NAME_MAX_LENGTH", "SOURCE_NOTE_MAX_LENGTH", "ProjectSink"]

#: A sink's model name: long enough for a maker and a model number, short enough to stay a name.
MODEL_NAME_MAX_LENGTH = 80

#: The reviewer's note on where the size came from. Kept short so the reference written beside each
#: number ("Sink <name>: <note>") fits the 200-character limit every setting reference has.
SOURCE_NOTE_MAX_LENGTH = 100


class ProjectSink(Base, TimestampedUUID, Immutable):
    """One sink entry for one project. The newest row is the sink in use."""

    __tablename__ = "project_sinks"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(MODEL_NAME_MAX_LENGTH))
    """What the reviewer calls the sink, from its spec sheet. A name, never a size."""

    source_note: Mapped[str | None] = mapped_column(String(SOURCE_NOTE_MAX_LENGTH), nullable=True)
    """Where the size was read, in the reviewer's words. Null when they gave none."""

    parameter_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("parameter_sets.id", ondelete="RESTRICT"), index=True
    )
    """The project settings version the sink's numbers were saved in."""

    set_by: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint("model_name !~ '^[[:space:]]*$'", name="project_sink_model_name_present"),
        CheckConstraint(
            "source_note IS NULL OR source_note !~ '^[[:space:]]*$'",
            name="project_sink_source_note_not_blank",
        ),
        CheckConstraint("set_by <> ''", name="project_sink_set_by_present"),
    )
