"""The arms of a probe experiment: two configurations, run on the same pages, compared (Phase 1).

A probe is only useful if it can answer "is this change better than what we have?". That means two
arms on the same page set, the same repeats and the same job — today's open-ended prompts against
a candidate, with recall and the control-page noise rate reported for each.

**The question list is a configuration input, not something this package writes.** The question
list belongs to the `phase6-question-list` workstream (`vocabulary/review_questions.yaml` and
`app/review/assistant/question_list.py`). The probe only has to be able to point a run at one, so
the contract is the smallest thing that can work:

- the arm names a file, and the probe puts its path in `GV_ASSISTANT_QUESTION_LIST`;
- the run reads that variable, and when it is unset behaves exactly as it does today.

`question_list_ready()` says whether that workstream has landed on this branch. Until it has,
`plan` prints a warning and an arm that names a question list is a copy of the arm that does not —
so an experiment set up today keeps working the day the list arrives, and nobody can mistake two
identical arms for a result.

Nothing here spends anything; an arm is a name, a few settings and an environment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Final

__all__ = [
    "QUESTION_LIST_ENV",
    "QUESTION_LIST_MODULE",
    "Arm",
    "load_arms",
    "question_list_ready",
]

#: Where the probe puts the question list's path for a run to find. The question-list workstream
#: has to read this; nothing else about its file format matters to the probe.
QUESTION_LIST_ENV: Final = "GV_ASSISTANT_QUESTION_LIST"

#: The module that workstream is writing. Its presence is how the probe knows the list can be used.
QUESTION_LIST_MODULE: Final = "app.review.assistant.question_list"


def question_list_ready() -> bool:
    """Whether the question-list workstream has landed on this branch."""
    try:
        return find_spec(QUESTION_LIST_MODULE) is not None
    except (ImportError, ValueError):  # pragma: no cover - a half-built package
        return False


@dataclass(frozen=True)
class Arm:
    """One configuration under test."""

    name: str
    #: The question list this arm runs with, or None for today's open-ended prompts.
    question_list: Path | None = None
    #: Extra environment for the run, e.g. a lens switched off.
    env: dict[str, str] = field(default_factory=dict)
    effort: str = "high"
    model: str = "claude-opus-5-5"
    products: tuple[str, ...] = ()
    flag_mode: str | None = None
    note: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Arm:
        question_list = raw.get("question_list")
        return cls(
            name=str(raw["name"]),
            question_list=Path(str(question_list)).expanduser() if question_list else None,
            env={str(key): str(value) for key, value in (raw.get("env") or {}).items()},
            effort=str(raw.get("effort") or "high"),
            model=str(raw.get("model") or "claude-opus-5-5"),
            products=tuple(str(item) for item in raw.get("products") or ()),
            flag_mode=str(raw["flag_mode"]) if raw.get("flag_mode") else None,
            note=str(raw.get("note") or ""),
        )

    @property
    def usable_question_list(self) -> bool:
        """A named question list only counts when the file is there and the code can read it."""
        return bool(self.question_list and self.question_list.is_file() and question_list_ready())

    def environment(self) -> dict[str, str]:
        """The environment this arm asks for, on top of whatever the app already runs with."""
        out = dict(self.env)
        if self.usable_question_list and self.question_list is not None:
            out[QUESTION_LIST_ENV] = str(self.question_list)
        if self.flag_mode is not None:
            out["GV_ASSISTANT_FLAG_MODE"] = self.flag_mode
        return out

    def warnings(self) -> list[str]:
        """Everything about this arm that would quietly make it a copy of another one."""
        out: list[str] = []
        if self.question_list is not None:
            if not self.question_list.is_file():
                out.append(f"{self.name}: no question list at {self.question_list}")
            elif not question_list_ready():
                out.append(
                    f"{self.name}: {QUESTION_LIST_MODULE} is not on this branch, so the question "
                    f"list would be ignored and this arm would repeat the other one"
                )
        if self.environment() and not os.environ.get("GV_PROBE_ENV_REACHES_APP"):
            out.append(
                f"{self.name}: this arm needs {sorted(self.environment())} set on the app's own "
                f"process; the probe asks the app to do the check and cannot set them from here"
            )
        return out


def load_arms(path: Path) -> list[Arm]:
    """Read an experiment's arms from a JSON file: `{"arms": [{...}, {...}]}`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    arms = raw.get("arms") if isinstance(raw, dict) else raw
    return [Arm.from_dict(item) for item in arms or []]
