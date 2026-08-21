"""Database contract for rule definitions, snapshots and applicability scopes (#198).

The point of these tables is that a finding can name the exact rule that produced it, years later,
and somebody can check that the row still says what it said. So the tests that matter are not "can a
row be written" — they are:

* **The stored id still matches the stored content.** Rehash `canonical_json` and compare. If that
  can drift, the identifier is decoration.
* **A snapshot cannot be edited.** `Immutable`, and asserted, because every finding cites one.
* **A version names one thing.** `latest()` selects by highest semver, which is meaningless if a
  version can be published twice with different content.
* **A vendor-keyed rule cannot be stored.** ADR-0006 forbids it at authoring time; this table would
  otherwise be the way in behind that.

Corrections to the issue's plan are recorded in `docs/decisions/C1_8_APPLICABILITY_SCOPE.md`.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import RuleApplicabilityScope, RuleDefinition, RuleSnapshot
from rules.schema import RESERVED_DISCRIMINATORS
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

RULE_TABLES = {"rule_definitions", "rule_snapshots", "rule_applicability_scopes"}

#: Stands in for the canonical JCS bytes `rules/snapshot.py` produces. The shape does not matter to
#: these tests; that the hash is taken over exactly these bytes does.
CANONICAL = '{"id":"CT-WIDTH-001","product_type":"countertop","version":"1.0.0"}'


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _seed(session: Session, rule_id: str = "CT-WIDTH-001") -> RuleDefinition:
    definition = RuleDefinition(rule_id=rule_id)
    session.add(definition)
    session.flush()
    return definition


def _snapshot_id(body: str) -> str:
    """The identity form `rules/snapshot.py` emits, prefix included."""
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def _definition(rule_id: str = "CT-WIDTH-001") -> RuleDefinition:
    return RuleDefinition(rule_id=rule_id)


def _snapshot(
    definition: RuleDefinition,
    *,
    version: str = "1.0.0",
    body: str = CANONICAL,
    unconfirmed: int = 0,
) -> RuleSnapshot:
    return RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=_snapshot_id(body),
        version=version,
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=unconfirmed,
    )


# ---------------------------------------------------------------------------
# Registration and immutability — no database required
# ---------------------------------------------------------------------------


def test_all_three_tables_are_registered() -> None:
    assert RULE_TABLES <= set(Base.metadata.tables)


@pytest.mark.parametrize("model", [RuleSnapshot, RuleApplicabilityScope])
def test_a_published_snapshot_and_its_scope_are_immutable(model: type) -> None:
    """What a rule applies to is part of what was published, so the scope is immutable for the same
    reason the snapshot is. A definition is not: its identity persists while versions accumulate."""
    assert issubclass(model, Immutable)


def test_a_definition_is_not_immutable() -> None:
    assert not issubclass(RuleDefinition, Immutable)


# ---------------------------------------------------------------------------
# The shape the plan got wrong (C1_8_APPLICABILITY_SCOPE.md)
# ---------------------------------------------------------------------------


def test_applicability_is_a_child_table_not_a_column_per_discriminator() -> None:
    """ADR-0007 rejected the column form: a rule keyed on `material` or `mount_type` could not be
    stored until somebody changed the shape, which in a database means a migration per
    discriminator — and a shipped migration may never be edited."""
    columns = set(Base.metadata.tables["rule_applicability_scopes"].columns.keys())
    assert {"discriminator", "value"} <= columns
    assert "wall_config" not in columns


def test_no_table_keys_a_rule_on_a_project() -> None:
    """ADR-0006 settles that rules are GV's own standards, and ADR-0007 carries project scope through
    the resolver "never used to filter". A project column on either table would quietly recreate
    per-project rule sets; project variation belongs in parameter sets (ADR-0009)."""
    for table in ("rule_definitions", "rule_snapshots", "rule_applicability_scopes"):
        assert "project_id" not in Base.metadata.tables[table].columns


def test_product_type_is_required() -> None:
    """A rule always has a category, and `CheckContext.product_type` is non-optional. The plan had it
    nullable, which would let a rule exist that matches nothing."""
    assert Base.metadata.tables["rule_snapshots"].columns["product_type"].nullable is False


def test_check_type_is_stored_but_not_on_the_applicability_scope() -> None:
    """It decides which documents load, not which rule applies to an item, so it is an attribute of
    the rule rather than a resolver key. The plan had it in the four-key list, where effective
    version belongs."""
    assert "check_type" in Base.metadata.tables["rule_snapshots"].columns
    assert "check_type" not in Base.metadata.tables["rule_applicability_scopes"].columns


def test_effective_version_lives_on_the_snapshot() -> None:
    assert "version" in Base.metadata.tables["rule_snapshots"].columns


def test_the_reserved_discriminator_check_matches_the_authoring_ban() -> None:
    """Two lists of forbidden names drift, and the one that drifts is the one nobody is reading when
    a vendor-keyed rule gets written. The constraint is built from `RESERVED_DISCRIMINATORS`; this
    asserts the rendering still covers every name in it."""
    constraints = Base.metadata.tables["rule_applicability_scopes"].constraints
    reserved_check = next(
        c for c in constraints if getattr(c, "name", "").endswith("discriminator_not_reserved")
    )
    expression = str(reserved_check.sqltext)  # type: ignore[attr-defined]
    for name in RESERVED_DISCRIMINATORS:
        assert f"'{name}'" in expression, f"{name} is banned at authoring time but not in the table"


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def test_the_stored_snapshot_id_still_matches_the_stored_content(postgres_engine: Engine) -> None:
    """The round trip that makes the identifier a fact rather than a promise. Read the row back,
    rehash `canonical_json`, and compare — a row that drifted from its own hash fails here."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        session.add(_snapshot(_seed(session)))
    with unit_of_work(factory) as session:
        stored = session.query(RuleSnapshot).one()
        assert stored.snapshot_id == _snapshot_id(stored.canonical_json)


def test_the_same_version_cannot_be_published_twice(postgres_engine: Engine) -> None:
    """`latest()` selects the highest semver, which only means something if a version names one
    thing. Two rows at 1.0.0 would make "the effective rule" a coin toss."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        session.add(_snapshot(_seed(session), version="1.0.0"))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        definition = session.query(RuleDefinition).one()
        session.add(
            _snapshot(definition, version="1.0.0", body=CANONICAL.replace("1.0.0", "9.9.9"))
        )


def test_identical_content_cannot_be_stored_under_two_rows(postgres_engine: Engine) -> None:
    """Publishing the same rule twice does not duplicate: the content hash is unique, so the second
    write is refused rather than creating a second identity for one rule."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        session.add(_snapshot(_seed(session), version="1.0.0"))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        definition = session.query(RuleDefinition).one()
        session.add(_snapshot(definition, version="2.0.0"))  # new version, identical bytes


def test_a_snapshot_id_without_its_algorithm_prefix_is_refused(postgres_engine: Engine) -> None:
    """The prefix names the hash. Dropping it would leave two indistinguishable families of
    identifier the day the algorithm changes."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        snapshot = _snapshot(_seed(session))
        snapshot.snapshot_id = hashlib.sha256(CANONICAL.encode("utf-8")).hexdigest()
        session.add(snapshot)


@pytest.mark.parametrize("reserved", sorted(RESERVED_DISCRIMINATORS))
def test_a_submitter_keyed_discriminator_is_refused_by_the_database(
    postgres_engine: Engine, reserved: str
) -> None:
    """ADR-0006 forbids keying a rule on who submitted the drawing. `rules/schema.py` enforces that
    at authoring time; without this constraint the table is the way in behind it, and a row written
    by any other path would carry a vendor-keyed rule the resolver would honour."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        snapshot = _snapshot(_seed(session))
        session.add(snapshot)
        session.flush()
        session.add(
            RuleApplicabilityScope(
                rule_snapshot_id=snapshot.id, discriminator=reserved, value="acme"
            )
        )


def test_one_snapshot_carries_several_discriminators(postgres_engine: Engine) -> None:
    """The property the column form could not have: two discriminators, no schema change."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        snapshot = _snapshot(_seed(session))
        session.add(snapshot)
        session.flush()
        session.add_all(
            [
                RuleApplicabilityScope(
                    rule_snapshot_id=snapshot.id,
                    discriminator="wall_config",
                    value="back_left_right",
                ),
                RuleApplicabilityScope(
                    rule_snapshot_id=snapshot.id, discriminator="material", value="quartz"
                ),
            ]
        )
    with unit_of_work(factory) as session:
        stored = {row.discriminator for row in session.query(RuleApplicabilityScope).all()}
        assert stored == {"wall_config", "material"}


def test_the_same_discriminator_value_cannot_be_recorded_twice(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        snapshot = _snapshot(_seed(session))
        session.add(snapshot)
        session.flush()
        session.add(
            RuleApplicabilityScope(
                rule_snapshot_id=snapshot.id, discriminator="wall_config", value="galley"
            )
        )
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        existing = session.query(RuleSnapshot).one()
        session.add(
            RuleApplicabilityScope(
                rule_snapshot_id=existing.id, discriminator="wall_config", value="galley"
            )
        )


def test_a_rule_with_an_unconfirmed_tolerance_is_findable_by_query(postgres_engine: Engine) -> None:
    """The acceptance criterion that matters for the release gate: an unreleasable rule has to be
    visible without parsing every snapshot's JSON. Authoring may run ahead of the client, but the
    gate must be able to see which rules can only ever return REVIEW REQUIRED."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        session.add(_snapshot(_seed(session, "CT-WIDTH-001"), unconfirmed=0))
        pending = _seed(session, "CT-OVERHANG-002")
        session.add(
            _snapshot(pending, body=CANONICAL.replace("WIDTH-001", "OVERHANG-002"), unconfirmed=2)
        )
    with unit_of_work(factory) as session:
        unreleasable = (
            session.query(RuleSnapshot).filter(RuleSnapshot.unconfirmed_tolerance_count > 0).all()
        )
        assert [row.unconfirmed_tolerance_count for row in unreleasable] == [2]


def test_a_negative_unconfirmed_count_is_refused(postgres_engine: Engine) -> None:
    """A count that can go negative is a count nobody can reason about."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add(_snapshot(_seed(session), unconfirmed=-1))


def test_a_snapshot_cannot_be_deleted_while_a_scope_references_it(postgres_engine: Engine) -> None:
    """`RESTRICT`, not cascade. A scope row is part of what was published; removing the snapshot and
    silently taking its scope with it would erase the record a finding cites."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        snapshot = _snapshot(_seed(session))
        session.add(snapshot)
        session.flush()
        session.add(
            RuleApplicabilityScope(
                rule_snapshot_id=snapshot.id, discriminator="wall_config", value="galley"
            )
        )
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.delete(session.query(RuleSnapshot).one())


def test_an_unknown_definition_cannot_own_a_snapshot(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add(
            RuleSnapshot(
                rule_definition_id=uuid4(),
                snapshot_id=_snapshot_id(CANONICAL),
                version="1.0.0",
                canonical_json=CANONICAL,
                product_type="countertop",
                check_type="internal",
                unconfirmed_tolerance_count=0,
            )
        )
