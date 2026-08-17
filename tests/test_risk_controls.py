"""Every risk in the register must name a control, and every claim must be checkable.

`docs/RISK_CONTROLS.md` is prose until something verifies it, and prose drifts. This guard parses it
and checks each reference against the tree, in both directions:

- an ENFORCED row whose artifact is missing fails — a control was deleted, or never landed
- a PLANNED row whose artifact now **exists** also fails — the table has fallen behind the code

The second is the one that catches the real failure. When #157 was written its own body claimed
*"C5 shipped hashes"*; `storage/` did not exist and every C5 issue was open. A half-built control
with a closed issue is indistinguishable from a finished one unless something looks.

Deliberately offline. A closed issue is a claim; a file on disk is a fact.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX = REPO_ROOT / "docs" / "RISK_CONTROLS.md"
SEMGREP_RULES = REPO_ROOT / ".semgrep" / "gv-rules.yaml"

#: The ten risks named in the system design §16. Pinned here so a risk cannot be dropped from the
#: matrix by deleting its row — the register is the architecture's, not this document's, to change.
REQUIRED_RISKS: tuple[str, ...] = (
    "R1",
    "R2",
    "R3",
    "R4",
    "R5",
    "R6",
    "R7",
    "R8",
    "R9",
    "R10",
)


class ControlStatus(StrEnum):
    ENFORCED = "ENFORCED"
    PARTIAL = "PARTIAL"
    PLANNED = "PLANNED"


@dataclass(frozen=True, slots=True)
class Row:
    """One risk, its control, and the references that claim to enforce it."""

    risk_id: str
    title: str
    status: ControlStatus
    refs: tuple[str, ...]
    owner: str
    effectiveness: str


class MatrixError(AssertionError):
    """The matrix could not be parsed. Deliberately loud: an unparseable table is unchecked."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_SECTION = re.compile(
    r"^## (?P<id>R\d+) — (?P<title>.+?)$\n" r"(?P<body>.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)
_FIELD = re.compile(
    r"^\*\*(?P<key>[A-Za-z ]+?)(?: \(§16\))?:\*\*\s*(?P<value>.+?)$", re.MULTILINE | re.DOTALL
)


def parse_matrix(path: Path = MATRIX) -> tuple[Row, ...]:
    """Read the table between the RISK TABLE markers into rows."""
    text = path.read_text(encoding="utf-8")
    try:
        body = text.split("<!-- RISK TABLE START -->", 1)[1].split("<!-- RISK TABLE END -->", 1)[0]
    except IndexError as exc:  # pragma: no cover - only when the markers are removed
        raise MatrixError(
            f"{path} has no RISK TABLE START/END markers. The guard cannot tell which part of the "
            "document is the table, so nothing would be checked."
        ) from exc

    rows: list[Row] = []
    for section in _SECTION.finditer(body):
        fields = {
            m.group("key").strip().lower(): m.group("value").strip()
            for m in _FIELD.finditer(section.group("body"))
        }
        missing = {"control", "status", "refs", "owner", "effectiveness claim"} - fields.keys()
        if missing:
            raise MatrixError(
                f"{section.group('id')} is missing: {', '.join(sorted(missing))}. "
                "A row without all five fields is not a checkable claim."
            )
        rows.append(
            Row(
                risk_id=section.group("id"),
                title=section.group("title").strip(),
                status=ControlStatus(fields["status"].split()[0]),
                refs=tuple(r.strip() for r in fields["refs"].split(",") if r.strip()),
                owner=fields["owner"],
                effectiveness=" ".join(fields["effectiveness claim"].split()),
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Resolving one reference against the tree
# ---------------------------------------------------------------------------


_RULE_ID = re.compile(r"^\s*-\s*id:\s*(?P<id>[\w-]+)\s*$", re.MULTILINE)


def _semgrep_rule_ids() -> frozenset[str]:
    """Read rule ids by pattern rather than by parsing YAML.

    Deliberately dependency-free. PyYAML lives in the `rules` extra, and the guards CI job installs
    only `dev` — so importing it made this guard fail to *collect* on CI while passing locally. A
    guard that needs a dependency to run is a guard that will one day not run.

    The failure mode is safe: a rule id this misses simply does not resolve, so an ENFORCED row
    fails loudly rather than passing quietly. The assertion below catches a total parse failure,
    which would otherwise make every semgrep reference fail for a confusing reason.
    """
    ids = frozenset(m.group("id") for m in _RULE_ID.finditer(SEMGREP_RULES.read_text("utf-8")))
    if not ids:
        raise MatrixError(
            f"no rule ids parsed from {SEMGREP_RULES}. Either the file moved or its formatting "
            "changed; every semgrep reference would otherwise fail for the wrong reason."
        )
    return ids


def _test_node_exists(spec: str) -> bool:
    """True when pytest can collect the named test.

    Collection rather than a text search: a test that was renamed still exists as text in the file
    it moved from, and a reference that resolves to a comment is not a control.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", spec],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,  # a non-zero exit IS the answer here — it means "not collectable"
    )
    return result.returncode == 0


def resolves(ref: str) -> bool:
    """True when a reference points at something that exists in the tree today."""
    prefix, _, target = ref.partition(":")
    if not target:
        raise MatrixError(
            f"reference {ref!r} has no prefix. Use test:, module: or semgrep: — bare prose like "
            "'enforced by B4' names an epic, not an artifact, and cannot be checked."
        )
    match prefix:
        case "semgrep":
            return target in _semgrep_rule_ids()
        case "test":
            if "::" in target:
                return _test_node_exists(target)
            return (REPO_ROOT / target).is_file()
        case "module":
            # A file, deliberately. `eval/gold_set/cases/` exists and holds nothing but a
            # `.gitkeep`, and it resolved on this guard's first run — an empty directory passing
            # as evidence of a gold set is precisely the false positive being guarded against.
            return (REPO_ROOT / target).is_file()
        case "cases":
            # A directory that actually contains something. Hidden files do not count: `.gitkeep`
            # exists to make an empty directory survive git, which is the opposite of content.
            path = REPO_ROOT / target
            if not path.is_dir():
                return False
            return any(child for child in path.iterdir() if not child.name.startswith("."))
        case _:
            raise MatrixError(
                f"unknown reference kind {prefix!r} in {ref!r}. "
                "Known kinds: test, module, semgrep, cases."
            )


ROWS = parse_matrix()


# ---------------------------------------------------------------------------
# The register itself
# ---------------------------------------------------------------------------


def test_every_risk_in_the_register_has_a_row() -> None:
    """The ten risks come from the architecture, not from this document. Dropping one here would
    silently shrink the register."""
    assert tuple(row.risk_id for row in ROWS) == REQUIRED_RISKS


def test_every_row_names_at_least_one_reference() -> None:
    for row in ROWS:
        assert row.refs, f"{row.risk_id} names no enforcing reference at all"


def test_every_row_states_an_effectiveness_claim() -> None:
    """Implementation is checked mechanically; effectiveness is a claim. It still has to be written
    down, because 'the control exists' and 'the control works' are different assertions (ISO 14971).
    """
    for row in ROWS:
        assert len(row.effectiveness) > 40, (
            f"{row.risk_id} has no meaningful effectiveness claim. State which test would fail if "
            "the risk materialised."
        )


# ---------------------------------------------------------------------------
# Implementation verification — both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.risk_id)
def test_enforced_rows_actually_resolve(row: Row) -> None:
    """An ENFORCED control whose artifact is gone is the plainest failure: something that was
    protecting us stopped existing and nothing said so."""
    if row.status is not ControlStatus.ENFORCED:
        pytest.skip(f"{row.risk_id} is {row.status}")
    unresolved = [ref for ref in row.refs if not resolves(ref)]
    assert not unresolved, (
        f"{row.risk_id} is marked ENFORCED but these do not exist: {unresolved}. "
        "Either the control was removed, or the row was optimistic."
    )


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.risk_id)
def test_planned_rows_have_not_quietly_been_built(row: Row) -> None:
    """The direction that catches the real failure.

    A PLANNED row whose artifacts now exist means the code moved and the table did not. Left alone
    the register keeps reporting a risk as unmitigated when it is covered — and the next person to
    read it stops trusting it, which is worse than it being wrong.
    """
    if row.status is not ControlStatus.PLANNED:
        pytest.skip(f"{row.risk_id} is {row.status}")
    resolved = [ref for ref in row.refs if resolves(ref)]
    assert not resolved, (
        f"{row.risk_id} is marked PLANNED but these now exist: {resolved}. "
        "Promote the row to PARTIAL or ENFORCED — the table has fallen behind the code."
    )


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.risk_id)
def test_partial_rows_are_genuinely_partial(row: Row) -> None:
    """PARTIAL is the dangerous state and must be earned in both directions.

    All-resolved means the row should be ENFORCED and is understating our position. None-resolved
    means it should be PLANNED and is overstating it — which is how a half-built control passes for
    a finished one.
    """
    if row.status is not ControlStatus.PARTIAL:
        pytest.skip(f"{row.risk_id} is {row.status}")
    resolved = [ref for ref in row.refs if resolves(ref)]
    unresolved = [ref for ref in row.refs if not resolves(ref)]
    assert resolved, f"{row.risk_id} is PARTIAL but nothing resolves — it is PLANNED"
    assert unresolved, f"{row.risk_id} is PARTIAL but everything resolves — it is ENFORCED"


# ---------------------------------------------------------------------------
# The reference syntax
# ---------------------------------------------------------------------------


def test_every_reference_uses_a_known_kind() -> None:
    """`resolves` raises on an unknown kind, so this is really asserting the whole table parses."""
    for row in ROWS:
        for ref in row.refs:
            resolves(ref)


def test_a_bare_prose_reference_is_rejected() -> None:
    """The failure mode this syntax exists to prevent: 'enforced by B4' names an epic, and an epic
    is not something a build can check."""
    with pytest.raises(MatrixError, match="no prefix"):
        resolves("B4")


def test_an_unknown_reference_kind_is_rejected() -> None:
    with pytest.raises(MatrixError, match="unknown reference kind"):
        resolves("wishful:it-is-fine")


def test_a_named_test_node_must_be_collectable() -> None:
    """Distinguishes a real test from a renamed one whose old name survives in a docstring."""
    assert resolves("test:tests/test_licences.py")
    assert not resolves("test:tests/test_licences.py::test_that_was_deleted")


def test_a_semgrep_reference_checks_the_rule_file() -> None:
    assert resolves("semgrep:gv-no-default-tolerance")
    assert not resolves("semgrep:gv-rule-nobody-wrote")


def test_a_module_reference_needs_a_file_not_a_directory() -> None:
    """The guard's first run resolved `module:eval/gold_set/cases` — a directory holding nothing
    but a `.gitkeep`. An empty directory standing in for a gold set is exactly the false positive
    this whole table exists to prevent, so `module:` means a file."""
    assert resolves("module:verdict/registry.py")
    assert not resolves("module:eval/gold_set")


def test_a_cases_reference_needs_real_content() -> None:
    """`.gitkeep` exists to make an empty directory survive git. Counting it would let an empty
    gold set report as a populated one."""
    assert not resolves("cases:eval/gold_set/cases")
    assert resolves("cases:tests")


# ---------------------------------------------------------------------------
# The prose under the table must agree with the table
# ---------------------------------------------------------------------------


def test_the_summary_counts_match_the_table() -> None:
    """Prose drifts; the table is the fact.

    Three separate drifts had accumulated before this check existed. The summary named retrieval
    contamination as enforced after `#257` demoted `R7`, and still said five risks were planned after
    `#309` promoted `R8`. Nothing caught either, because the guard verified every reference in the
    table and never read the paragraph a person actually reads first.
    """
    text = (REPO_ROOT / "docs" / "RISK_CONTROLS.md").read_text(encoding="utf-8")
    actual = Counter(row.status for row in ROWS)

    for status in ControlStatus:
        match = re.search(rf"^{status.value}: (\d+)", text, re.MULTILINE)
        assert match, (
            f"the summary states no count for {status.value}. It is the first thing a reader sees, "
            "so it has to be checkable rather than a sentence somebody remembered to update."
        )
        stated = int(match.group(1))
        assert stated == actual[status], (
            f"the summary says {stated} {status.value} row(s); the table has {actual[status]}. "
            "Update the paragraph, not this test."
        )
