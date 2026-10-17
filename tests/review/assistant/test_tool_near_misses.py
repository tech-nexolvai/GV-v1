"""Tool inputs the model got slightly wrong on real runs are understood, not refused ($0, no model).

On the stored chats of 2026-10-17, 11 of the 23 refused tool calls were a `view` of a picture that
existed under a slightly different name, and one whole finding was refused because its box left
out the page. Each case here is the shape the model really wrote (names made up):

- the set pack's page folder written with the PDF's ending or as its drawings-only copy;
- a page number padded differently from the file (`-046` for `-46`);
- a pdftoppm picture whose page number was guessed, when only one picture has that name;
- a finding's box without `page` takes the finding's page.

And what must still be refused: a real guess between two files, and anything outside the folders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.sandbox import (
    SCRATCH,
    SET_PACK,
    ChatWorkspace,
    SandboxRefused,
    view_path,
)


@pytest.fixture
def workspace(tmp_path: Path) -> ChatWorkspace:
    pack = tmp_path / "pack"
    (pack / "pages" / "Made-up set").mkdir(parents=True)
    (pack / "pages" / "Made-up set" / "p-003.png").write_bytes(b"x")
    (pack / "Made-up set_drawings_only.pdf").write_bytes(b"%PDF-1.4")
    return ChatWorkspace.open(tmp_path / "chat", set_pack=pack)


@pytest.mark.parametrize(
    "asked",
    [
        f"{SET_PACK}/pages/Made-up set.pdf/p-003.png",
        f"{SET_PACK}/pages/Made-up set_drawings_only/p-003.png",
        f"{SET_PACK}/pages/Made-up set_drawings_only.pdf/p-003.png",
        f"{SET_PACK}/pages/Made-up set/p-3.png",
        f"{SET_PACK}/pages/Made-up set.pdf/p-03.png",
    ],
)
def test_set_pack_page_pictures_are_found(workspace: ChatWorkspace, asked: str) -> None:
    real = workspace.set_folder / "pages" / "Made-up set" / "p-003.png"
    assert workspace.resolve(asked) == real.resolve()


def test_a_page_folder_with_the_pdf_ending_lists_the_real_folder(workspace: ChatWorkspace) -> None:
    real = (workspace.set_folder / "pages" / "Made-up set").resolve()
    assert workspace.resolve(f"{SET_PACK}/pages/Made-up set.pdf") == real


def test_pdftoppm_page_number_and_padding_misses(workspace: ChatWorkspace) -> None:
    (workspace.home / "hC").mkdir()
    made = workspace.home / "hC" / "v27a-27.png"
    made.write_bytes(b"x")
    (workspace.home / "zoom_D_46a-46.png").write_bytes(b"x")
    # `pdftoppm -f 27 -l 27 ... v27a` makes v27a-27.png; the model asked for v27a-1.png
    assert workspace.resolve(f"{SCRATCH}/hC/v27a-1.png") == made.resolve()
    # the PDF has 60 pages, so pdftoppm pads to 2 digits; the model padded to 3
    assert (
        workspace.resolve(f"{SCRATCH}/zoom_D_46a-046.png")
        == (workspace.home / "zoom_D_46a-46.png").resolve()
    )


def test_two_candidates_are_never_guessed_between(workspace: ChatWorkspace) -> None:
    (workspace.home / "v-3.png").write_bytes(b"x")
    (workspace.home / "v-4.png").write_bytes(b"x")
    with pytest.raises(SandboxRefused) as refused:
        workspace.resolve(f"{SCRATCH}/v-1.png")
    # the refusal names what is there, so the next try is right
    assert "v-3.png" in str(refused.value) and "v-4.png" in str(refused.value)


def test_a_different_prefix_or_type_is_not_taken(workspace: ChatWorkspace) -> None:
    (workspace.home / "a27-27.png").write_bytes(b"x")
    for wrong in ("b27-27.png", "a27-27.jpg", "a27.png"):
        with pytest.raises(SandboxRefused):
            workspace.resolve(f"{SCRATCH}/{wrong}")


def test_a_near_miss_never_leaves_the_folders(workspace: ChatWorkspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret-1.png").write_bytes(b"x")
    (workspace.home / "secret-2.png").symlink_to(outside / "secret-1.png")
    with pytest.raises(SandboxRefused):
        workspace.resolve(f"{SCRATCH}/secret-9.png")
    with pytest.raises(SandboxRefused):
        workspace.resolve(f"{SCRATCH}/../outside/secret-1.png")


def test_view_says_which_file_it_showed(workspace: ChatWorkspace) -> None:
    (workspace.home / "notes-7.txt").write_text("hello\n")
    viewed = view_path(workspace, f"{SCRATCH}/notes-07.txt")
    assert viewed.text.startswith(f"[There is no {SCRATCH}/notes-07.txt; this is {SCRATCH}/notes-7")
    exact = view_path(workspace, f"{SCRATCH}/notes-7.txt")
    assert not exact.text.startswith("[There is no")


def _book(workspace: ChatWorkspace) -> FindingBook:
    listing = [{"name": "Made-up set.pdf", "pages": 9, "kind": "pdf"}]
    return FindingBook(workspace=workspace, listing=listing, page_count=9)


def test_a_box_without_a_page_takes_the_findings_page(workspace: ChatWorkspace) -> None:
    given: dict[str, Any] = {
        "page": 8,
        "vs_page": 9,
        "item": "End filler",
        "category": "cabinet",
        "architect": '2"',
        "vendor": '5"',
        "status": "different",
        "verified": True,
        "source": "architect",
        "vendor_box": {"x0": 345, "y0": 420, "x1": 670, "y1": 452},
        "arch_box": {"x0": 10, "y0": 20, "x1": 30, "y1": 40},
    }
    fields = _book(workspace)._fields(given, partial=False)
    assert fields["vendor_box"]["page"] == 8
    assert fields["arch_box"]["page"] == 9
    # a page the model gave is kept
    given["vendor_box"] = {"page": 7, "x0": 1, "y0": 2, "x1": 3, "y1": 4}
    assert _book(workspace)._fields(given, partial=False)["vendor_box"]["page"] == 7
