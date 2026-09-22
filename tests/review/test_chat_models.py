"""The reviewer-chat model allow-list, and how a picked model routes to the provider.

The chat only narrates stored findings, so the model choice changes no verdict. What must hold is
that a reviewer can pick **only** from the deployment's allow-list: an unlisted id is refused, never
invoked. These are pure-value tests — no database, no Bedrock — over the allow-list functions and the
provider builder.

Source: issue #640; `docs/V1_TO_WORKING_PLAN.md` §2.2.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.review.chat_bedrock import configured_reviewer_chat
from app.review.chat_models import (
    ChatModelChoice,
    ChatModelNotAllowed,
    allowed_chat_models,
    default_chat_model,
    resolve_requested_model,
)

_DB = "postgresql+psycopg://gv:gv@localhost:5433/gv"
_HAIKU = ChatModelChoice(id="us.anthropic.claude-haiku-4-5-20251001-v1:0", label="Claude Haiku 4.5")
_NOVA = "amazon.nova-pro-v1:0"


def test_allow_list_appends_the_default_and_dedupes() -> None:
    allowed = allowed_chat_models((_HAIKU,), _NOVA)
    assert [choice.id for choice in allowed] == [_HAIKU.id, _NOVA]
    # The default already present is not duplicated.
    again = allowed_chat_models((_HAIKU, ChatModelChoice(id=_NOVA, label="Nova Pro")), _NOVA)
    assert [choice.id for choice in again] == [_HAIKU.id, _NOVA]


def test_empty_configured_exposes_just_the_default() -> None:
    allowed = allowed_chat_models((), _NOVA)
    assert [choice.id for choice in allowed] == [_NOVA]
    assert default_chat_model((), _NOVA) == _NOVA


def test_no_models_at_all_has_no_default() -> None:
    assert allowed_chat_models((), "") == ()
    assert default_chat_model((), "") is None


def test_none_resolves_to_the_default() -> None:
    assert resolve_requested_model((_HAIKU,), _NOVA, None) == _NOVA


def test_an_allow_listed_id_resolves_to_itself() -> None:
    assert resolve_requested_model((_HAIKU,), _NOVA, _HAIKU.id) == _HAIKU.id


def test_an_unlisted_id_is_refused_not_invoked() -> None:
    with pytest.raises(ChatModelNotAllowed):
        resolve_requested_model((_HAIKU,), _NOVA, "evil.model-v1:0")


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {"database_url": _DB, "bedrock_model": _NOVA}
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_provider_uses_the_chosen_model_over_the_default() -> None:
    chat = configured_reviewer_chat(_settings(), model_id=_HAIKU.id)
    assert chat is not None
    assert chat._config.model_id == _HAIKU.id


def test_provider_falls_back_to_the_default_when_none_chosen() -> None:
    chat = configured_reviewer_chat(_settings(), model_id=None)
    assert chat is not None
    assert chat._config.model_id == _NOVA


def test_provider_is_absent_when_chat_disabled() -> None:
    assert (
        configured_reviewer_chat(_settings(bedrock_chat_enabled=False), model_id=_HAIKU.id) is None
    )


def test_provider_is_absent_when_no_model_configured_and_none_chosen() -> None:
    assert configured_reviewer_chat(_settings(bedrock_model=""), model_id=None) is None
