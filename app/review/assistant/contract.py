"""The assistant's request and event shapes (#1128), and the internal draft an answer starts as.

The panel is built against these shapes, so they are the contract: change one only with the
frontend. A `Draft` uses the model's short record ids (`C1`, `F1`, `P4`); `publish` in
`app.review.assistant.answers` turns it into an `AnswerEvent` with the screen's real ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_HISTORY_TEXT",
    "MAX_HISTORY_TURNS",
    "MAX_QUESTION_LENGTH",
    "MODEL_LABEL",
    "STAGE_LABELS",
    "Action",
    "AnswerEvent",
    "AssistantInfoOut",
    "AssistantRequest",
    "Citation",
    "CountertopEvidence",
    "Draft",
    "ErrorEvent",
    "Evidence",
    "Focus",
    "GroupEvidence",
    "HistoryTurn",
    "OpenPageAction",
    "OpenQueueItemAction",
    "StageEvent",
    "StageId",
]

MAX_QUESTION_LENGTH: Final = 500
MAX_HISTORY_TURNS: Final = 6
#: A turn of history is a short chat message; anything longer is cut by the client or refused here.
MAX_HISTORY_TEXT: Final = 2000
MODEL_LABEL: Final = "Claude Sonnet 5.5"

type StageId = Literal["records", "model", "guard"]
STAGE_LABELS: Final[dict[str, str]] = {
    "records": "Reading this review's records",
    "model": "Asking Claude Sonnet",
    "guard": "Checking every number against the records",
}


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HistoryTurn(_Request):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=MAX_HISTORY_TEXT)


class Focus(_Request):
    """What the reviewer is looking at: a page, a record (countertop row id or finding id), both."""

    page_number: int | None = Field(default=None, ge=1)
    record_id: str | None = Field(default=None, min_length=1, max_length=64)


class AssistantRequest(_Request):
    """A reviewer question. It carries no value, outcome or decision the server would accept."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    # Not strict only so a JSON array is accepted as the tuple; each turn is still strict.
    history: tuple[HistoryTurn, ...] = Field(default=(), max_length=MAX_HISTORY_TURNS, strict=False)
    focus: Focus | None = None


class AssistantInfoOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    model_label: str = MODEL_LABEL
    keeps_no_data: bool = True
    starters: tuple[str, ...]


class StageEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: StageId
    label: str


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["page", "countertop", "finding"]
    page_number: int | None
    record_id: str | None
    label: str


class CountertopEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["countertop"]
    record_id: str


class GroupEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["blockers", "no_countertop_pages", "rows_not_checked"]


Evidence = Annotated[CountertopEvidence | GroupEvidence, Field(discriminator="kind")]


class OpenPageAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["open_page"]
    page_number: int
    label: str


class OpenQueueItemAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["open_queue_item"]
    record_id: str
    label: str


Action = Annotated[OpenPageAction | OpenQueueItemAction, Field(discriminator="kind")]


class AnswerEvent(BaseModel):
    """The answer. In `text`, `[[0]]`, `[[1]]` … mark the `citations` entry a sentence rests on."""

    model_config = ConfigDict(frozen=True)

    text: str
    citations: tuple[Citation, ...]
    evidence: tuple[Evidence, ...]
    actions: tuple[Action, ...]
    suggestions: tuple[str, ...] = Field(max_length=3)
    checked: bool
    mode: Literal["llm", "records_only", "refused", "disabled"]
    model_id: str | None
    sources: tuple[str, ...]


class ErrorEvent(BaseModel):
    """A plain-English failure: never a stack trace, a provider's words or a key."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Draft:
    """An answer before it is checked and published: a template in the records' short ids.

    `text` states facts only through placeholders (`{C1.outcome}`, `{count.needs_you}`; see
    `app.review.assistant.placeholders`), which code fills and cites. `evidence` are `C1` or a
    group name (`blockers`, `no_countertop_pages`, `rows_not_checked`); `actions` are
    `("open_page", "P4")` or `("open_queue_item", "C1")`.
    """

    text: str
    evidence: tuple[str, ...] = ()
    actions: tuple[tuple[str, str], ...] = field(default=())
