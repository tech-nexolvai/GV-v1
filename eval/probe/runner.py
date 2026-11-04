"""Running a probe: the plan of calls, and the one place money is spent (Phase 1).

The probe asks the **running app** to do the check, exactly as the screen does, with the page list
the selection chose. That is deliberate:

- the full check already takes a page list (`ChatMessageIn.pages`, added 2026-10-31 so a fix can
  be tested on the pages it touches) and cuts the plan to it, so the probe needs no new code path
  in the product and measures the real finding stage, not a copy of it;
- the answers come back in exactly the shape the exam already reads, so `eval.exam.replay`'s
  loader and miss matcher score a probe with no changes;
- the app holds the provider key, the rate limiter and the one-paid-job-at-a-time slot, so the
  probe cannot start a second paid job behind the owner's back.

**It never starts the app.** A probe that started a server would be running code nobody chose,
under settings nobody saw. `preflight` checks the app is answering and stops if it is not.

**It writes nothing to the database itself.** The app stores the chat message as it does for every
check; the probe only reads the stream and saves it to disk.

`plan` is the whole of the dry run: it builds the real request body, validates it against the
product's own contract, prices it, and prints the calls it would make. Nothing is sent.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from eval.probe.config import Arm
from eval.probe.cost import Estimate, estimate
from eval.probe.pages import Selection

__all__ = ["Plan", "RunResult", "ask", "fetch_chat", "plan_for", "probe_question"]

_PREFIX: Final = "/api/v1"

#: What the probe asks for. A full check does the asking itself from the drawings; the text only
#: has to say what the check is, and carries no client value.
probe_question: Final = (
    "Run the full check on the pages picked for this message and report every problem you find."
)


@dataclass(frozen=True)
class Plan:
    """What one probe run would do, priced, with nothing sent."""

    job: str
    arm: str
    pages: tuple[int, ...]
    control_pages: tuple[int, ...]
    body: dict[str, Any]
    estimate: Estimate
    warnings: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "arm": self.arm,
            "pages": list(self.pages),
            "control_pages": list(self.control_pages),
            "request_body": self.body,
            "estimate": {
                "per_run_usd": round(self.estimate.cost_usd, 2),
                "per_run_range_usd": [
                    round(self.estimate.low_usd, 2),
                    round(self.estimate.high_usd, 2),
                ],
                "per_run_minutes": round(self.estimate.seconds / 60, 1),
                "ceiling_per_run_usd": round(self.estimate.ceiling_usd, 2),
                "ceiling_from": self.estimate.ceiling_from,
                "runs": self.estimate.runs,
                "experiment_usd": round(self.estimate.total_cost_usd, 2),
                "experiment_ceiling_usd": round(self.estimate.total_ceiling_usd, 2),
                "experiment_minutes": round(self.estimate.total_seconds / 60, 1),
                "fitted_on_runs": self.estimate.fitted_on,
            },
            "warnings": list(self.warnings),
            "calls": list(self.calls),
        }


def _validated(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Put the request through the product's own contract, so a dry run catches a bad ask.

    This is most of what the paid path does before it spends anything: if the page list is too
    long, a page is repeated, or the mode does not allow a page list, it is refused here for free.
    """
    try:
        from app.review.assistant.chat_contract import ChatMessageIn

        checked = ChatMessageIn.model_validate(body)
        return checked.model_dump(mode="json", exclude_none=True), []
    except Exception as refused:  # noqa: BLE001 - every refusal is a dry-run finding
        return body, [f"the product's own contract refuses this request: {refused}"]


def plan_for(
    selection: Selection,
    arm: Arm,
    *,
    repeats: int = 1,
    arms: int = 1,
    cap_usd: float | None = None,
) -> Plan:
    """Price one arm's probe and build the request it would send. Nothing is sent."""
    pages = selection.pages
    priced = estimate(len(pages), repeats=repeats, arms=arms)
    body: dict[str, Any] = {
        "text": probe_question,
        "model": arm.model,
        "effort": arm.effort,
        "thinking": True,
        "mode": "full_check",
        "pages": list(pages),
        "run_anyway": True,
    }
    if arm.products:
        body["products"] = list(arm.products)
    body["cap_usd"] = float(cap_usd) if cap_usd is not None else round(priced.ceiling_usd + 0.5, 2)
    body, refusals = _validated(body)
    warnings = [*arm.warnings(), *refusals]
    if priced.extrapolated:
        warnings.append(
            f"{len(pages)} pages is more than any picked run the price is fitted to, so the "
            f"estimate is an extrapolation: read the range, not the middle"
        )
    if not selection.problem_pages:
        warnings.append("no page carries a known problem: this probe can measure nothing")
    if not selection.control_pages:
        warnings.append(
            "no control page: recall can be bought by flagging everything and nothing here "
            "would show it"
        )
    if selection.uncovered:
        warnings.append(
            f"{len(selection.uncovered)} asked-for problems are not on these pages and are left "
            f"out of the score: {', '.join(selection.uncovered)}"
        )
    if selection.partly:
        warnings.append(
            f"{len(selection.partly)} problems have only some of their sheets here, so they are "
            f"not a fair test and are not scored: {', '.join(selection.partly)}"
        )
    calls = (
        f"POST {_PREFIX}/packages/<package>/chats  (only when --chat is not given)",
        f"POST {_PREFIX}/chats/<chat>/messages  with mode=full_check and {len(pages)} picked pages",
        (
            f"one page reviewer per picked vendor page ({len(pages)}), plus the plan, the "
            f"pairing and the finishing steps"
        ),
        f"repeated {repeats} time(s) per arm, {arms} arm(s), one run at a time",
    )
    return Plan(
        job=selection.job,
        arm=arm.name,
        pages=pages,
        control_pages=selection.control_pages,
        body=body,
        estimate=priced,
        warnings=tuple(warnings),
        calls=calls,
    )


@dataclass
class RunResult:
    """One finished probe run."""

    chat: str
    message: str
    events: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    cost_usd: float = 0.0
    stopped: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat": self.chat,
            "message": self.message,
            "seconds": round(self.seconds),
            "cost_usd": round(self.cost_usd, 2),
            "stopped": self.stopped,
            "error": self.error,
            "events": len(self.events),
        }


def _request(
    api: str, path: str, headers: dict[str, str], body: dict[str, Any] | None
) -> urllib.request.Request:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{api.rstrip('/')}{_PREFIX}{path}", data=data, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("accept", "text/event-stream, application/json")
    for name, value in headers.items():
        request.add_header(name, value)
    return request


def _events(lines: Iterator[bytes]) -> Iterator[tuple[str, Any]]:
    """Server-Sent Events as (event, data); keepalive comments skipped."""
    event: str = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").strip())


def fetch_chat(api: str, chat: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """The stored chat, in the shape the exam's own loader reads.

    Saving this beside the stream is what lets `eval.exam.replay.findings_of` score a probe with
    no changes: a probe's output file is the same kind of file as every saved run in the exam.
    """
    request = urllib.request.Request(f"{api.rstrip('/')}{_PREFIX}/chats/{chat}", method="GET")
    request.add_header("accept", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=120) as response:
        loaded = json.loads(response.read().decode("utf-8") or "null")
    return loaded if isinstance(loaded, dict) else {}


def ask(
    api: str,
    *,
    package: str | None,
    chat: str | None,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    out: Path | None = None,
    timeout_s: int = 7200,
) -> RunResult:
    """Send one probe run to the running app and keep everything it streams back.

    **This spends money.** It is reached only from `scripts/recall_probe.py run --spend`, after the
    preflight has passed and the owner has approved the estimate.
    """
    headers = dict(headers or {})
    if chat is None:
        if not package:
            raise ValueError("a probe needs either an existing chat or a package to start one in")
        with urllib.request.urlopen(
            _request(api, f"/packages/{package}/chats", headers, {}), timeout=60
        ) as response:
            chat = str(json.loads(response.read().decode("utf-8"))["id"])

    result = RunResult(chat=chat, message="")
    started = time.monotonic()
    stream = None
    try:
        stream = urllib.request.urlopen(
            _request(api, f"/chats/{chat}/messages", headers, body), timeout=timeout_s
        )
        for name, data in _events(stream):
            result.events.append({"event": name, "data": data})
            if out is not None:
                with out.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"event": name, "data": data}) + "\n")
            if isinstance(data, dict):
                if name == "message_start" and data.get("id"):
                    result.message = str(data["id"])
                for key in ("cost_usd", "spent_usd", "total_usd"):
                    value = data.get(key)
                    if isinstance(value, int | float):
                        result.cost_usd = max(result.cost_usd, float(value))
                if name == "message_end":
                    result.stopped = bool(data.get("stopped"))
                if name == "error":
                    result.error = str(data.get("message") or "the run failed")
    except (urllib.error.URLError, OSError, ValueError) as failed:
        result.error = str(failed)
    finally:
        if stream is not None:
            stream.close()
        result.seconds = time.monotonic() - started
    return result
