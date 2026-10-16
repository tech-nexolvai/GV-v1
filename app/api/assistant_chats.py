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
- `GET  /chats/{chat_id}/messages/{message_id}/events`: follow a message (#1479): the running
  message's events so far, then live until `message_end`; a finished one's `message_end` at once.
- `POST /chats/{chat_id}/cancel`: stop the message being answered (the only way to stop it).
- `GET  /chats/{chat_id}/files/{file_id}`, `GET /chats/{chat_id}/pictures/{picture_id}`.
- `POST /chats/{chat_id}/messages/{message_id}/markup-pdf`: the answer's findings drawn by code on a
  copy of the vendor's PDF (clouds, notes, the draft stamp, a notes page), kept as a chat file; the
  same file every time for the same answer. `GET .../findings.csv`: the findings with their checks.

The message runs in its own thread (`chat_service.run_message`) until it ends or Stop is pressed,
like claude.ai: the browser going away (a closed tab, a reload) does not stop it, and it is stored
when it ends. Its events go into the message's feed (`chat_feed.MessageFeed`); the `POST` stream and
every `GET .../events` are followers of that feed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import ARTIFACT_STORE_STATE, get_session
from app.auth import Principal
from app.auth.dependencies import Authenticated
from app.config import Settings
from app.errors import CodedHTTPException
from app.models import (
    AssistantChat,
    AssistantChatFile,
    AssistantChatMessage,
    Package,
    PackageRevision,
)
from app.models.document import Document, DocumentVersion, PackageRevisionDocument
from app.models.package import package_product
from app.review.assistant.chat_contract import (
    AssistantModelOut,
    AssistantModelsOut,
    ChatCancelOut,
    ChatCreatedOut,
    ChatFileOut,
    ChatListOut,
    ChatMessageIn,
    ChatOut,
)
from app.review.assistant.chat_engine import cost_text
from app.review.assistant.chat_feed import MessageFeed
from app.review.assistant.chat_findings import live_findings, make_markup_file
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
    add_file,
    chat_summaries,
    chat_title,
    create_chat,
    file_out,
    files_of,
    first_question,
    message_out,
    messages_of,
    package_name,
)
from app.review.assistant.chat_tools import ResultsLoader
from app.review.assistant.chat_trust import findings_csv
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
    package = _package(session, package_id, principal)
    return ChatListOut(
        chats=chat_summaries(session, package_id), package_name=package_name(package)
    )


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
    run = registry.get(chat_id)
    return ChatOut(
        id=chat.id,
        package_id=package.id,
        package_name=package_name(package),
        title=chat.title or (chat_title(first) if first else None),
        created_at=chat.created_at,
        messages=[message_out(message, files) for message in messages],
        running=run is not None,
        running_message_id=None if run is None else run.message_id,
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


# ---- an answer's findings: the marked-up PDF and the CSV -----------------------------------------

#: A marked-up PDF's file id is made from its answer's id, so asking twice finds the first file.
_MARKUP_NAMESPACE: Final = uuid.UUID("6c1f4a8e-1d1b-4c6e-9a54-6a1d2e7f0b3c")


def _answer(session: Session, chat_id: UUID, message_id: UUID) -> AssistantChatMessage:
    row = session.get(AssistantChatMessage, message_id)
    if row is None or row.chat_id != chat_id or row.role != "assistant":
        raise _not_found()
    return row


@router.post(
    "/chats/{chat_id}/messages/{message_id}/markup-pdf",
    response_model=ChatFileOut,
    summary="The answer's findings drawn on a copy of the vendor's PDF (by code, no AI call)",
    responses={
        404: {"description": "No such stored answer in this chat (or no access)."},
        409: {"description": "`uploads_missing`: the chat has no copy of the set's PDFs."},
        422: {"description": "`no_findings`: the answer has no findings to mark up."},
    },
)
def markup_pdf(
    request: Request,
    chat_id: UUID,
    message_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> ChatFileOut:
    """Red clouds and notes ("AI n: item, architect X; vendor Y") at each finding's vendor box (else
    its architect box), "AI markup: draft, not GV's signed review" on page 1, and a notes page
    listing every finding. Kept as a file of the answer; asking again returns the same file."""
    _chat(session, chat_id, principal)
    row = _answer(session, chat_id, message_id)
    findings = live_findings(row.blocks)
    if not findings:
        raise CodedHTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "no_findings",
            "This answer has no findings to mark up.",
        )
    file_id = uuid.uuid5(_MARKUP_NAMESPACE, str(message_id))
    workspace = ChatWorkspace(_chat_folder(request, chat_id))
    kept = workspace.files / str(file_id)
    existing = session.get(AssistantChatFile, file_id)
    if existing is not None and kept.is_file():
        return file_out(existing)
    scratch = workspace.files / f"{file_id}.partial"
    try:
        made = make_markup_file(workspace, findings, scratch)
    except LookupError:
        raise CodedHTTPException(
            status.HTTP_409_CONFLICT,
            "uploads_missing",
            "This chat has no copy of the drawing set's PDFs. Ask a question first.",
        ) from None
    if existing is not None:
        # The row is there but its file was lost: the same drawing again.
        scratch.replace(kept)
        return file_out(existing)
    scratch.replace(kept)
    try:
        added = add_file(
            session,
            file_id=file_id,
            chat_id=chat_id,
            message_id=message_id,
            name=made.name,
            size=made.size,
            mime="application/pdf",
            sha256=made.sha256,
        )
        session.commit()
    except IntegrityError:
        # Asked twice at once: the other request's row stands, with the same drawing.
        session.rollback()
        other = session.get(AssistantChatFile, file_id)
        if other is None:
            raise
        return file_out(other)
    return file_out(added)


@router.get(
    "/chats/{chat_id}/messages/{message_id}/findings.csv",
    response_class=Response,
    summary="The answer's findings with their checks, as a spreadsheet (CSV)",
    responses={404: {"description": "No such stored answer in this chat (or no access)."}},
)
def findings_spreadsheet(
    chat_id: UUID,
    message_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    _chat(session, chat_id, principal)
    row = _answer(session, chat_id, message_id)
    body = findings_csv(live_findings(row.blocks))
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="findings.csv"'},
    )


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
    own = package_product(package)
    products = (
        tuple(body.products)
        if body.products is not None
        else (None if own is None else (own.value,))
    )
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
            mode=body.mode,
            products=products,
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
    only stops this stream: the message goes on and is stored, and `GET .../events` follows it."""
    # A daemon, as before: a server that is stopped does not wait minutes for an answer (that
    # question is then left without one, as after a crash).
    threading.Thread(
        target=run_message,
        args=(message.job, message.run),
        name="assistant-chat-message",
        daemon=True,
    ).start()
    async for event, data in message.run.feed.follow():
        yield ServerSentEvent(event=event, raw_data=json.dumps(data, ensure_ascii=False))


@dataclass(frozen=True, slots=True)
class _Followed:
    """The message to follow: its feed while it runs, else its stored `message_end`."""

    feed: MessageFeed | None
    stored: dict[str, Any] | None


def _followed(
    chat_id: UUID,
    message_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> _Followed:
    """Access (404), then the running message's feed, else the stored answer (404 when there is
    neither: an unknown id, or an answer that was never stored)."""
    _chat(session, chat_id, principal)
    run = registry.get(chat_id)
    if run is not None and run.message_id == message_id:
        return _Followed(feed=run.feed, stored=None)
    # The feed closes before the slot is freed, and the answer is stored before the feed closes:
    # a message no longer running is in the database (unless it could not be stored).
    row = session.get(AssistantChatMessage, message_id)
    if row is None or row.chat_id != chat_id or row.role != "assistant":
        raise _not_found()
    stored = message_out(row, files_of(session, chat_id)).model_dump(mode="json")
    return _Followed(feed=None, stored=stored)


@router.get(
    "/chats/{chat_id}/messages/{message_id}/events",
    response_class=EventSourceResponse,
    summary="Follow a message: its events so far, then live until it ends",
    responses={404: {"description": "No such message in this chat (or no access)."}},
)
async def follow_message(
    followed: Annotated[_Followed, Depends(_followed)],
) -> AsyncIterator[ServerSentEvent]:
    """For a running message, every event so far (the same events as the `POST` stream, with
    streamed words merged), then each new one until `message_end`. For a finished message,
    `message_end` with the stored message at once. Leaving changes nothing for the message."""
    if followed.feed is None:
        yield ServerSentEvent(
            event="message_end",
            raw_data=json.dumps({"message": followed.stored}, ensure_ascii=False),
        )
        return
    async for event, data in followed.feed.follow():
        yield ServerSentEvent(event=event, raw_data=json.dumps(data, ensure_ascii=False))


__all__ = ["chat_registry", "chat_runtime", "router"]
