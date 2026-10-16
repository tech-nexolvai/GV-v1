"""The chat-first assistant's routes (#1463): saved chats per drawing set, streamed answers.

Access: the caller must belong to the package's project (the same boundary as the review routes);
anything else is a 404 that says nothing. The routes, under `/api/v1`:

- `GET  /assistant/models`: the models offered, whether the assistant can answer here (and why
  not), the caps, and (with `?package_id=`) what the drawing set's chats have spent.
- `GET  /packages/{package_id}/chats`, `POST /packages/{package_id}/chats`: list, start.
- `GET  /chats/{chat_id}`: the chat and its stored messages.
- `POST /chats/{chat_id}/messages`: one message, answered as Server-Sent Events. 409 while another
  message of the chat is being answered; 422 for a model, effort and thinking the API would
  refuse; 503 when no provider key is set.
- `POST /chats/{chat_id}/cancel`: stop the message being answered.
- `GET  /chats/{chat_id}/files/{file_id}`, `GET /chats/{chat_id}/pictures/{picture_id}`.

The message runs in its own thread (`chat_service.run_message`), so it is stored even when the
browser goes away; the stream only relays its events, and the browser going away cancels it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import ARTIFACT_STORE_STATE, get_session
from app.auth import Principal
from app.auth.dependencies import Authenticated
from app.config import Settings
from app.errors import CodedHTTPException
from app.models import (
    AssistantChat,
    AssistantChatFile,
    Package,
    PackageRevision,
)
from app.models.document import Document, DocumentVersion, PackageRevisionDocument
from app.review.assistant.chat_contract import (
    AssistantModelOut,
    AssistantModelsOut,
    ChatCancelOut,
    ChatCreatedOut,
    ChatListOut,
    ChatMessageIn,
    ChatOut,
)
from app.review.assistant.chat_engine import cost_text
from app.review.assistant.chat_provider import (
    AnthropicSdkProvider,
    ChatProvider,
    ProviderChoice,
    choose_provider,
)
from app.review.assistant.chat_request import (
    DEFAULT_EFFORT,
    EFFORTS,
    MODELS,
    ChoiceRefused,
    check_choice,
)
from app.review.assistant.chat_service import (
    ChatRegistry,
    MessageJob,
    RunningMessage,
    SkillBuilder,
    UploadSource,
    default_skill_builder,
    package_spent_micros,
    run_message,
)
from app.review.assistant.chat_store import (
    chat_summaries,
    chat_title,
    create_chat,
    files_of,
    first_question,
    message_out,
    messages_of,
    package_name,
)
from app.review.assistant.chat_tools import ResultsLoader
from app.review.assistant.sandbox import ChatWorkspace, Sandbox, open_sandbox
from app.runs.rates import ModelRates, rates_from_environment

router = APIRouter(tags=["assistant chats"])
_log = logging.getLogger(__name__)

NOT_FOUND_DETAIL: Final = "Not found"
_REGISTRY_STATE: Final = "assistant_chat_registry"
_REGISTRY_LOCK: Final = threading.Lock()
_MICROS: Final = Decimal(1_000_000)
#: A chat's sandbox: a little more room than the side panel's, for rendering whole sets.
_CHAT_DOCKER_LIMITS: Final = ("2", "2g")
_KIND_WORDS: Final = {
    "shop": "the vendor's shop drawings (a sheet may carry the architect's drawing beside the "
    "vendor's, with GV's markup on top)",
    "architectural": "the architect's drawings",
}


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


def chat_registry(request: Request) -> ChatRegistry:
    """The process's running messages (one per chat at most)."""
    registry: ChatRegistry | None = getattr(request.app.state, _REGISTRY_STATE, None)
    if registry is None:
        with _REGISTRY_LOCK:
            registry = getattr(request.app.state, _REGISTRY_STATE, None)
            if registry is None:
                registry = ChatRegistry()
                setattr(request.app.state, _REGISTRY_STATE, registry)
    return registry


def _rates(model_key: str) -> ModelRates | None:
    """The deployment's price file when it prices the model, else the published one."""
    from app.usage_history import published_rates

    stated = rates_from_environment()
    if stated is not None and stated.rate_for(model_key) is not None:
        return stated
    return published_rates()


@dataclass(frozen=True, slots=True)
class ChatRuntime:
    """What a deployment configured for the chats. A dependency, so a test swaps in fakes and no
    paid call is possible from a test."""

    choice: ProviderChoice
    provider: Callable[[], ChatProvider]
    open_sandbox: Callable[[ChatWorkspace], Sandbox | None]
    skill_builder: Callable[[], SkillBuilder | None]
    rates: Callable[[str], ModelRates | None]


def _secret(value: Any) -> str | None:
    text = None if value is None else value.get_secret_value()
    return text if text and text.strip() else None


def chat_runtime(request: Request) -> ChatRuntime:
    settings: Settings = request.app.state.settings
    choice = choose_provider(
        settings.assistant_provider,
        _secret(settings.anthropic_api_key),
        _secret(settings.openrouter_api_key),
    )

    def provider() -> ChatProvider:
        return AnthropicSdkProvider(choice, timeout_seconds=settings.assistant_call_timeout_seconds)

    def sandbox(workspace: ChatWorkspace) -> Sandbox | None:
        return open_sandbox(
            workspace,
            prefer=settings.assistant_sandbox,
            timeout_seconds=settings.assistant_command_timeout_seconds,
            docker_limits=_CHAT_DOCKER_LIMITS,
        )

    return ChatRuntime(
        choice=choice,
        provider=provider,
        open_sandbox=sandbox,
        skill_builder=default_skill_builder,
        rates=_rates,
    )


# ---- access --------------------------------------------------------------------------------------


def _package(session: Session, package_id: UUID, principal: Principal) -> Package:
    package = session.get(Package, package_id)
    if package is None or not principal.belongs_to(package.project_id):
        _log.warning(
            "assistant chat refused",
            extra={"actor": principal.id, "target": str(package_id), "reason": "package"},
        )
        raise _not_found()
    return package


def _chat(session: Session, chat_id: UUID, principal: Principal) -> tuple[AssistantChat, Package]:
    chat = session.get(AssistantChat, chat_id)
    if chat is None:
        raise _not_found()
    return chat, _package(session, chat.package_id, principal)


def _latest_revision_id(session: Session, package_id: UUID) -> UUID | None:
    return session.scalar(
        select(PackageRevision.id)
        .where(PackageRevision.package_id == package_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    )


def _factory(session: Session) -> sessionmaker[Session]:
    """Sessions on the request's own database, for work that outlives the request."""
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


# ---- models --------------------------------------------------------------------------------------


@router.get(
    "/assistant/models",
    response_model=AssistantModelsOut,
    summary="The chat's models, whether it can answer here, its caps and (per set) its spend",
)
def assistant_models(
    request: Request,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    runtime: Annotated[ChatRuntime, Depends(chat_runtime)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
    package_id: Annotated[UUID | None, Query()] = None,
) -> AssistantModelsOut:
    settings: Settings = request.app.state.settings
    spent = 0
    if package_id is not None:
        _package(session, package_id, principal)
        spent = package_spent_micros(session, package_id, registry)
    return AssistantModelsOut(
        available=runtime.choice.available,
        reason=runtime.choice.reason,
        provider=runtime.choice.name,
        models=[
            AssistantModelOut(
                id=model.id,
                label=model.label,
                default=model.default,
                efforts=list(EFFORTS),
                default_effort=DEFAULT_EFFORT,
                thinking=model.thinking,
                thinking_off_max_effort=model.thinking_off_max_effort,
            )
            for model in MODELS.values()
        ],
        message_cap_usd=cost_text(int(settings.assistant_message_cap_usd * _MICROS)),
        package_cap_usd=cost_text(int(settings.assistant_package_cap_usd * _MICROS)),
        package_spent_usd=cost_text(spent),
    )


# ---- chats ---------------------------------------------------------------------------------------


@router.get(
    "/packages/{package_id}/chats",
    response_model=ChatListOut,
    summary="The drawing set's saved chats, newest first",
)
def list_chats(
    package_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> ChatListOut:
    _package(session, package_id, principal)
    return ChatListOut(chats=chat_summaries(session, package_id))


@router.post(
    "/packages/{package_id}/chats",
    response_model=ChatCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start an empty chat about the drawing set",
)
def start_chat(
    package_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> ChatCreatedOut:
    _package(session, package_id, principal)
    chat = create_chat(session, package_id=package_id, created_by=principal.id)
    session.commit()
    return ChatCreatedOut(id=chat.id, title=None, created_at=chat.created_at)


@router.get("/chats/{chat_id}", response_model=ChatOut, summary="One chat and its messages")
def read_chat(
    chat_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> ChatOut:
    chat, package = _chat(session, chat_id, principal)
    messages = messages_of(session, chat_id)
    files = files_of(session, chat_id)
    first = first_question(messages)
    return ChatOut(
        id=chat.id,
        package_id=package.id,
        package_name=package_name(package),
        title=chat.title or (chat_title(first) if first else None),
        created_at=chat.created_at,
        messages=[message_out(message, files) for message in messages],
        running=registry.get(chat_id) is not None,
    )


@router.post("/chats/{chat_id}/cancel", response_model=ChatCancelOut, summary="Stop the message")
def cancel_message(
    chat_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> ChatCancelOut:
    _chat(session, chat_id, principal)
    return ChatCancelOut(cancelled=registry.cancel(chat_id))


# ---- files and pictures --------------------------------------------------------------------------


def _chat_folder(request: Request, chat_id: UUID) -> Path:
    settings: Settings = request.app.state.settings
    return Path(settings.assistant_chat_root).expanduser() / str(chat_id)


@router.get(
    "/chats/{chat_id}/files/{file_id}",
    response_class=FileResponse,
    summary="A file the assistant handed over",
)
def download_file(
    request: Request,
    chat_id: UUID,
    file_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> FileResponse:
    _chat(session, chat_id, principal)
    row = session.get(AssistantChatFile, file_id)
    if row is None or row.chat_id != chat_id:
        raise _not_found()
    path = ChatWorkspace(_chat_folder(request, chat_id)).files / str(file_id)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row.sha256:
        raise _not_found()
    return FileResponse(
        path, media_type=row.mime, filename=row.name, content_disposition_type="attachment"
    )


@router.get(
    "/chats/{chat_id}/pictures/{picture_id}",
    response_class=FileResponse,
    summary="A picture the assistant looked at",
)
def read_picture(
    request: Request,
    chat_id: UUID,
    picture_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> FileResponse:
    _chat(session, chat_id, principal)
    path = ChatWorkspace(_chat_folder(request, chat_id)).pictures / f"{picture_id}.png"
    if not path.is_file():
        raise _not_found()
    return FileResponse(path, media_type="image/png")


# ---- one message ---------------------------------------------------------------------------------


def _upload_sources(
    factory: sessionmaker[Session], store: Any, package_id: UUID, revision_id: UUID | None
) -> Callable[[], Sequence[UploadSource]]:
    """The latest revision's PDFs, read from the store only when the chat needs their bytes."""

    def listed() -> Sequence[UploadSource]:
        if revision_id is None:
            return []
        if store is None:
            raise RuntimeError("no artifact store is configured")
        from app.api.documents import storage_key

        with factory() as session:
            rows = session.execute(
                select(Document, DocumentVersion)
                .join(PackageRevisionDocument, PackageRevisionDocument.document_id == Document.id)
                .join(
                    DocumentVersion,
                    DocumentVersion.id == PackageRevisionDocument.document_version_id,
                )
                .where(
                    PackageRevisionDocument.package_revision_id == revision_id,
                    PackageRevisionDocument.package_id == package_id,
                )
                .order_by(Document.kind.desc(), Document.id)
            ).all()

        def reader(key: str) -> Callable[[], bytes]:
            def read() -> bytes:
                with store.get(key) as stored:
                    data: bytes = stored.read()
                return data

            return read

        return [
            UploadSource(
                name=version.file_name or f"{document.kind}.pdf",
                sha256=version.sha256,
                kind=_KIND_WORDS.get(document.kind, str(document.kind)),
                pages=version.page_count,
                read=reader(storage_key(version.document_id, version.sha256)),
            )
            for document, version in rows
        ]

    return listed


def _results_loader(
    factory: sessionmaker[Session], project_id: UUID, package_id: UUID, revision_id: UUID | None
) -> ResultsLoader | None:
    """`review_results`: the recorded results, read with the screens' own functions."""
    if revision_id is None:
        return None

    def load(page: int | None) -> dict[str, Any]:
        from app.api.review_assistant import ai_review_and_job_values, load_snapshot
        from app.review.assistant.agent import _page_snapshot
        from app.review.assistant.records import prompt_records

        with factory() as session:
            revision = session.get(PackageRevision, revision_id)
            if revision is None:
                raise LookupError("revision")
            document: dict[str, Any] = {}
            try:
                document.update(ai_review_and_job_values(session, project_id, revision_id, page))
            except Exception as failed:  # noqa: BLE001 - the records still answer
                _log.warning("assistant chat AI review read failed: %s", type(failed).__name__)
                session.rollback()
                document["ai_review_and_job_values"] = "could not be read"
            snapshot = load_snapshot(session, project_id, package_id, revision)
            scoped = snapshot if page is None else _page_snapshot(snapshot, page)
            document["records"] = json.loads(prompt_records(scoped))
            return document

    return load


@dataclass(frozen=True, slots=True)
class _Message:
    job: MessageJob
    run: RunningMessage


def _message(
    request: Request,
    chat_id: UUID,
    body: ChatMessageIn,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    runtime: Annotated[ChatRuntime, Depends(chat_runtime)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> _Message:
    """Everything checked before the stream starts: access (404), the choice (422), a provider
    (503), and the chat's one slot (409)."""
    settings: Settings = request.app.state.settings
    chat, package = _chat(session, chat_id, principal)
    try:
        model = check_choice(body.model, body.effort, body.thinking)
    except ChoiceRefused as refused:
        raise CodedHTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "choice_refused", str(refused)
        ) from None
    if not runtime.choice.available:
        # The reason is in `GET /assistant/models`; a 5xx body never carries details.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=runtime.choice.reason
        )
    revision_id = _latest_revision_id(session, package.id)
    factory = _factory(session)
    run = registry.claim(chat.id, package.id, uuid4())
    if run is None:
        raise CodedHTTPException(
            status.HTTP_409_CONFLICT,
            "message_running",
            "A message is already being answered in this chat. Wait for it, or stop it.",
        )
    try:
        job = MessageJob(
            chat_id=chat.id,
            package_id=package.id,
            revision_id=revision_id,
            created_by=principal.id,
            text=body.text,
            model=model,
            effort=body.effort,
            thinking=body.thinking,
            chat_root=Path(settings.assistant_chat_root).expanduser(),
            factory=factory,
            provider=runtime.provider(),
            registry=registry,
            lead_max_turns=settings.assistant_lead_max_turns,
            helper_max_turns=settings.assistant_helper_max_turns,
            message_cap_micros=int(settings.assistant_message_cap_usd * _MICROS),
            package_cap_micros=int(settings.assistant_package_cap_usd * _MICROS),
            rates=runtime.rates,
            uploads=_upload_sources(
                factory,
                getattr(request.app.state, ARTIFACT_STORE_STATE, None),
                package.id,
                revision_id,
            ),
            results=_results_loader(factory, package.project_id, package.id, revision_id),
            open_sandbox=runtime.open_sandbox,
            skill_builder=runtime.skill_builder(),
        )
    except BaseException:
        registry.release(run)
        raise
    return _Message(job=job, run=run)


@router.post(
    "/chats/{chat_id}/messages",
    response_class=EventSourceResponse,
    summary="Send a message and stream the answer",
    responses={
        409: {"description": "`message_running`: a message is being answered in this chat."},
        422: {"description": "`choice_refused`: a model, effort and thinking the API refuses."},
        503: {"description": "No AI provider key is set (the reason: GET /assistant/models)."},
    },
)
async def send_message(
    message: Annotated[_Message, Depends(_message)],
) -> AsyncIterator[ServerSentEvent]:
    """`message_start`, then thinking, text, steps, helpers, files and usage as they happen, then
    `message_end` with the stored message (`error` first when it failed). The browser going away
    cancels the message, which is still stored."""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    def emit(event: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(events.put_nowait, (event, data))

    def work() -> None:
        try:
            run_message(message.job, message.run, emit)
        finally:
            try:
                loop.call_soon_threadsafe(events.put_nowait, None)
            except RuntimeError:
                pass

    thread = threading.Thread(target=work, name="assistant-chat-message", daemon=True)
    finished = False
    try:
        thread.start()
        while True:
            item = await events.get()
            if item is None:
                finished = True
                break
            event, data = item
            yield ServerSentEvent(event=event, raw_data=json.dumps(data, ensure_ascii=False))
    finally:
        if not finished:
            # The browser went away (or the server is stopping): the message stops and is stored.
            message.run.stop()
        if not thread.is_alive() and not finished and not message.run.started:
            message.job.registry.release(message.run)


__all__ = ["chat_registry", "chat_runtime", "router"]
