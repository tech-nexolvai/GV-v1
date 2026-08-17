"""Gold case storage, loading and the integrity check (#187, B1.2).

Every metric in the system is measured against the gold set, so the tests that matter here are the
refusals. An annotation is a statement about specific bytes; against different bytes the value, the
page and the polygon can all be wrong at once while the case still scores — and the result reads as
a passing gate rather than a broken one.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from eval.gold_set.schema import (
    GoldCase,
    GoldManifest,
    GroundTruth,
    Provenance,
    ReviewedDocument,
)
from eval.gold_set.store import (
    MissingDrawing,
    StaleAnnotation,
    UnverifiableDocument,
    content_hash,
    load_cases,
    stale,
    verify,
)
from rules.semantic_types import OperandSource, ProductType


def _hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _write(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _case(
    root: Path,
    *,
    case_id: str = "CT-001",
    arch: bytes = b"architectural drawing",
    shop: bytes = b"shop drawing",
    arch_hash: str | None = None,
    shop_hash: str | None = None,
) -> GoldCase:
    _write(root, "arch.pdf", arch)
    _write(root, "shop.pdf", shop)
    return GoldCase(
        id=case_id,
        product_type=ProductType.COUNTERTOP,
        arch=Path("arch.pdf"),
        shop=Path("shop.pdf"),
        ground_truth=GroundTruth(observations=(), matches=(), expected_findings=()),
        provenance=Provenance(
            annotator="anant",
            annotated_on=date(2026, 8, 17),
            documents=(
                ReviewedDocument(
                    source=OperandSource.ARCH,
                    document_version_id=uuid4(),
                    content_hash=arch_hash or _hash(arch),
                ),
                ReviewedDocument(
                    source=OperandSource.SHOP,
                    document_version_id=uuid4(),
                    content_hash=shop_hash or _hash(shop),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# The integrity check
# ---------------------------------------------------------------------------


def test_a_case_whose_drawings_are_unchanged_verifies(tmp_path: Path) -> None:
    """The check has to be able to say yes, or the refusals below prove nothing."""
    verify(_case(tmp_path), root=tmp_path)


def test_a_changed_shop_drawing_is_refused(tmp_path: Path) -> None:
    """The failure this module exists for. The annotation still parses; it is simply no longer about
    the bytes on disk, and scoring it would produce a confidently wrong metric."""
    case = _case(tmp_path)
    _write(tmp_path, "shop.pdf", b"a different revision entirely")
    with pytest.raises(StaleAnnotation, match="SHOP drawing"):
        verify(case, root=tmp_path)


def test_a_changed_architectural_drawing_is_refused(tmp_path: Path) -> None:
    """The gap that prompted the schema change. `GoldMatch` pairs an `arch_item` with a `shop_item`,
    so the architectural drawing is annotated too — and one hash for the whole case would have let
    this pass while every match was silently invalid."""
    case = _case(tmp_path)
    _write(tmp_path, "arch.pdf", b"a different architectural set")
    with pytest.raises(StaleAnnotation, match="ARCH drawing"):
        verify(case, root=tmp_path)


def test_the_refusal_names_the_file_and_both_hashes(tmp_path: Path) -> None:
    """ "Something changed" sends somebody hunting. The file, the annotator and the two hashes let
    them go straight to it."""
    case = _case(tmp_path)
    _write(tmp_path, "shop.pdf", b"changed")
    with pytest.raises(StaleAnnotation) as raised:
        verify(case, root=tmp_path)
    message = str(raised.value)
    assert "shop.pdf" in message
    assert "annotated against:" in message and "on disk now:" in message
    assert "anant" in message


def test_a_missing_drawing_is_a_different_failure_from_a_changed_one(tmp_path: Path) -> None:
    """A changed drawing means the annotation is invalid; an absent one means the case was never
    loadable. The fixes differ — re-review the package, or go and find the file."""
    case = _case(tmp_path)
    (tmp_path / "shop.pdf").unlink()
    with pytest.raises(MissingDrawing):
        verify(case, root=tmp_path)


def test_a_source_with_no_path_on_the_case_is_refused(tmp_path: Path) -> None:
    """`PRODUCT_SPEC` is a hashed artifact under ADR-0015, and a gold case has nowhere to record its
    path. Recording a hash for bytes nothing can locate would claim a check that never runs."""
    case = _case(tmp_path)
    spec = ReviewedDocument(
        source=OperandSource.PRODUCT_SPEC,
        document_version_id=uuid4(),
        content_hash=_hash(b"cut sheet"),
    )
    with_spec = case.model_copy(
        update={"provenance": case.provenance.model_copy(update={"documents": (spec,)})}
    )
    with pytest.raises(UnverifiableDocument, match="PRODUCT_SPEC"):
        verify(with_spec, root=tmp_path)


def test_content_hash_matches_the_storage_dialect(tmp_path: Path) -> None:
    """One dialect across the system: a gold case's hash and a stored artifact's are produced by the
    same code, so they can be compared without translation."""
    path = _write(tmp_path, "x.pdf", b"bytes")
    assert content_hash(path) == _hash(b"bytes")
    assert content_hash(path).startswith("sha256:")


# ---------------------------------------------------------------------------
# The schema the check depends on
# ---------------------------------------------------------------------------


def test_a_provenance_with_no_documents_is_refused() -> None:
    """A case bound to nothing cannot be checked at all."""
    with pytest.raises(ValueError):
        Provenance(annotator="a", annotated_on=date(2026, 8, 17), documents=())


def test_two_documents_for_one_source_are_refused() -> None:
    """An observation names its source, so a repeated one leaves no way to say which bytes it came
    from."""
    document = ReviewedDocument(
        source=OperandSource.SHOP, document_version_id=uuid4(), content_hash=_hash(b"a")
    )
    with pytest.raises(ValueError, match="same source"):
        Provenance(
            annotator="a",
            annotated_on=date(2026, 8, 17),
            documents=(document, document.model_copy(update={"content_hash": _hash(b"b")})),
        )


@pytest.mark.parametrize("source", [OperandSource.LITERAL, OperandSource.USER_INPUT])
def test_a_source_with_no_bytes_cannot_carry_a_hash(source: OperandSource) -> None:
    """A literal lives in a rule and a user input is what somebody typed. Binding either to a content
    hash would claim a provenance that cannot be re-checked."""
    with pytest.raises(ValueError, match="no document to hash"):
        ReviewedDocument(source=source, document_version_id=uuid4(), content_hash=_hash(b"x"))


@pytest.mark.parametrize(
    "bad", ["a" * 64, "sha256:" + "A" * 64, "sha256:abc", "md5:" + "a" * 32, "sha256:reviewed"]
)
def test_a_hash_outside_the_canonical_form_is_refused(bad: str) -> None:
    """The prefix names the algorithm. Two dialects would mean a stored artifact and its gold case
    could not be compared without somebody translating between them."""
    with pytest.raises(ValueError, match="sha256:"):
        ReviewedDocument(source=OperandSource.SHOP, document_version_id=uuid4(), content_hash=bad)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path, *case_ids: str) -> Path:
    import yaml

    cases = [_case(tmp_path, case_id=case_id) for case_id in case_ids]
    manifest = GoldManifest(version=0, cases=tuple(cases))
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8")
    return path


def test_cases_load_in_a_deterministic_order(tmp_path: Path) -> None:
    """Two runs of the harness have to be comparable. An order that depends on how the manifest was
    written makes a difference in the results attributable to nothing."""
    path = _manifest(tmp_path, "CT-003", "CT-001", "CT-002")
    loaded = load_cases(path, cases_directory=tmp_path)
    assert [case.id for case in loaded] == ["CT-001", "CT-002", "CT-003"]


def test_loading_verifies_every_case(tmp_path: Path) -> None:
    """Malformed or stale data fails at load, not at scoring time — the acceptance criterion, and the
    reason a metric never gets computed from a case nobody can vouch for."""
    path = _manifest(tmp_path, "CT-001")
    _write(tmp_path, "shop.pdf", b"changed after the manifest was written")
    with pytest.raises(StaleAnnotation):
        load_cases(path, cases_directory=tmp_path)


def test_verification_can_be_skipped_only_deliberately(tmp_path: Path) -> None:
    """For listing what a gold set contains on a machine that does not hold the proprietary files.
    Scoring must never use it, which is why it defaults to on and is named for what it turns off."""
    path = _manifest(tmp_path, "CT-001")
    (tmp_path / "shop.pdf").unlink()
    assert len(load_cases(path, cases_directory=tmp_path, verify_integrity=False)) == 1


def test_stale_reports_everything_rather_than_the_first_failure(tmp_path: Path) -> None:
    """For a person asking what is broken, rather than a harness asking whether it may score. One
    case fixed per run is a slow way to learn the drawings were re-exported."""
    good = _case(tmp_path, case_id="CT-001")
    (tmp_path / "other").mkdir(exist_ok=True)
    broken = _case(tmp_path, case_id="CT-002", shop_hash=_hash(b"never written"))
    assert stale([good], root=tmp_path) == []
    assert stale([good, broken], root=tmp_path) == ["CT-002: StaleAnnotation"]
