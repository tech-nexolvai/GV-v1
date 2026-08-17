"""The gate enforces the issue dependencies a contract declares (`scripts/issue_gate.py`).

Until this check existed, an issue number in `requires:` was printed in the brief and enforced by
nothing. `status:` was the only lock, and it is maintained by hand. A sweep of the 144 open issues
found **twelve** stories reporting READY while the issue they declared a dependency on was still
open — in two stacked ways:

* the number was usually written `- #165`, and the contract parser strips whitespace-`#` as a YAML
  comment, so the entry vanished and `requires:` parsed as empty;
* the `status:` field said `ready` regardless, because nothing had recomputed it since the
  dependency was written down.

Either alone would have let the story through. `docs/BUILD_ORDER.md` D7 recorded that a stronger
enforcement was possible and left it undecided; twelve live cases decided it.

The network is never touched here. `open_issue_dependencies` shells out to `gh`, so these tests
replace `subprocess.run` — a test that needed GitHub to be up would be a test that fails for
reasons that have nothing to do with the code.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence

import pytest

from scripts.issue_gate import open_issue_dependencies, parse_contract


class _Result:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


@pytest.fixture
def issues(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, tuple[str, str]]]:
    """Answer `gh api .../issues/N` from a dict, so no test needs the network."""
    table: dict[str, tuple[str, str]] = {}

    def fake_run(command: Sequence[str], **_: object) -> _Result:
        number = str(command[2]).rsplit("/", 1)[-1]
        if number not in table:
            return _Result(1, "")
        state, title = table[number]
        return _Result(0, f"{state}\t{title}\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    yield table


def test_an_open_dependency_blocks(issues: dict[str, tuple[str, str]]) -> None:
    """The twelve. A story cannot be ready for work whose input does not exist."""
    issues["165"] = ("open", "B7.2 — drawing_items")
    blocking = open_issue_dependencies([165])
    assert len(blocking) == 1
    assert "#165 is still open" in blocking[0]
    assert "drawing_items" in blocking[0], "the reason names the work, not just the number"


def test_a_closed_dependency_does_not_block(issues: dict[str, tuple[str, str]]) -> None:
    """The check has to be able to say yes, or it would simply stop all work."""
    issues["191"] = ("closed", "C1.1 — SQLAlchemy base")
    assert open_issue_dependencies([191]) == []


def test_an_unresolvable_dependency_blocks(issues: dict[str, tuple[str, str]]) -> None:
    """Unknown is refused, as everywhere else in this system. A deleted issue or a network failure
    leaves the dependency unverified, and an unverified dependency is not a satisfied one."""
    blocking = open_issue_dependencies([9999])
    assert len(blocking) == 1
    assert "could not be resolved" in blocking[0]


@pytest.mark.parametrize("entry", ["D3", "Q5", "", "not-a-number"])
def test_non_numeric_entries_are_left_to_their_own_checks(
    entry: str, issues: dict[str, tuple[str, str]]
) -> None:
    """`Qn` is resolved against `docs/CLIENT_FACTS.md` and `Dn` is held by
    `status: needs-architecture`. Treating either as an issue number here would produce a second,
    contradictory verdict on the same entry."""
    assert open_issue_dependencies([entry]) == []


def test_a_hash_prefixed_number_is_still_resolved(issues: dict[str, tuple[str, str]]) -> None:
    """Defensive. The contract parser eats `- #165` before this ever sees it, which is why the
    contracts were rewritten to bare digits — but if one survives by another route it must not be
    silently ignored, because that is exactly how the twelve got through."""
    issues["165"] = ("open", "B7.2 — drawing_items")
    assert len(open_issue_dependencies(["#165"])) == 1


def test_the_parser_eats_a_hash_prefixed_requires_entry() -> None:
    """The other half of the failure, asserted so nobody reintroduces the form.

    `- #165` is stripped as a YAML comment and the list comes back empty, so a story declaring a
    dependency that way declares nothing at all.
    """
    contract = parse_contract(
        "## Agent contract\n\n```yaml\nstatus: ready\nrequires:\n  - #165\n```\n"
    )
    assert contract is not None
    assert contract["requires"] == [], "the '- #N' form must stay demonstrably broken here"

    bare = parse_contract("## Agent contract\n\n```yaml\nstatus: ready\nrequires:\n  - 165\n```\n")
    assert bare is not None
    assert bare["requires"] == ["165"]
