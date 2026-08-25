"""Parameter sets and values, persisted (#303, C1.13).

The test that carries this story is the round trip. `Finding.parameter_set_ids` already records content
hashes, and ADR-0016 requires a finding to pin the exact parameters that judged it — so the stored `set_id`
has to be the *same* hash the in-memory set computes, and `1/8` has to come back as `Fraction(1, 8)` rather
than `0.125`. If either fails, the numbers behind a six-month-old verdict are unrecoverable.

Everything else here is the constraints, and each is tested by trying the thing it forbids. A check
constraint nobody has watched refuse anything is a check constraint that might not be attached.

Source: ADR-0006, ADR-0016 · Design: `docs/DESIGN.md` §3.9, §3.12 · Verification: this file
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import Package, ParameterSet, ParameterValue, Project
from app.models.parameters import from_rows, to_rows
from rules.parameters import ParameterLayer, Provenance
from rules.parameters import ParameterSet as InMemorySet
from rules.parameters import ParameterValue as InMemoryValue
from rules.schema import Quantity
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

WHEN = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    factory = session_factory(postgres_engine)
    with factory() as opened:
        yield opened
        opened.rollback()


def _project(session: Session) -> Project:
    project = Project(name="Ridgewood")
    session.add(project)
    session.flush()
    return project


def _value(value: str = "1/8", unit: str = "in") -> InMemoryValue:
    return InMemoryValue(
        value=Quantity(value=value, unit=unit),
        provenance=Provenance.MEASURED,
        set_by="raj",
        set_at=WHEN,
    )


def _in_memory(project_id: str | None, layer: ParameterLayer, version: int = 1) -> InMemorySet:
    return InMemorySet(
        project_id=project_id,
        layer=layer,
        version=version,
        parameters={"filler_minimum": _value("1/8"), "field_cut": _value("1/4")},
    )


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_the_stored_set_id_equals_the_in_memory_content_hash(session: Session) -> None:
    """The first acceptance criterion, and the reason the table exists.

    `Finding.parameter_set_ids` records this hash. If the stored one differed from what the value object
    computes, a finding would cite an identifier nothing could resolve — and the failure would be silent,
    because both sides would look internally consistent.
    """
    project = _project(session)
    original = _in_memory(str(project.id), ParameterLayer.PROJECT)

    stored, values = to_rows(original)
    session.add(stored)
    session.add_all(values)
    session.flush()

    assert stored.set_id == original.set_id

    reread = session.get(ParameterSet, stored.id)
    assert reread is not None
    assert reread.set_id == original.set_id


def test_the_set_round_trips_and_recomputes_the_same_hash(session: Session) -> None:
    """**Recomputed after the round trip, not read back.**

    Reading the stored hash and comparing it with itself would pass however badly the values were stored.
    This rebuilds the value object from the rows and recomputes — so a lost fraction, a dropped
    provenance or a shifted timestamp all change the hash and fail here.
    """
    project = _project(session)
    original = _in_memory(str(project.id), ParameterLayer.PROJECT)

    stored, values = to_rows(original)
    session.add(stored)
    session.add_all(values)
    session.flush()
    session.expire_all()

    rebuilt = from_rows(
        session.get(ParameterSet, stored.id),  # type: ignore[arg-type]
        list(session.query(ParameterValue).filter_by(parameter_set_id=stored.id).all()),
    )

    assert rebuilt.set_id == original.set_id, "the recomputed hash differs from the original"
    assert rebuilt == original


def test_an_exact_fraction_survives_as_a_fraction(session: Session) -> None:
    """`1/8` returns as `Fraction(1, 8)`, never `0.125` — the fifth acceptance criterion.

    A float column would lose the authored form, and `1/3` has no decimal form at all. `AGENTS.md` §2.4
    forbids a float in the decision path, and a parameter is squarely in it.
    """
    project = _project(session)
    original = InMemorySet(
        project_id=str(project.id),
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={"third": _value("1/3"), "eighth": _value("1/8")},
    )

    stored, values = to_rows(original)
    session.add(stored)
    session.add_all(values)
    session.flush()
    session.expire_all()

    by_name = {
        v.name: v for v in session.query(ParameterValue).filter_by(parameter_set_id=stored.id)
    }
    assert by_name["eighth"].exact_value == Fraction(1, 8)
    assert by_name["third"].exact_value == Fraction(1, 3)
    assert not isinstance(by_name["third"].exact_value, float)


def test_the_stored_pair_is_normalised(session: Session) -> None:
    """`2/16` and `1/8` store identically, so two logically equal sets cannot hash differently.

    `Fraction` normalises on construction, and this asserts the property rather than assuming the library
    keeps doing it.
    """
    project = _project(session)
    unreduced = InMemorySet(
        project_id=str(project.id),
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={"filler_minimum": _value("2/16")},
    )
    reduced = InMemorySet(
        project_id=str(project.id),
        layer=ParameterLayer.PROJECT,
        version=2,
        parameters={"filler_minimum": _value("1/8")},
    )

    _, unreduced_values = to_rows(unreduced)
    _, reduced_values = to_rows(reduced)

    assert (unreduced_values[0].numerator, unreduced_values[0].denominator) == (1, 8)
    assert (reduced_values[0].numerator, reduced_values[0].denominator) == (1, 8)


def test_provenance_and_author_survive_the_round_trip(session: Session) -> None:
    """The fourth criterion: who set it, when, and under which provenance.

    All three are inside the content hash, so losing any of them would already fail the round trip — but
    this asserts them directly, because "the hash matched" is a weaker statement than "the reviewer can
    see who set this number".
    """
    project = _project(session)
    original = _in_memory(str(project.id), ParameterLayer.PROJECT)

    stored, values = to_rows(original)
    session.add(stored)
    session.add_all(values)
    session.flush()
    session.expire_all()

    row = session.query(ParameterValue).filter_by(parameter_set_id=stored.id).first()
    assert row is not None
    assert row.set_by == "raj"
    assert row.provenance == Provenance.MEASURED.value
    assert row.set_at == WHEN


# ---------------------------------------------------------------------------
# One version names one set of numbers
# ---------------------------------------------------------------------------


def test_project_layer_and_version_are_unique(session: Session) -> None:
    """The third criterion, mirroring `ParameterSetStore.ParameterSetConflictError`.

    Two rows claiming project X's layer `project` version 3 would make "the parameters that judged this"
    ambiguous — and a finding citing version 3 could not say which numbers it meant.
    """
    project = _project(session)
    first, _ = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT, version=3))
    session.add(first)
    session.flush()

    clashing = ParameterSet(
        set_id="sha256:" + "b" * 64,
        project_id=project.id,
        layer=ParameterLayer.PROJECT.value,
        version=3,
    )
    session.add(clashing)
    with pytest.raises(IntegrityError, match="uq_parameter_sets_project_layer_version"):
        session.flush()


def test_two_sets_cannot_share_a_content_hash(session: Session) -> None:
    """The hash *is* the identity of the numbers. Two rows sharing one is the same set stored twice."""
    project = _project(session)
    digest = "sha256:" + "c" * 64

    session.add(
        ParameterSet(
            set_id=digest, project_id=project.id, layer=ParameterLayer.PROJECT.value, version=1
        )
    )
    session.flush()
    session.add(
        ParameterSet(
            set_id=digest, project_id=project.id, layer=ParameterLayer.PROJECT.value, version=2
        )
    )
    with pytest.raises(IntegrityError, match="uq_parameter_sets_set_id"):
        session.flush()


def test_a_set_id_that_is_not_a_digest_is_refused(session: Session) -> None:
    """A finding cites this string. A free-form value would let `"latest"` be stored and pinned."""
    project = _project(session)
    session.add(
        ParameterSet(
            set_id="latest", project_id=project.id, layer=ParameterLayer.PROJECT.value, version=1
        )
    )
    with pytest.raises(IntegrityError, match="ck_parameter_sets_set_id_is_a_digest"):
        session.flush()


def test_a_global_layer_may_not_name_a_project(session: Session) -> None:
    """The defaults belong to no project, and a project-scoped one must name its project.

    Either mistake makes the resolver's precedence order silently wrong for exactly one project — which
    is the kind of error that shows up as one package behaving differently from the rest.
    """
    project = _project(session)
    session.add(
        ParameterSet(
            set_id="sha256:" + "d" * 64,
            project_id=project.id,
            layer=ParameterLayer.GLOBAL.value,
            version=1,
        )
    )
    with pytest.raises(IntegrityError, match="ck_parameter_sets_global_has_no_project"):
        session.flush()


def test_a_project_layer_must_name_a_project(session: Session) -> None:
    session.add(
        ParameterSet(
            set_id="sha256:" + "e" * 64,
            project_id=None,
            layer=ParameterLayer.PROJECT.value,
            version=1,
        )
    )
    with pytest.raises(IntegrityError, match="ck_parameter_sets_global_has_no_project"):
        session.flush()


def test_a_layer_outside_the_vocabulary_is_refused(session: Session) -> None:
    """A set written under a layer the resolver does not know is a set it silently never consults.

    The row names a project on purpose. My first version passed `project_id=None`, which also violates
    `global_has_no_project` — PostgreSQL reported that constraint instead, so the test failed while the
    behaviour was right. A row that breaks two constraints proves nothing about either.
    """
    project = _project(session)
    session.add(
        ParameterSet(
            set_id="sha256:" + "f" * 64, project_id=project.id, layer="whatever", version=1
        )
    )
    with pytest.raises(IntegrityError, match="ck_parameter_sets_layer_in_vocabulary"):
        session.flush()


def test_the_constraint_vocabularies_still_match_the_code(session: Session) -> None:
    """The migration writes the layers and provenances out; `rules/parameters.py` defines them.

    Two lists drift, and the copy that drifts is the one nobody is looking at. Same argument
    `app/models/rules.py` makes about the reserved discriminators — so this asserts they still agree
    rather than trusting a comment.
    """
    del session
    # Loaded by path: a module whose name starts with a digit cannot be imported by name, and every
    # migration in this project is named that way.
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "alembic/versions/0027_parameter_sets.py"
    spec = importlib.util.spec_from_file_location("migration_0027", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert set(migration._LAYERS) == {layer.value for layer in ParameterLayer}
    assert set(migration._PROVENANCES) == {p.value for p in Provenance}


# ---------------------------------------------------------------------------
# Immutable: a changed value is a new set
# ---------------------------------------------------------------------------


def test_a_parameter_set_cannot_be_updated(session: Session) -> None:
    """The second acceptance criterion, enforced by the database rather than by convention.

    Editing a value in place would change the numbers behind every finding that already cited the set —
    silently, and after the fact.
    """
    project = _project(session)
    stored, values = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT))
    session.add(stored)
    session.add_all(values)
    session.commit()

    with pytest.raises(DBAPIError, match="append-only|gv_reject_mutation"):
        session.execute(
            text("UPDATE parameter_sets SET version = 99 WHERE id = :id"), {"id": stored.id}
        )
    session.rollback()


def test_a_parameter_value_cannot_be_updated(session: Session) -> None:
    project = _project(session)
    stored, values = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT))
    session.add(stored)
    session.add_all(values)
    session.commit()

    with pytest.raises(DBAPIError, match="append-only|gv_reject_mutation"):
        session.execute(
            text("UPDATE parameter_values SET numerator = 7 WHERE id = :id"), {"id": values[0].id}
        )
    session.rollback()


def test_a_parameter_value_cannot_be_deleted(session: Session) -> None:
    """Deleting one value would leave a set whose content no longer hashes to its own `set_id`."""
    project = _project(session)
    stored, values = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT))
    session.add(stored)
    session.add_all(values)
    session.commit()

    with pytest.raises(DBAPIError, match="append-only|gv_reject_mutation"):
        session.execute(text("DELETE FROM parameter_values WHERE id = :id"), {"id": values[0].id})
    session.rollback()


def test_changing_a_value_produces_a_new_set_with_a_new_hash(session: Session) -> None:
    """The criterion stated positively: the way to change a number is to write a new set."""
    project = _project(session)
    first = _in_memory(str(project.id), ParameterLayer.PROJECT, version=1)
    second = InMemorySet(
        project_id=str(project.id),
        layer=ParameterLayer.PROJECT,
        version=2,
        parameters={"filler_minimum": _value("3/16"), "field_cut": _value("1/4")},
    )

    assert first.set_id != second.set_id

    for parameters in (first, second):
        stored, values = to_rows(parameters)
        session.add(stored)
        session.add_all(values)
    session.flush()

    assert session.query(ParameterSet).count() == 2


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def test_a_zero_denominator_is_refused(session: Session) -> None:
    """A zero denominator is not a number; it would raise on every read instead of at write time."""
    project = _project(session)
    stored, _ = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT))
    session.add(stored)
    session.flush()

    session.add(
        ParameterValue(
            parameter_set_id=stored.id,
            name="broken",
            numerator=1,
            denominator=0,
            unit="in",
            provenance=Provenance.MEASURED.value,
            set_by="raj",
            set_at=WHEN,
        )
    )
    with pytest.raises(IntegrityError, match="ck_parameter_values_denominator_positive"):
        session.flush()


def test_one_name_appears_once_per_set(session: Session) -> None:
    """Two rows for `filler_minimum` in one set would make the resolver's answer depend on row order."""
    project = _project(session)
    stored, values = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT))
    session.add(stored)
    session.add_all(values)
    session.flush()

    session.add(
        ParameterValue(
            parameter_set_id=stored.id,
            name="filler_minimum",
            numerator=9,
            denominator=16,
            unit="in",
            provenance=Provenance.MEASURED.value,
            set_by="raj",
            set_at=WHEN,
        )
    )
    with pytest.raises(IntegrityError, match="uq_parameter_values_set_name"):
        session.flush()


def test_a_value_with_no_author_is_refused(session: Session) -> None:
    """A parameter with no author is a number nobody can be asked about."""
    project = _project(session)
    stored, _ = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT))
    session.add(stored)
    session.flush()

    session.add(
        ParameterValue(
            parameter_set_id=stored.id,
            name="anonymous",
            numerator=1,
            denominator=8,
            unit="in",
            provenance=Provenance.MEASURED.value,
            set_by="",
            set_at=WHEN,
        )
    )
    with pytest.raises(IntegrityError, match="ck_parameter_values_set_by_present"):
        session.flush()


# ---------------------------------------------------------------------------
# The foreign key #192 had to drop
# ---------------------------------------------------------------------------


def test_a_project_may_name_a_company_standard_set(session: Session) -> None:
    """The sixth criterion. The column existed since 0003 with nothing to point at."""
    project = _project(session)
    standards, values = to_rows(_in_memory(None, ParameterLayer.GLOBAL))
    session.add(standards)
    session.add_all(values)
    session.flush()

    project.company_standards_id = standards.id
    session.flush()
    session.expire_all()

    assert session.get(Project, project.id).company_standards_id == standards.id  # type: ignore[union-attr]


def test_a_project_cannot_name_a_set_that_does_not_exist(session: Session) -> None:
    """What the foreign key buys: before 0027 this succeeded and left a dangling pointer."""
    project = _project(session)
    project.company_standards_id = uuid4()

    with pytest.raises(IntegrityError, match="fk_projects_company_standards_id_parameter_sets"):
        session.flush()


def test_a_named_parameter_set_cannot_be_deleted(session: Session) -> None:
    """`RESTRICT`. The numbers behind a project's findings are what that pointer is for."""
    project = _project(session)
    standards, values = to_rows(_in_memory(None, ParameterLayer.GLOBAL))
    session.add(standards)
    session.add_all(values)
    session.flush()
    project.company_standards_id = standards.id
    session.commit()

    # The append-only trigger refuses first, which is the stronger guarantee — but assert the row
    # survives either way, because that is the property that matters.
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM parameter_sets WHERE id = :id"), {"id": standards.id})
    session.rollback()
    assert session.get(ParameterSet, standards.id) is not None


def test_deleting_a_project_that_owns_parameters_is_refused(session: Session) -> None:
    """`RESTRICT` on the other side: findings outlive the project record and still cite these hashes."""
    project = _project(session)
    stored, values = to_rows(_in_memory(str(project.id), ParameterLayer.PROJECT))
    session.add(stored)
    session.add_all(values)
    session.add(Package(project_id=project.id, vendor=None))
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project.id})
    session.rollback()
