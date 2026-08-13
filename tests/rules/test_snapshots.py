"""A snapshot identifier ties a manufacturing decision to the rule that made it.

The properties worth testing are therefore about stability and tamper-detection, not about
happy-path construction: the same rule must always yield the same identifier, any change must
yield a different one, and an altered snapshot must be detectable after the fact.
"""

from __future__ import annotations

import json

import pytest

from rules.schema import (
    Applicability,
    ApplicabilityVariant,
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
    Tolerance,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import (
    RuleSnapshot,
    SnapshotConflictError,
    SnapshotIntegrityError,
    SnapshotStore,
    canonical_json,
    compute_snapshot_id,
    publish,
)
from units.measurement import Unit
from verdict.outcomes import Severity


def _rule(**overrides: object) -> Rule:
    base: dict[str, object] = {
        "id": "CT-WIDTH-001",
        "version": "1.0.0",
        "product_type": ProductType.COUNTERTOP,
        "check_type": CheckType.INTERNAL,
        # Required since ADR-0007: a rule states its applicability rather than omitting it.
        "applicability": GlobalApplicability(scope="global"),
        "severity": Severity.CRITICAL,
        "arithmetic_unit": Unit.MM,
        "inputs": {
            "countertop_width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
            ),
            "cabinet_width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.CABINET_WIDTH,
            ),
        },
        "operation": OperationRef(
            type="within_tolerance",
            operands={"actual": "countertop_width"},
            tolerance=Tolerance(value="1/8", unit=Unit.INCH),
        ),
    }
    base.update(overrides)
    return Rule(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_rule_always_yields_the_same_identifier() -> None:
    assert compute_snapshot_id(_rule()) == compute_snapshot_id(_rule())


def test_publishing_twice_is_idempotent() -> None:
    """Republishing unchanged content must not create a second snapshot."""
    assert publish(_rule()).snapshot_id == publish(_rule()).snapshot_id


def test_identifier_is_full_length_and_names_its_algorithm() -> None:
    """Untruncated on purpose: this identifies a manufacturing decision."""
    snapshot_id = compute_snapshot_id(_rule())
    algorithm, _, digest = snapshot_id.partition(":")
    assert algorithm == "sha256"
    assert len(digest) == 64
    assert digest == digest.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "1.0.1"),
        ("severity", Severity.MAJOR),
        ("product_type", ProductType.CABINET),
        ("arithmetic_unit", Unit.INCH),
        ("description", "changed"),
    ],
)
def test_any_change_yields_a_different_identifier(field: str, value: object) -> None:
    assert compute_snapshot_id(_rule()) != compute_snapshot_id(_rule(**{field: value}))


def test_a_changed_tolerance_changes_the_identifier() -> None:
    """The single most important case. A tolerance edit must never be invisible."""
    tighter = OperationRef(
        type="within_tolerance",
        operands={"actual": "countertop_width"},
        tolerance=Tolerance(value="1/16", unit=Unit.INCH),
    )
    assert compute_snapshot_id(_rule()) != compute_snapshot_id(_rule(operation=tighter))


def test_a_changed_applicability_variant_changes_the_identifier() -> None:
    with_variants = Applicability(
        discriminator="wall_config",
        variants=(
            ApplicabilityVariant(
                when="back_left_right",
                tolerance=Tolerance(value="1/8", unit=Unit.INCH),
                extras={"field_cut_count": 2},
            ),
        ),
    )
    changed = Applicability(
        discriminator="wall_config",
        variants=(
            ApplicabilityVariant(
                when="back_left_right",
                tolerance=Tolerance(value="1/8", unit=Unit.INCH),
                extras={"field_cut_count": 1},  # one field cut, not two
            ),
        ),
    )
    assert compute_snapshot_id(_rule(applicability=with_variants)) != compute_snapshot_id(
        _rule(applicability=changed)
    )


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def test_canonical_json_sorts_keys() -> None:
    """Decouples the identifier from field declaration order in rules/schema.py.

    Without this, reordering fields — a cosmetic edit — would change every stored identifier
    and orphan every finding that referenced one.
    """
    body = canonical_json(_rule())
    keys = list(json.loads(body).keys())
    assert keys == sorted(keys)


def test_canonical_json_sorts_nested_dictionaries_too() -> None:
    """`inputs` keeps the order it was written in, so it must be sorted as well."""
    inputs = json.loads(canonical_json(_rule()))["inputs"]
    assert list(inputs.keys()) == sorted(inputs.keys())


def test_input_declaration_order_does_not_affect_the_identifier() -> None:
    """Two logically identical rules written in a different order are the same rule."""
    forwards = _rule()
    backwards = _rule(
        inputs={
            "cabinet_width": InputSelector(
                source=OperandSource.SHOP, semantic_type=SemanticType.CABINET_WIDTH
            ),
            "countertop_width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
            ),
        }
    )
    assert compute_snapshot_id(forwards) == compute_snapshot_id(backwards)


def test_canonical_json_has_no_insignificant_whitespace() -> None:
    body = canonical_json(_rule())
    assert ", " not in body
    assert ": " not in body
    assert "\n" not in body


def test_a_fraction_survives_as_an_exact_string_not_a_float() -> None:
    """RFC 8785's number normalisation does not apply because ADR-0001 forbids floats.
    A tolerance must never appear in the hashed bytes as 0.125."""
    body = canonical_json(_rule())
    assert '"1/8"' in body
    assert "0.125" not in body


# ---------------------------------------------------------------------------
# Immutability and tamper detection
# ---------------------------------------------------------------------------


def test_a_snapshot_cannot_be_mutated() -> None:
    snapshot = publish(_rule())
    with pytest.raises((AttributeError, TypeError)):
        snapshot.snapshot_id = "sha256:0000"  # type: ignore[misc]


def test_the_rule_inside_a_snapshot_cannot_be_mutated() -> None:
    from pydantic import ValidationError

    snapshot = publish(_rule())
    with pytest.raises(ValidationError):
        snapshot.rule.severity = Severity.MINOR  # type: ignore[misc]


def test_verify_accepts_an_untouched_snapshot() -> None:
    publish(_rule()).verify()


def test_verify_detects_a_snapshot_altered_at_rest() -> None:
    """The realistic attack: the stored bytes are edited but the identifier is left alone."""
    original = publish(_rule())
    tampered = RuleSnapshot(
        snapshot_id=original.snapshot_id,
        rule=original.rule,
        canonical_json=original.canonical_json.replace('"1/8"', '"1"'),
    )
    with pytest.raises(SnapshotIntegrityError, match="altered after publication"):
        tampered.verify()


def test_verify_checks_the_stored_bytes_not_the_current_model() -> None:
    """Deliberately hashes canonical_json as stored, so a future change to the model cannot
    retroactively alter the bytes of an already-published snapshot."""
    original = publish(_rule())
    assert original.canonical_json == canonical_json(original.rule)
    reconstructed = RuleSnapshot(
        snapshot_id=original.snapshot_id,
        rule=_rule(version="9.9.9"),  # a different rule entirely
        canonical_json=original.canonical_json,
    )
    reconstructed.verify()  # passes: the bytes are what is hashed


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_is_append_only_and_idempotent() -> None:
    store = SnapshotStore()
    first = store.add(publish(_rule()))
    second = store.add(publish(_rule()))
    assert first is second
    assert len(store) == 1


def test_store_keeps_both_versions_of_a_republished_rule() -> None:
    """An edit produces a new identifier, so both remain retrievable — which is what lets a
    finding name the exact snapshot that judged it."""
    store = SnapshotStore()
    store.add(publish(_rule()))
    store.add(publish(_rule(version="1.0.1")))
    assert len(store) == 2
    assert len(store.versions_of("CT-WIDTH-001")) == 2


def test_store_rejects_mismatched_content_under_an_existing_identifier() -> None:
    store = SnapshotStore()
    original = store.add(publish(_rule()))
    forged = RuleSnapshot(
        snapshot_id=original.snapshot_id,
        rule=original.rule,
        canonical_json=original.canonical_json.replace('"1/8"', '"1/4"'),
    )
    with pytest.raises((SnapshotConflictError, SnapshotIntegrityError)):
        store.add(forged)


def test_store_refuses_a_tampered_snapshot_on_entry() -> None:
    store = SnapshotStore()
    original = publish(_rule())
    tampered = RuleSnapshot(
        snapshot_id=original.snapshot_id,
        rule=original.rule,
        canonical_json=original.canonical_json + " ",
    )
    with pytest.raises(SnapshotIntegrityError):
        store.add(tampered)


def test_an_unknown_identifier_raises_rather_than_returning_none() -> None:
    """A finding referencing an unknown snapshot is an integrity problem, not a cache miss."""
    with pytest.raises(KeyError):
        SnapshotStore().get("sha256:" + "0" * 64)


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------


def test_a_finding_can_name_the_exact_snapshot() -> None:
    """The acceptance criterion: enough on the snapshot to identify it in a report."""
    snapshot = publish(_rule())
    assert snapshot.rule_id == "CT-WIDTH-001"
    assert snapshot.version == "1.0.0"
    assert snapshot.label.startswith("CT-WIDTH-001 1.0.0 (")
    assert snapshot.short_id in snapshot.snapshot_id
    assert len(snapshot.short_id) == 8


# ---------------------------------------------------------------------------
# One content hash per (rule id, version) — ADR-0006
# ---------------------------------------------------------------------------


def test_editing_a_published_rule_without_bumping_the_version_is_an_error() -> None:
    """The whole point. Two snapshots sharing a version leave "the highest version"
    naming two different rules, and the resolver with no defined way to choose."""
    from rules.snapshot import VersionConflictError

    store = SnapshotStore()
    store.add(publish(_rule()))
    edited = _rule(
        operation=OperationRef(
            type="within_tolerance",
            operands={"actual": "countertop_width"},
            tolerance=Tolerance(value="1/16", unit=Unit.INCH),  # tightened, version unchanged
        )
    )
    with pytest.raises(VersionConflictError) as err:
        store.add(publish(edited))
    assert "CT-WIDTH-001" in str(err.value)
    assert "1.0.0" in str(err.value)
    assert "bump the version" in str(err.value)


def test_bumping_the_version_is_the_way_through() -> None:
    store = SnapshotStore()
    store.add(publish(_rule()))
    tighter = _rule(
        version="1.0.1",
        operation=OperationRef(
            type="within_tolerance",
            operands={"actual": "countertop_width"},
            tolerance=Tolerance(value="1/16", unit=Unit.INCH),
        ),
    )
    store.add(publish(tighter))
    assert len(store) == 2


def test_republishing_identical_content_remains_idempotent() -> None:
    """The uniqueness check must not break the no-op case."""
    store = SnapshotStore()
    first = store.add(publish(_rule()))
    second = store.add(publish(_rule()))
    assert first is second
    assert len(store) == 1


def test_latest_returns_the_highest_version() -> None:
    store = SnapshotStore()
    store.add(publish(_rule(version="1.0.0")))
    newest = store.add(publish(_rule(version="2.1.0")))
    store.add(publish(_rule(version="1.9.3")))
    latest = store.latest("CT-WIDTH-001")
    assert latest is not None
    assert latest.snapshot_id == newest.snapshot_id


def test_latest_compares_versions_numerically_not_as_strings() -> None:
    """String ordering puts "1.0.10" below "1.0.9" — a silently wrong answer to
    "which is newest", and exactly the kind of bug that shows up only after ten releases."""
    store = SnapshotStore()
    store.add(publish(_rule(version="1.0.9")))
    tenth = store.add(publish(_rule(version="1.0.10")))
    latest = store.latest("CT-WIDTH-001")
    assert latest is not None
    assert latest.version == "1.0.10"
    assert latest.snapshot_id == tenth.snapshot_id


def test_latest_compares_the_minor_component_numerically_too() -> None:
    store = SnapshotStore()
    store.add(publish(_rule(version="1.9.0")))
    store.add(publish(_rule(version="1.10.0")))
    latest = store.latest("CT-WIDTH-001")
    assert latest is not None
    assert latest.version == "1.10.0"


def test_latest_for_an_unpublished_rule_returns_none() -> None:
    """A rule that does not exist yet is a normal state, not an integrity failure —
    the caller turns it into NO_APPLICABLE_RULE."""
    assert SnapshotStore().latest("CT-NEVER-PUBLISHED") is None


def test_two_different_rules_may_share_a_version() -> None:
    """The constraint is per rule id. Every rule starting at 1.0.0 is normal."""
    store = SnapshotStore()
    store.add(publish(_rule(id="CT-WIDTH-001")))
    store.add(publish(_rule(id="CT-DEPTH-002")))
    assert len(store) == 2
    assert store.latest("CT-WIDTH-001") is not None
    assert store.latest("CT-DEPTH-002") is not None
