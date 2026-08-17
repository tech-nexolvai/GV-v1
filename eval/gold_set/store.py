"""Load gold cases and refuse the ones whose drawings have changed underneath them.

Phase 0 of the build order is the gold set, and every metric in the system is measured against it.
`schema.py` (`#129`) defines the format and parses it; this binds each case to the exact bytes it was
annotated against, and loads them in an order two runs can agree on.

**The failure this exists to prevent.** An annotation is a statement about specific bytes — *"the
overall width on page 3 of this PDF reads 6012 mm"*. Replace the PDF and the sentence is no longer
about anything: the polygon points at different ink, the page may not exist, and the value may be
right for a drawing nobody is reviewing. The metric computed from it is not missing or noisy, it is
*confidently wrong*, and it will read as a passing gate.

So a mismatch raises. It is not a warning and there is no flag to continue past it, because a gold
set that scores against stale annotations is worse than no gold set — it manufactures evidence that
the system works.

**Why every reviewed document is checked, not just one.** `GoldMatch` pairs an `arch_item` with a
`shop_item`, so the architectural drawing is annotated as surely as the shop drawing is. Until
`#187`, `Provenance` carried one hash for the whole case; the architectural PDF could be swapped and
every match silently invalidated while the check reported the case intact.

Source: `#187` (B1.2) · Verification: `tests/eval/test_store.py`
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from eval.gold_set.schema import (
    DEFAULT_CASES_DIRECTORY,
    DEFAULT_MANIFEST_PATH,
    GoldCase,
    GoldManifest,
    load_manifest,
)
from rules.semantic_types import OperandSource
from storage.hashing import sha256_stream

#: Which `GoldCase` field holds the file for each reviewed source. `PRODUCT_SPEC` is deliberately
#: absent: `OperandSource` admits it and `ADR-0015` treats it as a hashed artifact, but a gold case
#: has nowhere to put its path yet. Reaching one raises rather than skipping — an unverifiable
#: document quietly passing is the whole failure this module exists to prevent.
DOCUMENT_PATHS: dict[OperandSource, str] = {
    OperandSource.ARCH: "arch",
    OperandSource.SHOP: "shop",
}


class StaleAnnotation(Exception):
    """The drawing changed under an annotation.

    Loud, because the alternative is a confidently wrong metric. An annotation names a page, a
    polygon and a value; against different bytes all three can be wrong at once while the case still
    scores, and the result reads as a passing gate rather than as a broken one.
    """


class MissingDrawing(Exception):
    """A case names a file that is not there.

    Distinct from `StaleAnnotation`. A changed drawing means the annotation is invalid; an absent one
    means the case was never loadable, and the fix is different — find the file rather than re-review
    the package.
    """


class UnverifiableDocument(Exception):
    """A case records a hash for a source this loader cannot resolve to a file."""


def content_hash(path: Path) -> str:
    """The `sha256:<hex>` of a file, streamed.

    Streamed rather than read whole: a drawing set is large, and `storage/hashing.py` already solved
    this with a bounded read. Reusing it also means a gold case's hash and a stored artifact's are
    produced by the same code, so they can be compared without anybody wondering whether the two
    agree on encoding.
    """
    with path.open("rb") as handle:
        digest, _size = sha256_stream(handle)
    return f"sha256:{digest}"


def _resolve(case: GoldCase, source: OperandSource, root: Path) -> Path:
    field = DOCUMENT_PATHS.get(source)
    if field is None:
        raise UnverifiableDocument(
            f"case {case.id!r} records a {source.value} document, and a gold case has nowhere to "
            "record its path. Recording a hash for bytes nothing can locate would claim an integrity "
            "check that never runs."
        )
    declared = Path(getattr(case, field))
    # Relative paths resolve against the cases root, which lives outside the repository: client
    # drawings are proprietary and `tests/test_repo_hygiene.py` fails the build if they appear in it.
    return declared if declared.is_absolute() else (root / declared)


def verify(case: GoldCase, *, root: Path = Path(DEFAULT_CASES_DIRECTORY)) -> None:
    """Raise unless every document this case recorded still hashes to what was recorded.

    Checks all of them, and reports the first mismatch with both hashes. Naming the file and showing
    the two values is the difference between "something changed" and a person knowing which drawing
    to go and look at.
    """
    for document in case.provenance.documents:
        path = _resolve(case, document.source, root)
        if not path.is_file():
            raise MissingDrawing(
                f"case {case.id!r} names a {document.source.value} drawing that is not there: "
                f"{path}. The case cannot be loaded, let alone scored."
            )
        actual = content_hash(path)
        if actual != document.content_hash:
            raise StaleAnnotation(
                f"case {case.id!r}: the {document.source.value} drawing at {path} has changed since "
                f"it was annotated by {case.provenance.annotator} on "
                f"{case.provenance.annotated_on}.\n"
                f"  annotated against: {document.content_hash}\n"
                f"  on disk now:       {actual}\n"
                "The annotation describes bytes that are no longer there, so every value, page and "
                "polygon in it may now point somewhere else. Re-review the package or restore the "
                "drawing — scoring against this would produce a metric that is confidently wrong."
            )


def load_cases(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    *,
    cases_directory: str | Path = DEFAULT_CASES_DIRECTORY,
    verify_integrity: bool = True,
) -> tuple[GoldCase, ...]:
    """Load every gold case, verified and in a deterministic order.

    Ordered by case id rather than by however the manifest happened to list them. Two runs of the
    evaluation harness have to be comparable, and an order that depends on file layout makes a
    difference in the results attributable to nothing.

    `verify_integrity` exists for the narrow case of inspecting a manifest whose drawings are not
    present — listing what a gold set contains, on a machine that does not hold the proprietary
    files. It defaults to on, and scoring must never turn it off: `#187`'s whole point is that an
    annotation for a changed PDF is invalid, loudly.
    """
    manifest: GoldManifest = load_manifest(manifest_path, cases_directory=cases_directory)
    cases = tuple(sorted(manifest.cases, key=lambda case: case.id))
    if verify_integrity:
        root = Path(cases_directory)
        for case in cases:
            verify(case, root=root)
    return cases


def stale(cases: Iterable[GoldCase], *, root: Path = Path(DEFAULT_CASES_DIRECTORY)) -> list[str]:
    """Which cases would fail verification, without raising on the first.

    For a person asking "what is broken?" rather than a harness asking "may I score?". The harness
    uses `load_cases`, which refuses; this reports, so somebody can see all of it at once instead of
    fixing one case per run.
    """
    broken: list[str] = []
    for case in cases:
        try:
            verify(case, root=root)
        except (StaleAnnotation, MissingDrawing, UnverifiableDocument) as error:
            broken.append(f"{case.id}: {type(error).__name__}")
    return broken
