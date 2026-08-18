"""The label-versus-contract comparison, tested without touching the network.

The fetch is a `gh` call and the comparison is a pure function, split that way on purpose: the part
that can be wrong is the part that can be tested.

Verification for: `scripts/check_board_drift.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_board_drift import disagreements


def _issue(number: int, *labels: str, status: str | None = "ready", title: str = "A story") -> dict:
    contract = (
        ""
        if status is None
        else f"""## Agent contract

```yaml
status: {status}
owner: dev
implements: app/thing.py
```
"""
    )
    return {
        "number": number,
        "title": title,
        "body": f"{contract}\n\nSome prose.",
        "labels": [{"name": name} for name in labels],
    }


def test_agreement_is_not_reported() -> None:
    assert disagreements([_issue(1, "status:ready", "type:story", status="ready")]) == []


def test_an_optimistic_label_is_caught() -> None:
    """The expensive direction, and fifteen of the eighteen found in the first sweep. Somebody
    filters the board for ready work, plans an issue, and only then learns the gate stops it."""
    found = disagreements([_issue(163, "status:ready", status="deferred")])

    assert len(found) == 1
    assert (found[0].label, found[0].contract) == ("ready", "deferred")


def test_a_pessimistic_label_is_caught_too() -> None:
    """Both directions. A story labelled blocked while its contract says ready is work nobody picks
    up — cheaper than the other way round, but still a board that lies."""
    found = disagreements([_issue(180, "status:blocked-data", status="ready")])
    assert (found[0].label, found[0].contract) == ("blocked-data", "ready")


def test_two_status_labels_at_once_is_a_disagreement() -> None:
    """Not half-right. A filter matches it either way, which is worse than carrying no label at all,
    because it looks decided."""
    found = disagreements([_issue(5, "status:ready", "status:deferred", status="ready")])

    assert len(found) == 1
    assert found[0].label == "deferred/ready"


def test_no_status_label_at_all_is_a_disagreement() -> None:
    found = disagreements([_issue(6, "type:story", status="ready")])
    assert found[0].label == "(none)"


def test_an_issue_with_no_contract_is_left_alone() -> None:
    """Epics have none by design, and `issue_gate.py` already reports a malformed story as
    MALFORMED. Reporting it here as well trains people to skim this list."""
    assert disagreements([_issue(7, "status:epic", status=None)]) == []


def test_an_unknown_contract_status_is_left_to_the_gate() -> None:
    """One voice per defect. `issue_gate.py` refuses this outright; a second complaint saying
    something different about the same issue is how a check stops being read."""
    assert disagreements([_issue(8, "status:ready", status="nearly-ready")]) == []


def test_results_are_ordered_by_issue_number() -> None:
    """A sweep that reorders itself between runs cannot be diffed, and 'what changed since
    yesterday?' is the question anyone actually asks of it."""
    found = disagreements(
        [
            _issue(300, "status:ready", status="deferred"),
            _issue(12, "status:ready", status="deferred"),
            _issue(150, "status:ready", status="deferred"),
        ]
    )
    assert [entry.number for entry in found] == [12, 150, 300]
