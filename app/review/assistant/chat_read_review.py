"""After the second reads: the third reads and the critic, run for the chat engine (#1498).

`review_reads(engine)` is the engine's one hook, called right after its second reads. It:

1. **Escalates** each finding whose second read disagrees or is unsure (at most
   `read_adjudication.ADJUDICATION_LIMIT`, differences first) to a third read by a stronger model
   on sharper crops, and records `reads`, `checks.adjudication` and the new trust level
   (`read_adjudication.py`).
2. **Criticises** a whole-set or full-check answer's findings (`finding_critic.py`): up to 4 calls
   of up to 8 findings, and records `critic` and `checks.critic` (`passed` / `objects` / `unsure`,
   or `not_run` for a finding the limit or the cap left out).

Every call goes through the engine's own call path, so it is streamed, logged on the Usage page
(its own prompt id) and counted in the message's cost within its caps; a call that would pass a
cap is not made (the check is `not_run`). The calls run in parallel; the crops are drawn first,
one at a time (pdfium is not thread-safe). Code, never a model, decides every outcome.

Both modules are loaded by name, as the other checks are (`chat_trust.load_module`): a server
without them, or a test that leaves the checks out, simply runs neither.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant import chat_trust
from app.review.assistant.chat_request import BY_SHORT, thinking_config
from app.review.assistant.chat_trust import trust_level

if TYPE_CHECKING:
    from app.review.assistant import finding_critic, read_adjudication

__all__ = ["READ_SYSTEM", "review_reads"]

_log = logging.getLogger(__name__)

#: The third reader's instructions: those of the second read.
READ_SYSTEM: Final = (
    "You read values printed on crops of construction drawings. Answer only from what you can "
    "see in the pictures; never guess a number you cannot read."
)
_WORKERS: Final = 8
ADJUDICATION_MODULE: Final = "app.review.assistant.read_adjudication"
CRITIC_MODULE: Final = "app.review.assistant.finding_critic"
_SECOND_READ_MODEL: Final = "haiku"


class _NoRoom(Exception):
    """The call would pass the message's caps: it is not made."""


class _Budget:
    """The estimates of the calls running now, held so parallel calls do not overshoot a cap."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.held = 0
        self.lock = threading.Lock()

    def hold(self, estimate: int | None) -> int:
        amount = estimate or 0
        with self.lock:
            if not self.engine._fits(self.held + amount):
                raise _NoRoom
            self.held += amount
        return amount

    def release(self, amount: int) -> None:
        with self.lock:
            self.held -= amount


def _model_call(
    engine: Any,
    budget: _Budget,
    *,
    short: str,
    effort: str,
    system: str,
    max_tokens: int,
    prompt_tokens: int,
    prompt_id: str,
) -> Callable[[list[dict[str, Any]]], str]:
    model = BY_SHORT[short]

    def call(content: list[dict[str, Any]]) -> str:
        held = budget.hold(engine._price(model, prompt_tokens, max_tokens))
        try:
            reply = engine._call(
                model=model,
                effort=effort,
                thinking=thinking_config(model, False),
                system=system,
                tools=(),
                messages=[{"role": "user", "content": content}],
                final=False,
                max_tokens=max_tokens,
                prompt_id=prompt_id,
                display=None,
            )
        finally:
            budget.release(held)
        if reply.stop_reason == "refusal":
            raise RuntimeError("declined")
        text: str = reply.text()
        return text

    return call


def _prompt_id(engine: Any, purpose: str) -> str:
    """`review-assistant-chat-check-…` with the check's own purpose, for the Usage page."""
    return str(engine.check_prompt_id).replace("-chat-check-", f"-chat-{purpose}-", 1)


def _differences_first(finding: Mapping[str, Any]) -> int:
    return {"different": 0, "check": 1}.get(str(finding.get("status")), 2)


def _pdf_for(box: Mapping[str, Any], pdf_paths: Mapping[str, Path]) -> Path | None:
    name = box.get("file")
    if isinstance(name, str) and name in pdf_paths:
        return pdf_paths[name]
    if name is None and len(pdf_paths) == 1:
        return next(iter(pdf_paths.values()))
    return None


def _adjudication_crops(
    adjudication: Any, finding: Mapping[str, Any], pdf_paths: Mapping[str, Path]
) -> dict[str, read_adjudication.SideCrops]:
    crops: dict[str, read_adjudication.SideCrops] = {}
    for side, key in (("architect", "arch_box"), ("vendor", "vendor_box")):
        box = finding.get(key)
        if not isinstance(box, Mapping):
            continue
        pdf = _pdf_for(box, pdf_paths)
        if pdf is None or not pdf.is_file():
            continue
        try:
            drawn = adjudication.render_crops(pdf, box)
        except Exception as failed:  # noqa: BLE001 - no crop, the third read goes without it
            _log.warning("assistant chat third-read crop failed: %s", type(failed).__name__)
            continue
        if drawn is not None:
            crops[side] = drawn
    return crops


def review_reads(engine: Any) -> None:
    """The third reads and the critic for this message's findings (see the module's words)."""
    book = engine.book
    findings = book.live()
    if not findings:
        return
    budget = _Budget(engine)
    lock = threading.Lock()

    def update(finding_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        with lock:
            current = next((f for f in book.live() if f["id"] == finding_id), None)
            if current is None:
                return
            updated = change(dict(current))
            updated["trust_level"] = trust_level(updated["checks"], updated.get("status"))
            if book.replace(updated):
                engine._show_finding(updated)

    jobs: list[Callable[[], None]] = []
    adjudication_module = chat_trust.load_module(ADJUDICATION_MODULE)
    critic_module = chat_trust.load_module(CRITIC_MODULE)

    # 1. Third reads.
    escalate: list[dict[str, Any]] = []
    if adjudication_module is not None:
        escalate = sorted(
            (f for f in findings if adjudication_module.needs_adjudication(f)),
            key=_differences_first,
        )[: adjudication_module.ADJUDICATION_LIMIT]
    if adjudication_module is not None and escalate:
        pdf_paths = book.pdf_paths()
        model = adjudication_module.adjudication_model()
        read_call = _model_call(
            engine,
            budget,
            short=model,
            effort=adjudication_module.ADJUDICATION_EFFORT,
            system=READ_SYSTEM,
            max_tokens=adjudication_module.ADJUDICATION_MAX_TOKENS,
            prompt_tokens=adjudication_module.ADJUDICATION_PROMPT_TOKENS,
            prompt_id=_prompt_id(engine, "adjudicate"),
        )
        drawing = import_module("app.review.assistant.chat_drawing")
        for finding in escalate:
            with drawing._PDFIUM_LOCK:
                crops = _adjudication_crops(adjudication_module, finding, pdf_paths)
            if crops:
                jobs.append(
                    _third_read_job(
                        engine, adjudication_module, finding, crops, read_call, model, update
                    )
                )

    # 2. The critic, for whole-set and full-check answers only.
    whole_set = bool(book.coverage_used) or getattr(engine.settings, "mode", None) == "full_check"
    judged: list[str] = []
    if whole_set and critic_module is not None:
        candidates = critic_module.candidates(findings)
        critic_call = _model_call(
            engine,
            budget,
            short=critic_module.CRITIC_MODEL,
            effort=critic_module.CRITIC_EFFORT,
            system=critic_module.SYSTEM,
            max_tokens=critic_module.CRITIC_MAX_TOKENS,
            prompt_tokens=critic_module.CRITIC_PROMPT_TOKENS,
            prompt_id=_prompt_id(engine, "critic"),
        )
        for batch in critic_module.batches(candidates):
            jobs.append(_critic_job(engine, critic_module, batch, critic_call, update))
            judged.extend(str(f["id"]) for f in batch)
        for finding in candidates:
            if str(finding["id"]) not in judged:
                update(str(finding["id"]), _critic_not_run)

    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for future in [pool.submit(job) for job in jobs]:
            future.result()


def _critic_not_run(finding: dict[str, Any]) -> dict[str, Any]:
    finding["checks"] = {**(finding.get("checks") or {}), "critic": "not_run"}
    return finding


def _third_read_job(
    engine: Any,
    adjudication_module: Any,
    finding: Mapping[str, Any],
    crops: Mapping[str, read_adjudication.SideCrops],
    call: Callable[[list[dict[str, Any]]], str],
    model: str,
    update: Callable[[str, Callable[[dict[str, Any]], dict[str, Any]]], None],
) -> Callable[[], None]:
    def job() -> None:
        if engine.cancel.is_set():
            return
        try:
            result = adjudication_module.adjudicate(
                finding, crops, call, model=model, second_model=_SECOND_READ_MODEL
            )
        except Exception as failed:  # noqa: BLE001 - not run: no room, cancelled or a failure
            if not isinstance(failed, _NoRoom):
                _log.warning("assistant chat third read not made: %s", type(failed).__name__)
            return

        def change(current: dict[str, Any]) -> dict[str, Any]:
            current["checks"] = {**(current.get("checks") or {}), "adjudication": result.outcome}
            current["reads"] = result.reads
            return current

        update(str(finding["id"]), change)

    return job


def _critic_job(
    engine: Any,
    critic_module: Any,
    batch: list[Mapping[str, Any]],
    call: Callable[[list[dict[str, Any]]], str],
    update: Callable[[str, Callable[[dict[str, Any]], dict[str, Any]]], None],
) -> Callable[[], None]:
    def job() -> None:
        crops: dict[str, tuple[bytes, bytes]] = {}
        if not engine.cancel.is_set():
            for finding in batch:
                try:
                    crops[str(finding["id"])] = (
                        engine.read_picture(str(finding["arch_picture_id"])),
                        engine.read_picture(str(finding["vendor_picture_id"])),
                    )
                except OSError:
                    continue
        verdicts: dict[str, finding_critic.CriticVerdict] = {}
        if crops:
            try:
                verdicts = critic_module.criticise(batch, crops, call)
            except Exception as failed:  # noqa: BLE001 - not run: no room, cancelled or a failure
                if not isinstance(failed, _NoRoom):
                    _log.warning("assistant chat critic not run: %s", type(failed).__name__)
        for finding in batch:
            finding_id = str(finding["id"])
            verdict = verdicts.get(finding_id)
            if verdict is None:
                update(finding_id, _critic_not_run)
                continue

            def change(
                current: dict[str, Any], verdict: finding_critic.CriticVerdict = verdict
            ) -> dict[str, Any]:
                current["checks"] = {**(current.get("checks") or {}), "critic": verdict.result}
                current["critic"] = verdict.as_dict(critic_module.CRITIC_MODEL)
                return current

            update(finding_id, change)

    return job
