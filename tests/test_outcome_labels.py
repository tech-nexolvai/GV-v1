"""The badge and the sentence beside it must use the same word for the same outcome.

A reviewer reads one screen, not two codebases. The backend composes the narration under a finding
from `workflow/findings_composer.py::_OUTCOME_LABELS`; the frontend renders the badge, the findings
table, the summary chips and the usage breakdown from
`frontend/main/src/data/outcomeLabels.ts::OUTCOME_LABELS`. Those were two independent lists, and
they disagreed: the narration said "Waiting on a value" while the badge on the same card said
"NOT FOUND" and the table said "Not found".

"NOT FOUND" is the one that mattered. It sits beside an evidence panel that may be displaying a crop
perfectly well, and it reads as *the evidence was not found* — when it means the check could not run
because a value it needs has not been supplied. Those are different instructions to the person
reading: one sends them to look for a drawing, the other to type a number.

Nothing in either language can notice the drift, so this test does. It is a string comparison
between a Python dict and a TypeScript object literal, which is ugly and is the point — the
alternative is publishing the label on the API, and the label is presentation, not a stored fact.
"""

from __future__ import annotations

import re
from pathlib import Path

from workflow.findings_composer import _OUTCOME_LABELS

LABELS_TS = (
    Path(__file__).resolve().parents[1] / "frontend" / "main" / "src" / "data" / "outcomeLabels.ts"
)

#: `  PASS: 'Looks right',` — the entries of the exported object literal.
ENTRY = re.compile(r"^\s{2}([A-Z_]+):\s*'([^']+)',$", re.MULTILINE)


def _frontend_labels() -> dict[str, str]:
    return dict(ENTRY.findall(LABELS_TS.read_text(encoding="utf-8")))


def test_the_frontend_and_the_backend_agree_word_for_word() -> None:
    """**Input: both label maps. Outcome: identical keys and identical strings.**"""
    assert _frontend_labels() == _OUTCOME_LABELS


def test_no_engine_spelling_survives_in_the_frontend_labels() -> None:
    """The failure this guards is a label that is really the stored enum with the underscores out.

    `NOT FOUND` and `N/A RULE` both shipped, and both read as something the system did not mean.
    """
    for outcome, label in _frontend_labels().items():
        assert label != outcome
        assert label != outcome.replace("_", " ")
        assert label != outcome.replace("_", " ").title()
        assert not label.isupper(), f"{outcome} is still being shown in the engine's voice"
