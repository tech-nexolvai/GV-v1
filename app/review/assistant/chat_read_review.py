"""The third reads and the critic, for the chat engine (#1498, run in parallel since #1512).

1. **Third reads.** A finding whose second read disagrees or is unsure is read a third time by a
   stronger model on sharper crops; `reads`, `checks.adjudication` and the new trust level are
   recorded (`read_adjudication.py`). `third_read_job` makes one such job. The engine starts a
   would-be red's as soon as its second read comes back, every would-be red's before any other
   item's (2026-10-28); the others' wait for the lead's answer, at most
   `read_adjudication.ADJUDICATION_LIMIT` in all. Their money is `ThirdReadBudget`: at most
   `read_adjudication.ADJUDICATION_CAP_MICROS` per answer, inside the message's fixed price.
2. **The critic** (`finding_critic.py`), for a whole-set or full-check answer: up to 4 calls of up
   to 8 findings, recording `critic` and `checks.critic` (`passed` / `objects` / `unsure`, or
   `not_run` for a finding the limit or the cap left out). `critic_jobs` makes them; the engine
   runs them in the same pool as the second and third reads, from the start.

Every call goes through the engine's own call path, so it is streamed, logged on the Usage page
(its own prompt id) and counted in the message's cost within its caps; a call that would pass a
cap is not made (the check is `not_run`). The crops are drawn inside each job, one at a time
(pdfium is not thread-safe). Code, never a model, decides every outcome. `review_reads(engine)`
runs both kinds of job at once for an engine that does not pipeline.

Both modules are loaded by name, as the other checks are (`chat_trust.load_module`): a server
without them, or a test that leaves the checks out, simply runs neither.
"""

from __future__ import annotations

import base64
import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant import chat_trust
from app.review.assistant.agent import prompt_tokens_upper
from app.review.assistant.chat_request import BY_SHORT, thinking_config
from app.review.assistant.chat_trust import trust_level

if TYPE_CHECKING:
    from app.review.assistant import finding_critic, read_adjudication

__all__ = [
    "ADJUDICATION_MODULE",
    "READ_SYSTEM",
    "Budget",
    "NoRoom",
    "ThirdReadBudget",
    "critic_jobs",
    "needs_third_read",
    "review_reads",
    "third_read_job",
    "third_read_limit",
]

_log = logging.getLogger(__name__)

#: The reader's instructions, for the second read (the engine) and the third read alike.
READ_SYSTEM: Final = (
    "You read values printed on crops of construction drawings. Answer only from what you can "
    "see in the pictures; never guess a number you cannot read."
)
_WORKERS: Final = 8
ADJUDICATION_MODULE: Final = "app.review.assistant.read_adjudication"
CRITIC_MODULE: Final = "app.review.assistant.finding_critic"
_SECOND_READ_MODEL: Final = "haiku"


class NoRoom(Exception):
    """The call would pass the message's caps: it is not made."""


class Budget:
    """The estimates of the calls running now, held so parallel calls do not overshoot a cap."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.held = 0
        self.lock = threading.Lock()

    def hold(self, estimate: int | None) -> int:
        amount = estimate or 0
        with self.lock:
            if not self.engine._fits(self.held + amount):
                raise NoRoom
            self.held += amount
        return amount

    def release(self, amount: int) -> None:
        with self.lock:
            self.held -= amount

    def charge(self, cost: int) -> None:
        """What a finished call cost (only a capped budget counts it)."""


class ThirdReadBudget:
    """The third reads' money (2026-10-28): inside the message's caps (`inner`, the checks'
    shared budget) and at most `cap` in all, counting what the finished reads cost and the
    estimates of the reads running now. A read that would pass it is not made (`NoRoom`)."""

    def __init__(self, inner: Budget, cap: int) -> None:
        self.inner = inner
        self.cap = cap
        self.running = 0
        self.spent = 0
        self.lock = threading.Lock()

    def hold(self, estimate: int | None) -> int:
        amount = estimate or 0
        with self.lock:
            if self.spent + self.running + amount > self.cap:
                raise NoRoom
            held = self.inner.hold(amount)
            self.running += held
        return held

    def release(self, amount: int) -> None:
        self.inner.release(amount)
        with self.lock:
            self.running -= amount

    def charge(self, cost: int) -> None:
        with self.lock:
            self.spent += max(0, int(cost))


def _image_size(block: Mapping[str, Any]) -> tuple[int, int] | None:
    """A base64 PNG picture's (width, height), from its header; `None` for any other block."""
    source = block.get("source") if block.get("type") == "image" else None
    if not isinstance(source, Mapping) or source.get("media_type") != "image/png":
        return None
    try:
        head = base64.b64decode(str(source.get("data") or "")[:44])
    except ValueError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def content_tokens(content: list[dict[str, Any]], system: str, ceiling: int) -> int:
    """An upper bound on one read's prompt tokens from what it really sends (its text, and each
    picture at width x height / 750), never above `ceiling` (the read's fixed bound): a picture
    whose size cannot be told counts as the whole `ceiling`."""
    sizes = [_image_size(block) for block in content if block.get("type") == "image"]
    if any(size is None for size in sizes):
        return ceiling
    body = {"system": system, "messages": [{"role": "user", "content": content}]}
    pictures = [size for size in sizes if size is not None]
    return min(ceiling, prompt_tokens_upper(body, pictures))


def _model_call(
    engine: Any,
    budget: Budget | ThirdReadBudget,
    *,
    short: str,
    effort: str,
    system: str,
    max_tokens: int,
    prompt_tokens: int,
    prompt_id: str,
    output_format: Mapping[str, Any] | None = None,
    thinking: bool = False,
    sized: bool = False,
    enforce_format: bool = False,
) -> Callable[[list[dict[str, Any]]], str]:
    """One read-review call. `thinking`: adaptive thinking for a judgement (the critic, #1775 C4);
    off for copying a printed value (the second and third reads). `sized`: the call's estimate
    comes from what it really sends (`content_tokens`, at most `prompt_tokens`), so reads
    running at once do not hold their worst case. `enforce_format`: `output_format` is enforced
    on OpenRouter too (`chat_request.call_params`; the third read). What the call cost is charged
    to `budget`."""
    model = BY_SHORT[short]

    def call(content: list[dict[str, Any]]) -> str:
        tokens = content_tokens(content, system, prompt_tokens) if sized else prompt_tokens
        held = budget.hold(engine._price(model, tokens, max_tokens))
        try:
            reply = engine._call(
                model=model,
                effort=effort,
                thinking=thinking_config(model, thinking),
                system=system,
                tools=(),
                messages=[{"role": "user", "content": content}],
                final=False,
                max_tokens=max_tokens,
                prompt_id=prompt_id,
                display=None,
                output_format=output_format,
                **({"enforce_format": True} if enforce_format else {}),
            )
        finally:
            budget.release(held)
        charge = getattr(budget, "charge", None)
        cost = getattr(engine, "_cost", None)
        if callable(charge) and callable(cost):
            charge(cost(model, reply.usage))
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


type Update = Callable[[str, Callable[[dict[str, Any]], dict[str, Any]]], None]


def third_read_limit() -> int:
    """Third reads one answer may make (`read_adjudication.ADJUDICATION_LIMIT`); 0 without it."""
    module = chat_trust.load_module(ADJUDICATION_MODULE)
    return int(module.ADJUDICATION_LIMIT) if module is not None else 0


def needs_third_read(finding: Mapping[str, Any]) -> bool:
    module = chat_trust.load_module(ADJUDICATION_MODULE)
    return module is not None and bool(module.needs_adjudication(finding))


def third_read_job(
    engine: Any, finding: Mapping[str, Any], budget: Budget | ThirdReadBudget, update: Update
) -> Callable[[], None] | None:
    """One finding's third read as work to run (its crops drawn inside it), or `None` when the
    module is missing or the finding does not need one."""
    module = chat_trust.load_module(ADJUDICATION_MODULE)
    if module is None or not module.needs_adjudication(finding):
        return None
    model = module.adjudication_model()
    read_call = _model_call(
        engine,
        budget,
        short=model,
        effort=module.ADJUDICATION_EFFORT,
        system=READ_SYSTEM,
        max_tokens=module.ADJUDICATION_MAX_TOKENS,
        prompt_tokens=module.ADJUDICATION_PROMPT_TOKENS,
        prompt_id=_prompt_id(engine, "adjudicate"),
        output_format=getattr(module, "READ_SCHEMA", None),
        sized=True,
        enforce_format=True,
    )
    pdf_paths = engine.book.pdf_paths()

    def job() -> None:
        if engine.cancel.is_set():
            return
        centred, overall = True, False
        pictures = getattr(engine, "reading_pictures", None)
        crops: dict[str, read_adjudication.SideCrops] = {}
        if callable(pictures):
            # The readings' own pictures (`read_spots`): GV's markup removed, the item's label at
            # the centre, a vendor string's overall; the centre is said only for one value.
            drawn = pictures(finding)
            crops = dict(drawn.crops)
            centred, overall = drawn.centred, drawn.overall
        if not crops:
            drawing = import_module("app.review.assistant.chat_drawing")
            with drawing._PDFIUM_LOCK:
                crops = _adjudication_crops(module, finding, pdf_paths)
            centred, overall = False, False
        if crops:
            _third_read_job(
                engine,
                module,
                finding,
                crops,
                read_call,
                model,
                update,
                centred=centred,
                overall=overall,
            )()

    return job


def critic_jobs(
    engine: Any, findings: list[dict[str, Any]], budget: Budget, update: Update
) -> list[Callable[[], None]]:
    """The critic's calls for a whole-set or full-check answer (none otherwise); the candidates
    the limit leaves out are recorded `not_run` at once."""
    book = engine.book
    critic_module = chat_trust.load_module(CRITIC_MODULE)
    judge = getattr(engine, "_whole_set_work", None)
    if callable(judge):
        whole_set = bool(judge())  # the answer's scope decides (#1520)
    else:
        whole_set = (
            bool(book.coverage_used) or getattr(engine.settings, "mode", None) == "full_check"
        )
    if not whole_set or critic_module is None or getattr(engine, "critic_off", False):
        # A roster full check judges its red candidates with the devil's advocate instead.
        return []
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
        output_format=getattr(critic_module, "CRITIC_SCHEMA", None),
        thinking=True,
    )
    jobs: list[Callable[[], None]] = []
    judged: set[str] = set()
    for batch in critic_module.batches(candidates):
        jobs.append(_critic_job(engine, critic_module, batch, critic_call, update))
        judged.update(str(f["id"]) for f in batch)
    for finding in candidates:
        if str(finding["id"]) not in judged:
            update(str(finding["id"]), _critic_not_run)
    return jobs


def _updater(engine: Any) -> Update:
    book = engine.book
    lock = getattr(engine, "finding_lock", None) or threading.Lock()

    def update(finding_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        with lock:
            current = next((f for f in book.live() if f["id"] == finding_id), None)
            if current is None:
                return
            updated = change(dict(current))
            updated["trust_level"] = trust_level(updated["checks"], updated.get("status"))
            if book.replace(updated):
                engine._show_finding(updated)

    return update


def review_reads(engine: Any) -> None:
    """The third reads and the critic for this message's findings, all at once (see above)."""
    findings = engine.book.live()
    if not findings:
        return
    budget = Budget(engine)
    update = _updater(engine)
    escalate = sorted((f for f in findings if needs_third_read(f)), key=_differences_first)
    jobs = [
        job
        for finding in escalate[: third_read_limit()]
        if (job := third_read_job(engine, finding, budget, update)) is not None
    ]
    jobs += critic_jobs(engine, findings, budget, update)
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
    *,
    centred: bool = False,
    overall: bool = False,
) -> Callable[[], None]:
    def job() -> None:
        if engine.cancel.is_set():
            return
        try:
            result = adjudication_module.adjudicate(
                finding,
                crops,
                call,
                model=model,
                second_model=_SECOND_READ_MODEL,
                centred=centred,
                overall=overall,
            )
        except Exception as failed:  # noqa: BLE001 - not run: no room, cancelled or a failure
            if not isinstance(failed, NoRoom):
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
                if not isinstance(failed, NoRoom):
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
