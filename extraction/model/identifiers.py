"""Identifier uniqueness: asserted per package, and reported when violated (#166, B7.3).

The client confirmed that items may carry a **unique ID** in the item tag, and that ID is the strongest
matching signal in the system — priority 1 of the eight lanes. A matching lane that trusts an identifier
which is not actually unique will confidently pair two different cabinets, and every check downstream then
computes exact arithmetic about the wrong item.

**Most of this story was already built, and that is worth stating rather than re-declaring.**
`extraction/model/items.py` already has `IdentifierKind` (vendor unique, mark, catalogue),
`PrintedIdentifier` keeping the value exactly as printed, and `DrawingItem.identifiers` as a tuple so an
item may carry several of different kinds. Three of this story's four acceptance criteria are those types.
Redefining them here would have produced the second-definition problem that #164 just had to be corrected
for. So this module adds only the fourth: the check.

**Uniqueness expectations differ by kind, and the difference is quoted rather than invented.**
`items.py`'s own docstring: *"a catalogue number is shared by every unit of that model, while a mark is
unique to one drawing."*

| Kind | Unique within | Why |
|---|---|---|
| `VENDOR_UNIQUE` | the package | it is the unique ID; two items sharing one is a vendor error or a misread |
| `MARK` | one document version | a mark identifies an item on its own drawing, and two drawings may reuse `C-1` |
| `CATALOGUE` | nothing | every unit of a model carries it, so duplicates are the normal case |

Reporting a catalogue duplicate would be reporting that two base cabinets are the same model, which is not
a finding. A check that fires on the ordinary case is one somebody turns off.

**Compared exactly as printed, which is a real limitation stated plainly.** The second acceptance
criterion says normalisation *"lives in B5.1 and never overwrites"* the printed value, so this compares the
strings as read. `C-1` and `c-1` are therefore two identifiers here, and a duplicate that differs only by
OCR variance is not visible to this check — that is B9.2's job, where the original is still available to
show a reviewer. What this catches is the unambiguous case, and it does not pretend to catch more.

**Reported, never resolved.** A duplicate `VENDOR_UNIQUE` means the drawing is wrong or we misread it, and
nothing here can tell which. Picking one item to keep would be inventing an answer; the report names every
item involved so a reviewer can look at the sheets.

Source: backend proposal §10.1 `item_identifiers` · Design: `docs/DESIGN_EXTRACTION.md` §4.1 ·
Verification: `tests/extraction/model/test_identifiers.py`
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from extraction.model.items import DrawingItem, IdentifierKind

__all__ = [
    "UNIQUE_WITHIN",
    "DuplicateIdentifier",
    "IdentifierNotUnique",
    "UniquenessReport",
    "UniquenessScope",
    "check_uniqueness",
    "require_unique",
]


class UniquenessScope:
    """How far an identifier of a given kind is expected to be unique.

    Plain string constants rather than an enum: these are the *names of scopes* used in a report a person
    reads, and there is no branch that switches on them — `UNIQUE_WITHIN` drives the grouping instead.
    """

    PACKAGE: Final = "package"
    DOCUMENT_VERSION: Final = "document version"
    NOT_UNIQUE: Final = "not expected to be unique"


#: Which scope each kind must be unique within — the table from the module docstring, as data.
#:
#: A kind absent from this mapping would be silently unchecked, so `check_uniqueness` refuses to run
#: against an unknown kind rather than skipping it. A new `IdentifierKind` member must therefore state its
#: expectation here, which is the point of keeping this as a table rather than a chain of `if`s.
UNIQUE_WITHIN: Final[dict[IdentifierKind, str]] = {
    IdentifierKind.VENDOR_UNIQUE: UniquenessScope.PACKAGE,
    IdentifierKind.MARK: UniquenessScope.DOCUMENT_VERSION,
    IdentifierKind.CATALOGUE: UniquenessScope.NOT_UNIQUE,
}


@dataclass(frozen=True, slots=True)
class DuplicateIdentifier:
    """One identifier carried by more than one item where it should have been unique."""

    kind: IdentifierKind
    value_as_printed: str
    scope: str
    """Where it should have been unique — `package`, or the document version it was scoped to."""

    scope_key: UUID | None
    """The document version, when the scope is one drawing. `None` for a package-wide scope."""

    item_ids: tuple[UUID, ...]
    """Every item carrying it, in a stable order. All of them, not "the others" — nothing here decides
    which one was meant."""

    detail: str

    def __post_init__(self) -> None:
        if len(self.item_ids) < 2:
            raise ValueError(
                "a duplicate involves at least two items; one item is not a collision and reporting it "
                "would make the report meaningless"
            )


@dataclass(frozen=True, slots=True)
class UniquenessReport:
    """What the check found. Empty means every identifier was as unique as its kind requires."""

    duplicates: tuple[DuplicateIdentifier, ...] = ()
    checked: int = 0
    """How many identifiers were examined, so an empty report can be told from an empty input."""

    @property
    def is_unique(self) -> bool:
        return not self.duplicates


class IdentifierNotUnique(ValueError):
    """Raised by `require_unique` when an identifier that must be unique is not.

    Carries the whole report rather than the first duplicate: a caller that wanted the invariant will want
    to show a reviewer everything that broke it, not the first thing found.
    """

    def __init__(self, report: UniquenessReport) -> None:
        self.report = report
        first = report.duplicates[0]
        super().__init__(
            f"{len(report.duplicates)} identifier(s) are not unique. First: {first.detail}"
        )


def check_uniqueness(items: list[DrawingItem]) -> UniquenessReport:
    """Check every identifier against its kind's expectation. Reports; never raises for a duplicate.

    Refuses — with a `ValueError` — only when an identifier's kind has no stated expectation. That is a
    programming error rather than a data problem: a kind nobody has decided about would otherwise be
    skipped silently, and an unchecked identifier reads exactly like a unique one.
    """
    # (kind, scope key, value as printed) -> the items carrying it. The scope key is what makes a mark
    # unique per drawing rather than per package.
    seen: dict[tuple[IdentifierKind, UUID | None, str], list[UUID]] = defaultdict(list)
    checked = 0

    for item in items:
        for identifier in item.identifiers:
            if identifier.kind not in UNIQUE_WITHIN:
                raise ValueError(
                    f"{identifier.kind!r} has no stated uniqueness expectation in UNIQUE_WITHIN. A kind "
                    "nobody has decided about would be skipped, and an unchecked identifier reads "
                    "exactly like a unique one."
                )
            checked += 1
            scope = UNIQUE_WITHIN[identifier.kind]
            if scope == UniquenessScope.NOT_UNIQUE:
                continue
            scope_key = (
                item.view.document_version_id if scope == UniquenessScope.DOCUMENT_VERSION else None
            )
            seen[(identifier.kind, scope_key, identifier.value_as_printed)].append(item.id)

    duplicates = [
        DuplicateIdentifier(
            kind=kind,
            value_as_printed=value,
            scope=UNIQUE_WITHIN[kind],
            scope_key=scope_key,
            item_ids=tuple(sorted(carriers, key=str)),
            detail=(
                f"{len(carriers)} items carry the {kind.value} identifier {value!r}, which must be "
                f"unique within the {UNIQUE_WITHIN[kind]}"
                + (f" {scope_key}" if scope_key is not None else "")
                + ". Either the drawing repeats it or we misread one of them, and nothing here can tell "
                "which — every item is listed rather than one being chosen."
            ),
        )
        # Sorted so two runs over the same items produce the same report.
        for (kind, scope_key, value), carriers in sorted(
            seen.items(), key=lambda entry: (entry[0][0].value, str(entry[0][1]), entry[0][2])
        )
        if len(carriers) > 1
    ]

    return UniquenessReport(tuple(duplicates), checked)


def require_unique(items: list[DrawingItem]) -> UniquenessReport:
    """The same check, raising when it finds anything — for a caller that needs the invariant.

    Both exist on purpose. A report is what a reviewer reads; an exception is what stops a matching lane
    trusting an identifier it should not. The criterion asks for uniqueness to be *asserted* and
    *reported*, and those are two different needs rather than one with two names.
    """
    report = check_uniqueness(items)
    if not report.is_unique:
        raise IdentifierNotUnique(report)
    return report
