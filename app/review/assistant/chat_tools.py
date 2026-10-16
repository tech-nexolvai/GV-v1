"""The chat's computer and tools (#1463): claude.ai's toolset, in a locked sandbox, read-only on the review.

**The computer.** Each chat has a folder (`ChatWorkspace`) the sandbox sees with claude.ai's paths:
the drawing set's PDFs read-only at `/mnt/user-data/uploads`, the chat's home at `/home/claude` and
its outputs at `/mnt/user-data/outputs` (both kept for the whole chat), and the GV review skill
read-only at `/mnt/skills/gv-review`. No network, no key, no other package, no database.

**The tools, with claude.ai's names and fields:** `bash_tool {command, description}`, `view {path,
description, view_range?}`, `create_file {path, file_text, description}`, `str_replace {path,
old_str, new_str, description}`, `present_files {filepaths}`, `TaskCreate`, `TaskUpdate`, `Agent
{description, prompt, model?}` (run by the engine), and our `review_results {page?}`.

**Read-only is structural.** No tool reaches the review's records except `review_results`, which
reads them. `create_file` and `str_replace` write only inside `/home/claude` and
`/mnt/user-data/outputs` (a path is resolved, links included, before anything is written);
`present_files` hands over only files in the outputs folder (a file in the home folder is copied
there first), each as a copy the chat keeps, with its SHA-256.

**The prompts are short and generic, like claude.ai's** (over-prescriptive prompts made the old
harness worse): what the computer is, where the files are, the tools, and three GV facts. They never
change within a deployment: the history replay needs the same system prompt and tools every turn.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Final
from uuid import uuid4

from app.review.assistant.sandbox import (
    OUTPUTS,
    SCRATCH,
    SKILLS,
    UPLOADS,
    ChatWorkspace,
    Sandbox,
    SandboxRefused,
    check_command,
    cut_output,
    view_path,
)

__all__ = [
    "HELPER_SYSTEM_PROMPT",
    "HELPER_TOOLS",
    "LEAD_SYSTEM_PROMPT",
    "LEAD_TOOLS",
    "MAX_AGENTS",
    "ChatTools",
    "TaskList",
    "ToolResult",
    "arguments",
    "new_step",
    "step_label",
]

#: Helpers run at once, at most.
MAX_AGENTS: Final = 4
#: What a step shows of a tool's output; the model gets up to the sandbox's 20 KB.
SHOWN_OUTPUT_CHARS: Final = 4000
_RESULTS_CHARS: Final = 40_000
_MAX_FILE_CHARS: Final = 5_000_000
_MAX_PRESENTED_BYTES: Final = 200_000_000
_MAX_PRESENTED: Final = 20
_MAX_TASKS: Final = 50
_MAX_SUBJECT: Final = 120
_MAX_LABEL: Final = 160

LEAD_SYSTEM_PROMPT: Final = f"""\
You are Claude, an AI assistant made by Anthropic, working inside GV Review. Graniti Vicentia (GV) \
reviews vendors' countertop and cabinet shop drawings against the architect's drawings. A GV \
reviewer is chatting with you about one drawing set.

You have a Linux computer with no internet access:
- {UPLOADS}: the drawing set's PDFs (read-only).
- {SCRATCH}: your working folder. Its files stay for the whole chat.
- {OUTPUTS}: files for the reviewer. Make them here, then hand them over with present_files.
- {SKILLS}: how GV reviews drawings, and this set's rules (read-only). Read {SKILLS}/SKILL.md \
first, before you review, compare or mark up drawings.

Your tools: bash_tool runs a command (poppler, and Python with pypdf, pdfplumber, pypdfium2, \
Pillow, reportlab, matplotlib and openpyxl); view shows you a picture, a text file or a folder; \
create_file and str_replace write files in {SCRATCH} or {OUTPUTS}; present_files hands files to \
the reviewer; TaskCreate and TaskUpdate keep a task list the reviewer sees; Agent starts a helper \
with bash_tool, view, create_file and str_replace on this computer (up to {MAX_AGENTS} run at once: \
give each everything it needs, and check what a helper reports before you state it); \
review_results reads what GV Review's own checks recorded for this set.

Three GV facts:
- Measurements are in inches. Answer in inches.
- GV's markup (annotations GV reviewers added to the PDFs: clouds, boxes, coloured notes) is GV's \
notes, never the vendor's or the architect's numbers.
- You cannot change the review. Nothing you do records a result; the reviewer decides and signs off.

Write your answer in markdown."""

HELPER_SYSTEM_PROMPT: Final = f"""\
You are a helper agent working for Claude inside GV Review, on part of one drawing set of \
countertop and cabinet shop drawings. You share Claude's Linux computer (no internet access): the \
PDFs read-only in {UPLOADS}, the working folder {SCRATCH}, {OUTPUTS}, and GV's review skill in \
{SKILLS} (read SKILL.md there when your task is a review). Your tools: bash_tool, view, \
create_file and str_replace.

Do the task you are given. Then reply with your report in markdown: what you found, with page \
numbers and the values as printed, and what you could not read or did not get to. Measurements \
are in inches. GV's markup (annotations GV reviewers added to the PDFs) is GV's notes, never the \
vendor's or the architect's numbers. Never guess a number you cannot read."""

_BASH: Final[Mapping[str, Any]] = {
    "name": "bash_tool",
    "description": (
        "Run a bash command on the Linux computer (no internet access; a time limit per "
        "command; long output is cut at about 20 KB)."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["command", "description"],
        "properties": {
            "command": {"type": "string", "description": "Bash command to run"},
            "description": {"type": "string", "description": "Why I'm running this command"},
        },
    },
}
_VIEW: Final[Mapping[str, Any]] = {
    "name": "view",
    "description": (
        "View a picture (png, jpg, gif, webp: shown to you), a text file (numbered lines; "
        "view_range [start_line, end_line] for part of it) or a folder (two levels deep)."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "description"],
        "properties": {
            "path": {"type": "string", "description": "Absolute path to a file or folder"},
            "description": {"type": "string", "description": "Why I need to view this"},
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "Optional [start_line, end_line] of a text file (from 1; -1 = end)",
            },
        },
    },
}
_CREATE: Final[Mapping[str, Any]] = {
    "name": "create_file",
    "description": f"Create a file (or overwrite one) in {SCRATCH} or {OUTPUTS}.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["description", "path", "file_text"],
        "properties": {
            "description": {"type": "string", "description": "Why I'm creating this file"},
            "path": {"type": "string", "description": "Absolute path of the file to create"},
            "file_text": {"type": "string", "description": "The file's content"},
        },
    },
}
_REPLACE: Final[Mapping[str, Any]] = {
    "name": "str_replace",
    "description": (
        f"Replace a string in a file in {SCRATCH} or {OUTPUTS}. old_str must appear exactly once "
        "in the file."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["description", "path", "old_str"],
        "properties": {
            "description": {"type": "string", "description": "Why I'm making this edit"},
            "path": {"type": "string", "description": "Absolute path of the file to edit"},
            "old_str": {"type": "string", "description": "The text to replace (unique)"},
            "new_str": {
                "type": "string",
                "description": "The text to put in its place (empty to delete)",
            },
        },
    },
}
_PRESENT: Final[Mapping[str, Any]] = {
    "name": "present_files",
    "description": (
        f"Hand files to the reviewer as downloads. Give paths in {OUTPUTS} (a file in {SCRATCH} "
        "is copied there first)."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["filepaths"],
        "properties": {
            "filepaths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Absolute paths of the files to hand over",
            }
        },
    },
}
_TASK_CREATE: Final[Mapping[str, Any]] = {
    "name": "TaskCreate",
    "description": "Add a task to your task list (the reviewer sees it as Progress).",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["subject", "description"],
        "properties": {
            "subject": {"type": "string", "description": "A brief title for the task"},
            "description": {"type": "string", "description": "What needs to be done"},
            "activeForm": {
                "type": "string",
                "description": "Present continuous form shown while in progress",
            },
        },
    },
}
_TASK_UPDATE: Final[Mapping[str, Any]] = {
    "name": "TaskUpdate",
    "description": "Update a task's status: pending, in_progress or completed.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["taskId", "status"],
        "properties": {
            "taskId": {"type": "string", "description": "The task's id from TaskCreate"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
        },
    },
}
_AGENT: Final[Mapping[str, Any]] = {
    "name": "Agent",
    "description": (
        "Start a helper agent with bash_tool, view, create_file and str_replace on this computer. "
        f"Up to {MAX_AGENTS} Agent calls in one turn run at once; each returns its report to you."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["description", "prompt"],
        "properties": {
            "description": {
                "type": "string",
                "description": "A short (3-5 word) description of the task",
            },
            "prompt": {"type": "string", "description": "The task for the agent to perform"},
            "model": {
                "type": "string",
                "enum": ["opus", "sonnet", "haiku"],
                "description": (
                    "Optional model for this agent. Default: sonnet (opus only for a hard visual "
                    "check)"
                ),
            },
        },
    },
}
_RESULTS: Final[Mapping[str, Any]] = {
    "name": "review_results",
    "description": (
        "What GV Review's own checks recorded for this drawing set (decided by code): the review "
        "list, failures with their reasons, checks not run, the AI page review's rows and the "
        "job's values. Give a page for that page only."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "page": {"type": "integer", "description": "A page number (from 1); omit for all"}
        },
    },
}
#: The lead's tools, in a fixed order (the order is part of the cached, replayed prefix).
LEAD_TOOLS: Final[tuple[Mapping[str, Any], ...]] = (
    _BASH,
    _VIEW,
    _CREATE,
    _REPLACE,
    _PRESENT,
    _TASK_CREATE,
    _TASK_UPDATE,
    _AGENT,
    _RESULTS,
)
HELPER_TOOLS: Final[tuple[Mapping[str, Any], ...]] = (_BASH, _VIEW, _CREATE, _REPLACE)

_DEFAULT_LABELS: Final[Mapping[str, str]] = {
    "bash_tool": "Ran a command",
    "view": "Viewed a file",
    "create_file": "Created a file",
    "str_replace": "Edited a file",
    "present_files": "Presented files",
    "TaskCreate": "Added a task",
    "TaskUpdate": "Updated a task",
    "Agent": "Ran helper agents",
    "review_results": "Read the recorded results",
}


def arguments(
    raw: object, allowed: frozenset[str], required: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """A tool's input as a dict with only known fields, or `SandboxRefused`."""
    if not isinstance(raw, Mapping):
        raise SandboxRefused("the tool's input is not an object")
    unknown = set(raw) - allowed
    if unknown:
        raise SandboxRefused(f"unknown argument {min(unknown)!r}")
    missing = required - set(raw)
    if missing:
        raise SandboxRefused(f"missing argument {min(missing)!r}")
    return dict(raw)


def step_label(name: str, raw: object) -> str:
    """A step's words: the model's own `description` when it gave one."""
    described = raw.get("description") if isinstance(raw, Mapping) else None
    if name != "Agent" and isinstance(described, str) and described.strip():
        return described.strip().replace("\n", " ")[:_MAX_LABEL]
    if name == "view" and isinstance(raw, Mapping) and isinstance(raw.get("path"), str):
        return f"Viewed {raw['path']}"[:_MAX_LABEL]
    if name == "review_results" and isinstance(raw, Mapping) and isinstance(raw.get("page"), int):
        return f"Read the recorded results for page {raw['page']}"
    return _DEFAULT_LABELS.get(name, "Used a tool")


def new_step(step_id: str, name: str, raw: object) -> dict[str, Any]:
    """A step as it starts: what it is, no output yet (`ok` is null while it runs)."""
    step: dict[str, Any] = {
        "id": step_id,
        "tool": name if name in _DEFAULT_LABELS else "bash_tool",
        "label": step_label(name, raw),
        "command": None,
        "path": None,
        "output": None,
        "ok": None,
        "seconds": None,
        "picture_id": None,
        "tasks": None,
        "agents": None,
    }
    if isinstance(raw, Mapping):
        if name == "bash_tool" and isinstance(raw.get("command"), str):
            step["command"] = raw["command"]
        if name in ("view", "create_file", "str_replace") and isinstance(raw.get("path"), str):
            step["path"] = raw["path"]
    return step


@dataclass(slots=True)
class ToolResult:
    """One tool call's result for the model (`content`, `is_error`) and for the screen (`step`)."""

    content: list[dict[str, Any]]
    step: dict[str, Any]
    is_error: bool = False


def _shown(text: str) -> str:
    return cut_output(text, SHOWN_OUTPUT_CHARS)[0]


def _done(step: dict[str, Any], started: float, **changes: Any) -> dict[str, Any]:
    return {**step, **changes, "seconds": round(monotonic() - started, 1)}


def _refused(step: dict[str, Any], started: float, reason: str) -> ToolResult:
    return ToolResult(
        content=[{"type": "text", "text": f"Refused: {reason}"}],
        step=_done(step, started, ok=False, output=f"Refused: {reason}"),
        is_error=True,
    )


class TaskList:
    """The lead's task list (`TaskCreate` / `TaskUpdate`), shared by the message's turns."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def create(self, raw: object, step: dict[str, Any]) -> ToolResult:
        started = monotonic()
        try:
            given = arguments(
                raw, frozenset({"subject", "description", "activeForm"}), frozenset({"subject"})
            )
            subject = str(given["subject"]).strip()[:_MAX_SUBJECT]
            if not subject:
                raise SandboxRefused("the subject is empty")
            with self._lock:
                if len(self.tasks) >= _MAX_TASKS:
                    raise SandboxRefused(f"at most {_MAX_TASKS} tasks")
                active = given.get("activeForm")
                task = {
                    "id": str(len(self.tasks) + 1),
                    "subject": subject,
                    "active_form": str(active)[:_MAX_SUBJECT] if isinstance(active, str) else None,
                    "status": "pending",
                }
                self.tasks.append(task)
                tasks = [dict(item) for item in self.tasks]
        except SandboxRefused as refused:
            return _refused(step, started, str(refused))
        return ToolResult(
            content=[{"type": "text", "text": f"Task #{task['id']} created: {subject}"}],
            step=_done(step, started, ok=True, label=f"Added task: {subject}", tasks=tasks),
        )

    def update(self, raw: object, step: dict[str, Any]) -> ToolResult:
        started = monotonic()
        try:
            given = arguments(raw, frozenset({"taskId", "status"}), frozenset({"taskId", "status"}))
            task_id = str(given["taskId"]).lstrip("#")
            status = given["status"]
            if status not in ("pending", "in_progress", "completed"):
                raise SandboxRefused("status must be pending, in_progress or completed")
            with self._lock:
                task = next((item for item in self.tasks if item["id"] == task_id), None)
                if task is None:
                    raise SandboxRefused(f"there is no task {task_id!r}")
                task["status"] = status
                tasks = [dict(item) for item in self.tasks]
        except SandboxRefused as refused:
            return _refused(step, started, str(refused))
        verb = {"pending": "Reopened", "in_progress": "Started", "completed": "Completed"}[status]
        return ToolResult(
            content=[{"type": "text", "text": f"Task #{task_id} is now {status}."}],
            step=_done(
                step, started, ok=True, label=f"{verb} task: {task['subject']}", tasks=tasks
            ),
        )


_SAFE_NAME: Final = re.compile(r"[^A-Za-z0-9._ ()+-]")


def _file_name(path: Path) -> str:
    cleaned = _SAFE_NAME.sub("_", path.name).lstrip(".").strip()
    return (cleaned or "file")[:200]


#: Registers a handed-over file: (file id, name, size, mime, sha256) -> the file as the screen
#: shows it (`{id, name, size, mime, url}`).
type FileRegistrar = Callable[[str, str, int, str, str], dict[str, Any]]
#: The recorded results as JSON-ready data, for one page or (`None`) the whole set.
type ResultsLoader = Callable[[int | None], Mapping[str, Any]]


@dataclass(slots=True)
class ChatTools:
    """The tools of one message, over the chat's workspace and sandbox. Thread-safe enough for
    helpers: each call works on its own files; writes and copies take a lock."""

    workspace: ChatWorkspace
    sandbox: Sandbox
    page_count: int = 0
    results: ResultsLoader | None = None
    register_file: FileRegistrar | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- bash_tool -------------------------------------------------------------------------------

    def bash(self, raw: object, step: dict[str, Any]) -> ToolResult:
        started = monotonic()
        try:
            given = arguments(raw, frozenset({"command", "description"}), frozenset({"command"}))
            command = check_command(str(given.get("command", "")))
        except SandboxRefused as refused:
            return _refused(step, started, str(refused))
        try:
            result = self.sandbox.run(command)
        except Exception:  # noqa: BLE001 - the model reads it; the message goes on
            return _refused(step, started, "the computer could not run this command just now")
        status = (
            "stopped: over its time limit" if result.timed_out else f"exit code {result.exit_code}"
        )
        text = f"[{status}]\n{result.output}" if result.output else f"[{status}, no output]"
        ok = not result.timed_out and result.exit_code == 0
        return ToolResult(
            content=[{"type": "text", "text": text}],
            step=_done(step, started, ok=ok, command=command, output=_shown(result.output)),
        )

    # ---- view ------------------------------------------------------------------------------------

    def view(self, raw: object, step: dict[str, Any], max_edge: int | None = None) -> ToolResult:
        started = monotonic()
        try:
            given = arguments(
                raw, frozenset({"path", "description", "view_range"}), frozenset({"path"})
            )
            path = str(given.get("path", ""))
            viewed = view_path(self.workspace, path, given.get("view_range"), max_edge=max_edge)
        except SandboxRefused as refused:
            return _refused(step, started, str(refused))
        if viewed.png is None:
            return ToolResult(
                content=[{"type": "text", "text": viewed.text}],
                step=_done(step, started, ok=True, path=path, output=_shown(viewed.text)),
            )
        picture_id = str(uuid4())
        self.workspace.pictures.mkdir(parents=True, exist_ok=True)
        (self.workspace.pictures / f"{picture_id}.png").write_bytes(viewed.png)
        return ToolResult(
            content=[
                {"type": "text", "text": viewed.text},
                {
                    "type": "image",
                    "source": {
                        "type": "gv_picture",
                        "picture_id": picture_id,
                        "width": viewed.width,
                        "height": viewed.height,
                    },
                },
            ],
            step=_done(step, started, ok=True, path=path, picture_id=picture_id),
        )

    # ---- create_file / str_replace ---------------------------------------------------------------

    def _writable(self, path: str) -> Path:
        """`path` as a real file path inside the home or outputs folder, or `SandboxRefused`.

        A relative path is under `/home/claude`. The parent is resolved with its links, so a link
        out of the two folders is refused; the file itself may not be a link or a folder."""
        text = path.strip()
        if not text:
            raise SandboxRefused("the path is empty")
        roots = ((SCRATCH, self.workspace.home), (OUTPUTS, self.workspace.outputs))
        for shown, real in roots:
            if text == shown or text.startswith(shown + "/"):
                root, relative = real.resolve(), text[len(shown) :].lstrip("/")
                break
        else:
            if text.startswith("/"):
                raise SandboxRefused(f"files can be written only in {SCRATCH} or {OUTPUTS}")
            root, relative = self.workspace.home.resolve(), text
        if not relative:
            raise SandboxRefused("the path is a folder, not a file")
        candidate = root / relative
        parent = candidate.parent.resolve()
        if parent != root and root not in parent.parents:
            raise SandboxRefused(f"files can be written only in {SCRATCH} or {OUTPUTS}")
        target = parent / candidate.name
        if target.is_symlink():
            raise SandboxRefused("the path is a link; write the file itself")
        if target.exists() and not target.is_file():
            raise SandboxRefused("the path is a folder, not a file")
        return target

    @staticmethod
    def _write(target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o666)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)

    def create_file(self, raw: object, step: dict[str, Any]) -> ToolResult:
        started = monotonic()
        try:
            given = arguments(
                raw,
                frozenset({"description", "path", "file_text"}),
                frozenset({"path", "file_text"}),
            )
            text = given["file_text"]
            if not isinstance(text, str):
                raise SandboxRefused("file_text must be text")
            if len(text) > _MAX_FILE_CHARS:
                raise SandboxRefused(f"file_text is longer than {_MAX_FILE_CHARS} characters")
            path = str(given["path"])
            with self._lock:
                target = self._writable(path)
                existed = target.exists()
                self._write(target, text)
        except SandboxRefused as refused:
            return _refused(step, started, str(refused))
        except OSError:
            return _refused(step, started, "the file could not be written")
        shown = self.workspace.shown(target)
        verb = "Overwrote" if existed else "Created"
        note = f"{verb} {shown} ({len(text.encode('utf-8'))} bytes)"
        return ToolResult(
            content=[{"type": "text", "text": f"File {verb.lower()}: {shown}"}],
            step=_done(step, started, ok=True, path=shown, output=note),
        )

    def str_replace(self, raw: object, step: dict[str, Any]) -> ToolResult:
        started = monotonic()
        try:
            given = arguments(
                raw,
                frozenset({"description", "path", "old_str", "new_str"}),
                frozenset({"path", "old_str"}),
            )
            old, new = given["old_str"], given.get("new_str", "")
            if not isinstance(old, str) or not isinstance(new, str):
                raise SandboxRefused("old_str and new_str must be text")
            if not old:
                raise SandboxRefused("old_str is empty")
            with self._lock:
                target = self._writable(str(given["path"]))
                if not target.is_file():
                    raise SandboxRefused(f"there is no file {given['path']}")
                if target.stat().st_size > _MAX_FILE_CHARS:
                    raise SandboxRefused("the file is too large to edit; use bash_tool")
                try:
                    content = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    raise SandboxRefused("the file is not text") from None
                count = content.count(old)
                if count == 0:
                    raise SandboxRefused("old_str was not found in the file")
                if count > 1:
                    raise SandboxRefused(f"old_str appears {count} times; it must be unique")
                self._write(target, content.replace(old, new, 1))
        except SandboxRefused as refused:
            return _refused(step, started, str(refused))
        except OSError:
            return _refused(step, started, "the file could not be written")
        shown = self.workspace.shown(target)
        return ToolResult(
            content=[{"type": "text", "text": f"Replaced the text in {shown}."}],
            step=_done(step, started, ok=True, path=shown, output=f"Edited {shown}"),
        )

    # ---- present_files ---------------------------------------------------------------------------

    def present_files(self, raw: object, step: dict[str, Any]) -> ToolResult:
        started = monotonic()
        presented: list[dict[str, Any]] = []
        try:
            given = arguments(raw, frozenset({"filepaths"}), frozenset({"filepaths"}))
            paths = given["filepaths"]
            if (
                not isinstance(paths, list)
                or not paths
                or not all(isinstance(item, str) for item in paths)
            ):
                raise SandboxRefused("filepaths must be a list of paths")
            if len(paths) > _MAX_PRESENTED:
                raise SandboxRefused(f"at most {_MAX_PRESENTED} files at once")
            if self.register_file is None:
                raise SandboxRefused("files cannot be handed over here")
            outputs = self.workspace.outputs.resolve()
            home = self.workspace.home.resolve()
            for path in paths:
                real = self.workspace.resolve(path)
                if not real.is_file():
                    raise SandboxRefused(f"{path} is not a file")
                if home == real.parent or home in real.parents:
                    # A file in the home folder is copied to the outputs folder first.
                    with self._lock:
                        copied = outputs / _file_name(real)
                        if copied.is_symlink():
                            raise SandboxRefused(f"{self.workspace.shown(copied)} is a link")
                        shutil.copyfile(real, copied)
                    real = copied
                elif not (outputs == real.parent or outputs in real.parents):
                    raise SandboxRefused(
                        f"only files in {OUTPUTS} (or {SCRATCH}, copied there) can be handed over"
                    )
                size = real.stat().st_size
                if size > _MAX_PRESENTED_BYTES:
                    raise SandboxRefused(f"{path} is too large to hand over")
                file_id = str(uuid4())
                kept = self.workspace.files / file_id
                self.workspace.files.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(real, kept)
                digest = hashlib.sha256(kept.read_bytes()).hexdigest()
                name = _file_name(real)
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                presented.append(self.register_file(file_id, name, size, mime, digest))
        except SandboxRefused as refused:
            if presented:
                return _done_presented(step, started, presented, f"Stopped: {refused}")
            return _refused(step, started, str(refused))
        except OSError:
            return _refused(step, started, "a file could not be copied")
        return _done_presented(step, started, presented, None)

    # ---- review_results --------------------------------------------------------------------------

    def review_results(self, raw: object, step: dict[str, Any]) -> ToolResult:
        started = monotonic()
        try:
            page = arguments(raw, frozenset({"page"})).get("page")
            if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
                raise SandboxRefused("page must be a whole number")
            if page is not None and self.page_count and not 1 <= page <= self.page_count:
                raise SandboxRefused(
                    f"page {page} is not in this set (pages 1 to {self.page_count})"
                )
            if self.results is None:
                raise SandboxRefused("the recorded results cannot be read here")
            document = self.results(page)
        except SandboxRefused as refused:
            return _refused(step, started, str(refused))
        except Exception:  # noqa: BLE001 - a failed read is told plainly, never a crash
            return _refused(step, started, "the recorded results could not be read just now")
        text, _ = cut_output(
            json.dumps(document, ensure_ascii=False, default=str, separators=(",", ":")),
            _RESULTS_CHARS,
        )
        where = "the whole set" if page is None else f"page {page}"
        return ToolResult(
            content=[{"type": "text", "text": text}],
            step=_done(
                step, started, ok=True, label=f"Read the recorded results for {where}", output=None
            ),
        )


def _done_presented(
    step: dict[str, Any], started: float, presented: list[dict[str, Any]], problem: str | None
) -> ToolResult:
    names = ", ".join(item["name"] for item in presented)
    text = f"Handed over to the reviewer: {names}." if presented else "Nothing was handed over."
    if problem:
        text += f" {problem}"
    return ToolResult(
        content=[{"type": "text", "text": text}],
        step=_done(step, started, ok=problem is None, output=text),
        is_error=problem is not None,
    )
