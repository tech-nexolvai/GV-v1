"""The chat's computer (#1463): its folders, the write tools' boundaries, handing files over.

- Writes land only in /home/claude and /mnt/user-data/outputs; the PDFs, the skill, any other path
  and any link out of the two folders are refused, and nothing is written.
- `str_replace` needs its text exactly once; `present_files` hands over outputs only (a home file is
  copied there first), each as a copy with its SHA-256.
- Docker sees four folders, the PDFs and the skill read-only; on a Mac, `sandbox-exec` sees the same
  paths.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_tools import ChatTools, new_step
from app.review.assistant.sandbox import (
    OUTPUTS,
    PYTHON_STARTUP,
    SCRATCH,
    SET_PACK,
    SKILLS,
    UPLOADS,
    ChatWorkspace,
    DockerSandbox,
    MacSandbox,
)
from tests.review.assistant.chat_fakes import FakeSandbox


@pytest.fixture
def workspace(tmp_path: Path) -> ChatWorkspace:
    opened = ChatWorkspace.open(tmp_path / "chat")
    (opened.uploads / "set.pdf").write_bytes(b"%PDF-1.7 made up")
    (opened.skills / "SKILL.md").write_text("# skill")
    return opened


def _tools(workspace: ChatWorkspace) -> tuple[ChatTools, list[dict[str, Any]]]:
    handed: list[dict[str, Any]] = []

    def register(fid: str, name: str, size: int, mime: str, sha: str) -> dict[str, Any]:
        shown = {"id": fid, "name": name, "size": size, "mime": mime, "sha256": sha}
        handed.append(shown)
        return shown

    return ChatTools(workspace, FakeSandbox(workspace), register_file=register), handed


def _create(tools: ChatTools, path: str, text: str = "x") -> Any:
    raw = {"path": path, "file_text": text, "description": "d"}
    return tools.create_file(raw, new_step("s", "create_file", raw))


def test_the_chat_folder_has_claude_ais_four_folders(workspace: ChatWorkspace) -> None:
    assert [(m.shown, m.writable) for m in workspace.mounts] == [
        (UPLOADS, False),
        (SCRATCH, True),
        (OUTPUTS, True),
        (SKILLS, False),
        (SET_PACK, False),
    ]
    assert workspace.home.stat().st_mode & 0o777 == 0o777
    assert workspace.resolve("/mnt/skills/gv-review/SKILL.md") == workspace.skills / "SKILL.md"
    assert workspace.resolve("/mnt/user-data/uploads/set.pdf") == (workspace.uploads / "set.pdf")


@pytest.mark.parametrize(
    "path",
    [
        "/home/claude/notes.md",
        "notes.md",
        "/mnt/user-data/outputs/deep/er/report.csv",
    ],
)
def test_files_are_written_in_home_and_outputs(workspace: ChatWorkspace, path: str) -> None:
    tools, _ = _tools(workspace)

    result = _create(tools, path, "hello")

    assert not result.is_error, result.content
    real = workspace.resolve(path)
    assert real.read_text() == "hello"
    assert result.step["path"] in (path, f"{SCRATCH}/{path}")


@pytest.mark.parametrize(
    "path",
    [
        "/mnt/user-data/uploads/set.pdf",
        "/mnt/user-data/uploads/new.pdf",
        "/mnt/skills/gv-review/SKILL.md",
        "/etc/passwd",
        "/home/claude/../../escape.txt",
        "../escape.txt",
        "/mnt/user-data/outputs/../uploads/x",
        "/home/claude",
        "",
    ],
)
def test_writes_anywhere_else_are_refused(workspace: ChatWorkspace, path: str) -> None:
    tools, _ = _tools(workspace)
    before = sorted(p.name for p in workspace.root.rglob("*"))

    result = _create(tools, path)

    assert result.is_error and result.step["ok"] is False
    assert result.content[0]["text"].startswith("Refused:")
    assert sorted(p.name for p in workspace.root.rglob("*")) == before
    assert (workspace.uploads / "set.pdf").read_bytes() == b"%PDF-1.7 made up"


def test_a_link_out_of_the_folders_is_refused(workspace: ChatWorkspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.home / "door").symlink_to(outside)
    (workspace.home / "file-link").symlink_to(outside / "target.txt")
    tools, _ = _tools(workspace)

    through_folder = _create(tools, "/home/claude/door/x.txt")
    onto_link = _create(tools, "/home/claude/file-link")

    assert through_folder.is_error and onto_link.is_error
    assert list(outside.iterdir()) == []


def test_str_replace_needs_its_text_exactly_once(workspace: ChatWorkspace) -> None:
    tools, _ = _tools(workspace)
    _create(tools, "/home/claude/a.py", "x = 1\ny = 1\n")

    def replace(old: str, new: str = "z") -> Any:
        raw = {"path": "/home/claude/a.py", "old_str": old, "new_str": new, "description": "d"}
        return tools.str_replace(raw, new_step("s", "str_replace", raw))

    assert "2 times" in replace("= 1").content[0]["text"]
    assert "not found" in replace("nothing").content[0]["text"]
    assert not replace("x = 1", "x = 2").is_error
    assert (workspace.home / "a.py").read_text() == "x = 2\ny = 1\n"
    raw = {"path": "/mnt/user-data/uploads/set.pdf", "old_str": "PDF", "description": "d"}
    assert tools.str_replace(raw, new_step("s", "str_replace", raw)).is_error


def test_present_files_hands_over_copies_and_copies_home_files_to_outputs(
    workspace: ChatWorkspace,
) -> None:
    tools, handed = _tools(workspace)
    _create(tools, "/mnt/user-data/outputs/marked.pdf", "%PDF made by the agent")
    _create(tools, "/home/claude/notes.csv", "a,b\n")
    raw = {"filepaths": ["/mnt/user-data/outputs/marked.pdf", "/home/claude/notes.csv"]}

    result = tools.present_files(raw, new_step("s", "present_files", raw))

    assert not result.is_error, result.content
    assert [item["name"] for item in handed] == ["marked.pdf", "notes.csv"]
    assert handed[0]["mime"] == "application/pdf"
    assert (workspace.outputs / "notes.csv").read_text() == "a,b\n"
    for item in handed:
        kept = workspace.files / item["id"]
        assert hashlib.sha256(kept.read_bytes()).hexdigest() == item["sha256"]
    # A later change to the output does not change what was handed over.
    _create(tools, "/mnt/user-data/outputs/marked.pdf", "changed")
    assert (workspace.files / handed[0]["id"]).read_text() == "%PDF made by the agent"


@pytest.mark.parametrize(
    "path", ["/mnt/user-data/uploads/set.pdf", "/mnt/skills/gv-review/SKILL.md", "/etc/hosts"]
)
def test_present_files_refuses_anything_but_outputs_and_home(
    workspace: ChatWorkspace, path: str
) -> None:
    tools, handed = _tools(workspace)
    raw = {"filepaths": [path]}

    result = tools.present_files(raw, new_step("s", "present_files", raw))

    assert result.is_error and handed == []


def test_present_files_refuses_more_than_its_total_and_the_chats_total(
    workspace: ChatWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handed-over files are copies on the host's disk: one call may hand over 200 MB together,
    and a chat 1 GB in all (security audit 2026-10-18). Measured with small made-up limits."""
    from app.review.assistant import chat_tools

    monkeypatch.setattr(chat_tools, "_MAX_PRESENTED_TOTAL_BYTES", 1_500)
    tools, handed = _tools(workspace)
    for name in ("a.csv", "b.csv"):
        (workspace.outputs / name).write_text("x" * 1_000)
    raw = {"filepaths": ["/mnt/user-data/outputs/a.csv", "/mnt/user-data/outputs/b.csv"]}
    result = tools.present_files(raw, new_step("s", "present_files", raw))
    assert "larger than 0 MB together" in str(result.content)
    assert [item["name"] for item in handed] == ["a.csv"]  # the first one went over before

    monkeypatch.setattr(chat_tools, "_MAX_PRESENTED_TOTAL_BYTES", 10_000_000)
    monkeypatch.setattr(chat_tools, "MAX_FOLDER_BYTES", 1)
    raw = {"filepaths": ["/mnt/user-data/outputs/b.csv"]}
    result = tools.present_files(raw, new_step("s", "present_files", raw))
    assert result.is_error and "handed over" in str(result.content)
    assert len(handed) == 1


def test_view_reads_the_skill_and_refuses_other_paths(workspace: ChatWorkspace) -> None:
    tools, _ = _tools(workspace)
    raw = {"path": "/mnt/skills/gv-review/SKILL.md", "description": "Read the skill"}
    assert "# skill" in tools.view(raw, new_step("s", "view", raw)).content[0]["text"]
    raw = {"path": "/etc/hosts", "description": "x"}
    assert tools.view(raw, new_step("s", "view", raw)).is_error


def test_docker_sees_four_folders_with_the_pdfs_and_skill_read_only(
    workspace: ChatWorkspace,
) -> None:
    argv = DockerSandbox(workspace, cpus="2", memory="2g").argv("ls", "gv-assistant-test")
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--mount"]

    assert mounts == [
        f"type=bind,source={workspace.uploads},target={UPLOADS},readonly",
        f"type=bind,source={workspace.home},target={SCRATCH}",
        f"type=bind,source={workspace.outputs},target={OUTPUTS}",
        f"type=bind,source={workspace.skills},target={SKILLS},readonly",
        f"type=bind,source={workspace.root / 'set-pending'},target={SET_PACK},readonly",
        f"type=bind,source={workspace.root / 'python'},target={PYTHON_STARTUP},readonly",
    ]
    joined = " ".join(argv)
    assert "--network none" in joined and "--read-only" in joined and "--memory 2g" in joined
    assert [argv[i + 1] for i, item in enumerate(argv) if item == "--env"] == [
        f"HOME={SCRATCH}",
        f"PYTHONPATH={PYTHON_STARTUP}",
    ]


def test_kill_all_stops_the_running_containers(workspace: ChatWorkspace) -> None:
    sandbox = DockerSandbox(workspace, docker="/nonexistent/docker")
    sandbox._running.add("gv-assistant-abc")

    sandbox.kill_all()  # the docker command is missing: harmless, never raises


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec only")
def test_on_a_mac_the_four_paths_work_and_only_two_are_writable(workspace: ChatWorkspace) -> None:
    sandbox = MacSandbox(workspace, timeout_seconds=20)

    wrote = sandbox.run(
        f"echo hi > {SCRATCH}/a.txt && echo ok > {OUTPUTS}/b.txt && cat {SKILLS}/SKILL.md "
        f"&& ls {UPLOADS}"
    )
    refused = sandbox.run(f"echo x > {UPLOADS}/c.txt")

    assert wrote.exit_code == 0, wrote.output
    assert "# skill" in wrote.output and "set.pdf" in wrote.output
    assert (workspace.home / "a.txt").read_text().strip() == "hi"
    assert (workspace.outputs / "b.txt").read_text().strip() == "ok"
    assert refused.exit_code != 0 and not (workspace.uploads / "c.txt").exists()
