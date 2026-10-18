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
  same file every time for the same answer. `GET .../findings.csv`: the findings with their checks
  and decisions.
- `POST /chats/{chat_id}/messages/{message_id}/findings/{finding_id}/decision` (#1521): the
  reviewer's ✓ Agree / ✗ Not a problem, kept append-only; the latest per finding counts and comes
  back on the finding (`decision`) in `GET /chats/{chat_id}` and the follow feed.
- `POST /chats/{chat_id}/messages/{message_id}/cloud-drawing` (#1521, #1581): the cloud drawing
  and the check report made again by code with the current decisions as overrides (an agreed item
  is clouded, one marked not a problem is not; nobody has to decide); a new pair of files each
  time, the older ones kept.
- `GET  /packages/{package_id}/full-check-plan` (#1569): before a paid full check, code's plan in
  one line and 0-3 questions where it is unsure of the page map (whose drawing, which round, which
  architect page); the answers go with the full check (`plan_answers`). `$0`: no AI call. `GET
  /packages/{package_id}/plan-pages/{page}`: the page's picture for a question.

The message runs in its own thread (`chat_service.run_message`) until it ends or Stop is pressed,
like claude.ai: the browser going away (a closed tab, a reload) does not stop it, and it is stored
when it ends. Its events go into the message's feed (`chat_feed.MessageFeed`); the `POST` stream and
every `GET .../events` are followers of that feed.

A server restart (#1514): at start-up, and whenever a chat (or a message's events) is read, a
question left with no reply and nothing running for it gets its reply stored as `stopped:
interrupted` with the work saved so far. On SIGTERM no new message is taken (503), running answers
get a short wait, and each one still running is stored as interrupted before the process exits.
`POST .../messages` with `continue_from` goes on from an interrupted answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi import Path as Path_
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
    ChatFindingDecisionIn,
    ChatFindingDecisionOut,
    ChatFullCheckPlanOut,
    ChatListOut,
    ChatMessageIn,
    ChatMessageOut,
    ChatOut,
)
from app.review.assistant.chat_corrections import corrections_by_answer, with_corrections
from app.review.assistant.chat_decisions import (
    decided_findings,
    decided_summary,
    decision_out,
    latest_decisions,
    make_decided_files,
    record_decision,
    resolver_settled,
)
from app.review.assistant.chat_engine import cost_text
from app.review.assistant.chat_feed import MessageFeed
from app.review.assistant.chat_findings import live_findings, make_markup_file
from app.review.assistant.chat_memory import add_note, decision_note
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
    full_check_plan,
    package_spent_micros,
    plan_page_picture,
    recover_interrupted,
    run_message,
    shutdown_chats,
    start_set_pack,
    sync_uploads,
)
from app.review.assistant.chat_store import (
    add_assistant_message,
    add_file,
    add_user_message,
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
from app.review.assistant.consistency_answer import (
    CHECK_MODEL,
    NO_MODEL_CALL_MICROS,
    QUESTION,
    PackNotReady,
    run_check,
)
from app.review.assistant.sandbox import ChatWorkspace, Sandbox, open_sandbox
from app.runs.rates import ModelRates, rates_from_environment

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


def _registry_of(app: Any) -> ChatRegistry:
    registry: ChatRegistry | None = getattr(app.state, _REGISTRY_STATE, None)
    if registry is None:
        with _REGISTRY_LOCK:
            registry = getattr(app.state, _REGISTRY_STATE, None)
            if registry is None:
                registry = ChatRegistry()
                setattr(app.state, _REGISTRY_STATE, registry)
    return registry


def chat_registry(request: Request) -> ChatRegistry:
    """The process's running messages (one per chat at most)."""
    return _registry_of(request.app)


# ---- a server restart (#1514) --------------------------------------------------------------------


def _chat_root(app: Any) -> Path:
    settings: Settings = app.state.settings
    return Path(settings.assistant_chat_root).expanduser()


def _recover_at_start(app: Any) -> None:
    """The answers the last run of the server left without a reply, stored as interrupted."""
    try:
        from app.api.dependencies import _ENGINE_STATE, _engine_lock
        from app.db.session import engine_from_settings

        with _engine_lock:
            engine = getattr(app.state, _ENGINE_STATE, None)
            if engine is None:
                engine = engine_from_settings(app.state.settings)
                setattr(app.state, _ENGINE_STATE, engine)
        count = recover_interrupted(
            sessionmaker(bind=engine, expire_on_commit=False), _registry_of(app), _chat_root(app)
        )
        if count:
            _log.info("assistant chats: %d answers stored as interrupted by a restart", count)
    except Exception as failed:  # noqa: BLE001 - a chat is also repaired when it is next read
        _log.warning("assistant chats could not be checked at start-up: %s", type(failed).__name__)


def _pass_on(previous: Any, signum: int, frame: Any) -> None:
    """The signal handed to whoever handled it before (uvicorn), or its default."""
    if callable(previous):
        previous(signum, frame)
    elif previous == signal.SIG_DFL:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def _graceful_sigterm(app: Any) -> Callable[[], None] | None:
    """On SIGTERM: no new message, running answers saved (a bounded wait), then the server's own
    shutdown. Only in the main thread (where signals are handled); returns the undo."""
    if threading.current_thread() is not threading.main_thread():
        return None
    previous = signal.getsignal(signal.SIGTERM)
    stopping = threading.Event()

    def on_term(signum: int, frame: Any) -> None:
        registry = _registry_of(app)
        if stopping.is_set() or not registry.runs():
            registry.close()
            _pass_on(previous, signum, frame)
            return
        stopping.set()

        def stop() -> None:
            try:
                count = shutdown_chats(registry)
                _log.info("assistant chats: %d running answers stored as interrupted", count)
            finally:
                _pass_on(previous, signum, frame)

        threading.Thread(target=stop, name="assistant-chat-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)

    def undo() -> None:
        if signal.getsignal(signal.SIGTERM) is on_term:
            signal.signal(signal.SIGTERM, previous)

    return undo


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    threading.Thread(
        target=_recover_at_start, args=(app,), name="assistant-chat-recover", daemon=True
    ).start()
    undo = _graceful_sigterm(app)
    try:
        yield
    finally:
        if undo is not None:
            undo()
        # Whatever is still running when the app ends is kept as interrupted, not lost.
        shutdown_chats(_registry_of(app), wait_seconds=0)


router = APIRouter(tags=["assistant chats"], lifespan=_lifespan)


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
    sharpen: bool = False
    """#1551: sharpen a question no code rule settles with the cheap model (a deployment turns it
    on; a test runtime keeps code rules only unless it asks)."""
    sharpen_deadline_seconds: float = 1.5


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
        sharpen=settings.assistant_sharpen,
        sharpen_deadline_seconds=settings.assistant_sharpen_deadline_seconds,
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
            # Fable 5.1 only where the route offers it (#1577: Anthropic's own API).
            if runtime.choice.name in model.providers
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


def _repair(request: Request, session: Session, registry: ChatRegistry, chat_id: UUID) -> None:
    """A question of this chat left with no reply and nothing running for it (a restart): its
    reply stored as interrupted, before the chat is read."""
    if registry.get(chat_id) is not None:
        return
    try:
        recover_interrupted(_factory(session), registry, _chat_root(request.app), [chat_id])
    except Exception:  # noqa: BLE001 - the chat still reads; the next read tries again
        _log.exception("assistant chat could not store an interrupted answer")


@router.get("/chats/{chat_id}", response_model=ChatOut, summary="One chat and its messages")
def read_chat(
    request: Request,
    chat_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> ChatOut:
    chat, package = _chat(session, chat_id, principal)
    _repair(request, session, registry, chat_id)
    messages = messages_of(session, chat_id)
    files = files_of(session, chat_id)
    first = first_question(messages)
    run = registry.get(chat_id)
    decisions = latest_decisions(session, chat_id)
    corrections = corrections_by_answer(messages)
    return ChatOut(
        id=chat.id,
        package_id=package.id,
        package_name=package_name(package),
        title=chat.title or (chat_title(first) if first else None),
        created_at=chat.created_at,
        messages=[
            message_out(message, files, decisions.get(message.id), corrections.get(str(message.id)))
            for message in messages
        ],
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


# ---- the plan check before a full check (#1569) --------------------------------------------------


@router.get(
    "/packages/{package_id}/full-check-plan",
    response_model=ChatFullCheckPlanOut,
    summary="Code's plan for a full check and its 0-3 questions (no AI call)",
    responses={404: {"description": "No such drawing set (or no access)."}},
)
def get_full_check_plan(
    request: Request,
    package_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> ChatFullCheckPlanOut:
    """Copies the set's latest PDFs aside and starts its set pack when it is not made yet, then
    reads the page map: `building` until it is made (ask again in a moment)."""
    package = _package(session, package_id, principal)
    uploads = _upload_sources(
        _factory(session),
        getattr(request.app.state, ARTIFACT_STORE_STATE, None),
        package.id,
        _latest_revision_id(session, package.id),
    )
    try:
        plan = full_check_plan(
            _chat_root(request.app),
            package.id,
            uploads,
            lambda page: f"/api/v1/packages/{package.id}/plan-pages/{page}",
        )
    except Exception as failed:  # noqa: BLE001 - the full check still runs, without questions
        _log.warning("assistant chat plan check failed: %s", type(failed).__name__)
        plan = {"state": "none", "summary": None, "doubts": [], "more": 0}
    return ChatFullCheckPlanOut.model_validate(plan)


@router.get(
    "/packages/{package_id}/plan-pages/{page}",
    response_class=FileResponse,
    summary="A page's picture for a plan-check question",
)
def plan_page(
    request: Request,
    package_id: UUID,
    page: Annotated[int, Path_(ge=1, le=9999)],
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
) -> FileResponse:
    _package(session, package_id, principal)
    path = plan_page_picture(_chat_root(request.app), package_id, page)
    if path is None:
        raise _not_found()
    return FileResponse(path, media_type="image/png")


# ---- an answer's findings: the marked-up PDF and the CSV -----------------------------------------

#: A marked-up PDF's file id is made from its answer's id, so asking twice finds the first file.
_MARKUP_NAMESPACE: Final = uuid.UUID("6c1f4a8e-1d1b-4c6e-9a54-6a1d2e7f0b3c")


def _corrections(
    session: Session, chat_id: UUID, row: AssistantChatMessage
) -> dict[str, dict[str, Any]]:
    """Later messages' newest correction of each of this answer's findings (#1548)."""
    return corrections_by_answer(messages_of(session, chat_id)).get(str(row.id), {})


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
    findings = live_findings(with_corrections(row.blocks, _corrections(session, chat_id, row)))
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
        # A narrow answer's own marked-up PDF keeps amber clouds at the items needing a decision:
        # they are often all it found (a full check's cloud drawing marks them with a number only).
        made = make_markup_file(workspace, findings, scratch, cloud_decisions=True)
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
    decided = latest_decisions(session, chat_id, message_id).get(message_id)
    body = findings_csv(decided_findings(row, decided, _corrections(session, chat_id, row)))
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="findings.csv"'},
    )


# ---- the reviewer's decisions (#1521) -------------------------------------------------------------


@router.post(
    "/chats/{chat_id}/consistency",
    response_model=ChatMessageOut,
    summary="The vendor consistency check: the vendor's drawings against themselves (code, $0)",
    responses={
        404: {"description": "No such chat (or no access)."},
        409: {
            "description": "`message_running`: a message is being answered in this chat; "
            "`uploads_missing`: the drawing set has no PDFs; `pack_building`: the set is still "
            "being read (ask again in a minute)."
        },
    },
)
def consistency_check(
    request: Request,
    chat_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> ChatMessageOut:
    """The "+" menu's Vendor consistency check (#1584), by code only (no AI call, $0): every
    dimension string the drawing proves is one string added up, two values printed for one span
    compared, and the areas, rounds and indexed sheets the set lacks listed. Stored in the chat as
    a question and its answer (findings with code's proof, the "not checked" list), so the cloud
    drawing and the CSV work on it as on any answer. Returns the stored answer."""
    chat, package = _chat(session, chat_id, principal)
    message_id = uuid4()
    run = registry.claim(chat.id, package.id, message_id)
    if run is None:
        raise CodedHTTPException(
            status.HTTP_409_CONFLICT,
            "message_running",
            "A message is being answered in this chat. Wait for it, or stop it.",
        )
    try:
        uploads = _upload_sources(
            _factory(session),
            getattr(request.app.state, ARTIFACT_STORE_STATE, None),
            package.id,
            _latest_revision_id(session, package.id),
        )
        workspace = ChatWorkspace.open(_chat_folder(request, chat_id))
        listing, _changed = sync_uploads(workspace, uploads())
        if not listing:
            raise CodedHTTPException(
                status.HTTP_409_CONFLICT,
                "uploads_missing",
                "This drawing set has no PDFs to check.",
            )
        workspace = start_set_pack(workspace, _chat_root(request.app), listing)
        try:
            made = run_check(workspace, listing)
        except PackNotReady:
            raise CodedHTTPException(
                status.HTTP_409_CONFLICT,
                "pack_building",
                "The drawing set is still being read (its numbers are being listed). Try again "
                "in a minute.",
            ) from None
        add_user_message(
            session, chat_id=chat.id, created_by=principal.id, text=QUESTION, mode="chat"
        )
        answer = add_assistant_message(
            session,
            message_id=message_id,
            chat_id=chat.id,
            created_by=principal.id,
            model=CHECK_MODEL,
            effort="",
            thinking=False,
            text=made.text,
            blocks=made.blocks,
            api_content=made.api_content,
            cost_micros=NO_MODEL_CALL_MICROS,
            stopped=None,
            error=None,
            mode="chat",
        )
        session.commit()
        return message_out(answer, [])
    finally:
        registry.release(run)


def _not_running(registry: ChatRegistry, chat_id: UUID, message_id: UUID) -> None:
    run = registry.get(chat_id)
    if run is not None and run.message_id == message_id:
        raise CodedHTTPException(
            status.HTTP_409_CONFLICT,
            "message_running",
            "This answer is still being written. Decide when it has finished.",
        )


@router.post(
    "/chats/{chat_id}/messages/{message_id}/findings/{finding_id}/decision",
    response_model=ChatFindingDecisionOut,
    status_code=status.HTTP_201_CREATED,
    summary="The reviewer agrees with a finding, or marks it not a problem (kept append-only)",
    responses={
        404: {"description": "No such stored answer or finding in this chat (or no access)."},
        409: {"description": "`message_running`: the answer is still being written."},
    },
)
def decide_finding(
    chat_id: UUID,
    message_id: UUID,
    finding_id: str,
    body: ChatFindingDecisionIn,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> ChatFindingDecisionOut:
    """One more decision row, by the signed-in person; the latest per finding is the one read."""
    _chat(session, chat_id, principal)
    _not_running(registry, chat_id, message_id)
    row = _answer(session, chat_id, message_id)
    corrected = live_findings(with_corrections(row.blocks, _corrections(session, chat_id, row)))
    finding = next((f for f in corrected if str(f.get("id")) == finding_id), None)
    if finding is None:
        raise _not_found()
    decided = record_decision(
        session,
        chat_id=chat_id,
        message_id=message_id,
        finding_id=finding_id,
        decision=body.decision,
        note=body.note,
        decided_by=principal.id,
    )
    # The set's memory (#1548): every later chat on the set knows what the reviewer decided.
    text, page = decision_note(finding, body.decision, principal.id, body.note)
    chat = session.get(AssistantChat, chat_id)
    if chat is not None:
        add_note(
            session,
            package_id=chat.package_id,
            note=text,
            page=page,
            source="decision",
            chat_id=chat_id,
            message_id=message_id,
            created_by=principal.id,
        )
    session.commit()
    return ChatFindingDecisionOut.model_validate(decision_out(decided))


@router.post(
    "/chats/{chat_id}/messages/{message_id}/cloud-drawing",
    response_model=ChatFileOut,
    summary="The cloud drawing (and check report) made again from the reviewer's decisions",
    responses={
        404: {"description": "No such stored answer in this chat (or no access)."},
        409: {
            "description": "`message_running`: the answer is still being written; "
            "`uploads_missing`: the chat has no copy of the set's PDFs."
        },
        422: {"description": "`no_findings`: the answer has no findings."},
    },
)
def cloud_drawing(
    request: Request,
    chat_id: UUID,
    message_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> ChatFileOut:
    """The zero-click cloud drawing made again with the reviewer's decisions as overrides (#1581):
    red clouds at the Wrong items code proved and at every agreed item, none at an item marked not
    a problem; the rest listed in grey; stamped "Checked automatically by GV Review: <date>, and
    reviewed by <name>". A new cloud drawing and check report each time (older files are kept);
    returns the cloud drawing."""
    chat, package = _chat(session, chat_id, principal)
    _not_running(registry, chat_id, message_id)
    row = _answer(session, chat_id, message_id)
    findings = decided_findings(
        row,
        latest_decisions(session, chat_id, message_id).get(message_id),
        _corrections(session, chat_id, row),
    )
    if not findings:
        raise CodedHTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "no_findings",
            "This answer has no findings to mark up.",
        )
    question = session.scalar(
        select(AssistantChatMessage.text)
        .where(
            AssistantChatMessage.chat_id == chat_id,
            AssistantChatMessage.seq < row.seq,
            AssistantChatMessage.role == "user",
        )
        .order_by(AssistantChatMessage.seq.desc())
        .limit(1)
    )
    summary, not_checked = decided_summary(row, question, findings, package_name(package))
    workspace = ChatWorkspace(_chat_folder(request, chat.id))
    drawing_id, report_id = uuid4(), uuid4()
    scratch = {
        drawing_id: workspace.files / f"{drawing_id}.partial",
        report_id: workspace.files / f"{report_id}.partial",
    }
    try:
        made = make_decided_files(
            workspace,
            findings,
            summary,
            not_checked,
            scratch[drawing_id],
            scratch[report_id],
            settled=resolver_settled(row.blocks),
        )
    except LookupError:
        raise CodedHTTPException(
            status.HTTP_409_CONFLICT,
            "uploads_missing",
            "This chat has no copy of the drawing set's PDFs. Ask a question first.",
        ) from None
    except BaseException:
        for path in scratch.values():
            path.unlink(missing_ok=True)
        raise
    rows = []
    for file_id, file in ((drawing_id, made.drawing), (report_id, made.report)):
        scratch[file_id].replace(workspace.files / str(file_id))
        rows.append(
            add_file(
                session,
                file_id=file_id,
                chat_id=chat_id,
                message_id=message_id,
                name=file.name,
                size=file.size,
                mime="application/pdf",
                sha256=file.sha256,
            )
        )
    session.commit()
    return file_out(rows[0])


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
    if registry.closing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The server is restarting. Send your message again in a moment.",
        )
    try:
        model = check_choice(body.model, body.effort, body.thinking, runtime.choice.name)
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
    mode: str = body.mode
    continue_from = None if body.continue_from is None else UUID(body.continue_from)
    if continue_from is not None:
        # Continue asks the way the interrupted answer was asked (a full check stays one).
        resume = _resume_of(session, chat.id, continue_from)
        mode = str(resume.get("mode") or "chat")
        if resume.get("products"):
            products = tuple(resume["products"])
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
            max_parallel_agents=settings.assistant_max_parallel_agents,
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
            mode=mode,
            products=products,
            continue_from=continue_from,
            drawn_helpers=settings.assistant_drawn_helpers,
            roster=settings.assistant_full_check_roster,
            full_check_cap_micros=int(settings.assistant_full_check_cap_usd * _MICROS),
            pairing_proof=settings.assistant_pairing_proof,
            plan_answers=(
                None
                if mode != "full_check" or not body.plan_answers
                else {answer.id: answer.answer for answer in body.plan_answers}
            ),
            sharpen_model=runtime.sharpen,
            sharpen_deadline_seconds=runtime.sharpen_deadline_seconds,
            task_edit=None if body.task is None else body.task.model_dump(),
        )
    except BaseException:
        registry.release(run)
        raise
    return _Message(job=job, run=run)


def _resume_of(session: Session, chat_id: UUID, message_id: UUID) -> dict[str, Any]:
    """The interrupted answer's `resume` block; 422 when it is not an interrupted answer here."""
    row = session.get(AssistantChatMessage, message_id)
    if row is None or row.chat_id != chat_id or row.stopped != "interrupted":
        raise CodedHTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_request",
            "continue_from is not an interrupted answer of this chat.",
        )
    return next(
        (
            dict(block.get("resume") or {})
            for block in reversed(row.blocks or [])
            if isinstance(block, dict) and block.get("type") == "resume"
        ),
        {},
    )


@router.post(
    "/chats/{chat_id}/messages",
    response_class=EventSourceResponse,
    summary="Send a message and stream the answer",
    responses={
        409: {"description": "`message_running`: a message is being answered in this chat."},
        422: {
            "description": "`choice_refused`: a model, effort and thinking the API refuses; "
            "`invalid_request`: a body (or a `continue_from`) that is not right."
        },
        503: {
            "description": "No AI provider key is set (the reason: GET /assistant/models), or "
            "the server is restarting."
        },
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
    request: Request,
    chat_id: UUID,
    message_id: UUID,
    principal: Authenticated,
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[ChatRegistry, Depends(chat_registry)],
) -> _Followed:
    """Access (404), then the running message's feed, else the stored answer (404 when there is
    neither: an unknown id, or an answer that was never stored). A message a restart cut off is
    stored as interrupted first, under the id the screen was following."""
    _chat(session, chat_id, principal)
    run = registry.get(chat_id)
    if run is not None and run.message_id == message_id:
        return _Followed(feed=run.feed, stored=None)
    _repair(request, session, registry, chat_id)
    # The feed closes before the slot is freed, and the answer is stored before the feed closes:
    # a message no longer running is in the database (unless it could not be stored).
    row = session.get(AssistantChatMessage, message_id)
    if row is None or row.chat_id != chat_id or row.role != "assistant":
        raise _not_found()
    decided = latest_decisions(session, chat_id, message_id).get(message_id)
    stored = message_out(row, files_of(session, chat_id), decided).model_dump(mode="json")
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
