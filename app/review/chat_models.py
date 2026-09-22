"""The server-side allow-list of reviewer-chat models.

The reviewer chat only *narrates* stored findings — every number in the answer comes from the
deterministic run, and `app.review.chat` rejects prose that adds, removes, or rejudges a fact. So
which model writes the prose changes nothing a verdict depends on. It is still untrusted input,
which is the one property this module protects: a reviewer may pick a model **only** from a list the
deployment approved, never an arbitrary id, so nobody can point the chat at an unapproved or
unmetered model. Picking one is a display and cost-attribution choice, not a change to what the AI
is allowed to decide.

Pure functions over plain values, so the choice logic is unit-testable without a database or a
provider. `resolve_requested_model` is the gate the endpoint calls.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field


class ChatModelChoice(BaseModel):
    """One selectable model: the exact Bedrock id, and the name a reviewer reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=300)
    label: str = Field(min_length=1, max_length=120)


class ChatModelNotAllowed(ValueError):
    """A reviewer asked for a model id that is not on the deployment's allow-list."""


def allowed_chat_models(
    configured: Sequence[ChatModelChoice], default_model: str
) -> tuple[ChatModelChoice, ...]:
    """The models a reviewer may choose from.

    The configured allow-list, in order, plus the deployment's default model when it is set and not
    already listed — so a deployment that only sets `bedrock_model` (today's shape) still exposes
    exactly that one model rather than an empty picker. Ids are de-duplicated; order is preserved.
    """

    chosen: list[ChatModelChoice] = []
    seen: set[str] = set()
    for choice in configured:
        if choice.id not in seen:
            chosen.append(choice)
            seen.add(choice.id)
    fallback = default_model.strip()
    if fallback and fallback not in seen:
        chosen.append(ChatModelChoice(id=fallback, label=fallback))
    return tuple(chosen)


def default_chat_model(configured: Sequence[ChatModelChoice], default_model: str) -> str | None:
    """The id used when a reviewer does not pick one, or `None` when nothing is available.

    Prefers the deployment's configured default; otherwise the first allow-listed model. `None`
    means no model is available at all, and the endpoint uses its plain structured fallback.
    """

    allowed = allowed_chat_models(configured, default_model)
    if not allowed:
        return None
    fallback = default_model.strip()
    return fallback if fallback else allowed[0].id


def resolve_requested_model(
    configured: Sequence[ChatModelChoice], default_model: str, requested: str | None
) -> str | None:
    """The model id to answer with, or raise if the reviewer asked for one not on the allow-list.

    `None` (the reviewer did not pick) resolves to the default. A non-`None` id must be on the
    allow-list; an unlisted id raises `ChatModelNotAllowed` rather than being invoked — a reviewer
    cannot reach an unapproved model.
    """

    if requested is None:
        return default_chat_model(configured, default_model)
    allowed = {choice.id for choice in allowed_chat_models(configured, default_model)}
    if requested not in allowed:
        raise ChatModelNotAllowed(requested)
    return requested
