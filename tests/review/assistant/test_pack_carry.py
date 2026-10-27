"""The saved pairing answers carried over a set pack's version bump (2026-10-27), $0.

On a real upload a code change bumped the pack's version, the same PDFs got a new pack, and the
set index and the page matcher were asked again: the AI answered differently and three vendor
pages that had paired went "pairing unsure". Made-up packs only:

- the same drawings, pictures and text: every saved answer is carried, marked where from;
- a changed drawing, picture or printed text, or an older question: asked again (not carried);
- answers the new pack already has are kept; no earlier pack, or an unfinished one: nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import pack_carry, set_index, set_pack

LISTING = [
    {"name": "vendor.pdf", "sha256": "a" * 64, "kind": "the vendor's shop drawings"},
    {"name": "architect.pdf", "sha256": "b" * 64, "kind": "the architect's drawings"},
]
PAGES = [
    ("vendor.pdf", 1, "vendor"),
    ("architect.pdf", 1, "architect"),
    ("architect.pdf", 2, "architect"),
]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(json.dumps(value), encoding="utf-8")


def _pack(root: Path, version: int, *, texts: dict[tuple[str, int], str] | None = None) -> Path:
    """A finished pack of the listing's uploads at `version`, with made-up pictures and text."""
    folder = root / set_pack.pack_key(LISTING, version=version)
    _write(folder / "status.json", {"state": "ready", "version": version})
    for item in LISTING:
        name = str(item["name"])
        _write(folder / f"{set_pack.stem(name)}_drawings_only.pdf", f"%PDF {name}".encode())
    _write(
        folder / "page_map.json",
        {
            "version": version,
            "files": [item["name"] for item in LISTING],
            "upload_roles": {"architect.pdf": "architect"},
            "pages": [
                {"file": f, "page": p, "halves": [{"role": role, "box": [0, 0, 800, 600]}]}
                for f, p, role in PAGES
            ],
        },
    )
    shown = texts or {}
    _write(
        folder / "page_texts.json",
        {
            "pages": [
                {"file": f, "page": p, "text": shown.get((f, p), f"KITCHEN {f} {p}")}
                for f, p, _role in PAGES
            ]
        },
    )
    _write(folder / "annotations.json", [])
    for f, p, _role in PAGES:
        _write(folder / "pages" / set_pack.stem(f) / f"p-{p:03d}.png", f"png {f} {p}".encode())
    return folder


def _answers(folder: Path, *, prompt_version: int = set_index.PROMPT_VERSION) -> None:
    pages = {
        f"{f}#{p}": {"file": f, "page": p, "role": role, "prompt_version": prompt_version}
        for f, p, role in PAGES
    }
    _write(folder / "set_index.json", {"version": 1, "pages": pages})
    _write(
        folder / "page_match.json",
        {"version": 1, "answers": {"p1.v0": {"matches": ["p1001"], "prompt_version": 1}}},
    )
    _write(folder / "blind_picks.json", {"k1": {"answer": {"pick": "p2.D3"}, "model": "m"}})


def _read(folder: Path, name: str) -> Any:
    return json.loads((folder / name).read_text(encoding="utf-8"))


def test_the_pack_key_of_this_version_is_unchanged() -> None:
    assert set_pack.pack_key(LISTING) == set_pack.pack_key(LISTING, version=set_pack.PACK_VERSION)
    assert set_pack.pack_key(LISTING) != set_pack.pack_key(LISTING, version=1)


def test_the_same_drawings_carry_every_saved_answer(tmp_path: Path) -> None:
    old = _pack(tmp_path, set_pack.PACK_VERSION - 1)
    _answers(old)
    new = _pack(tmp_path, set_pack.PACK_VERSION)
    report = pack_carry.carry_over(new, LISTING)
    assert (report.source, report.index_pages, report.match_answers, report.picks) == (
        old.name,
        3,
        1,
        1,
    )
    pages = _read(new, "set_index.json")["pages"]
    assert sorted(pages) == sorted(f"{f}#{p}" for f, p, _r in PAGES)
    assert all(entry[pack_carry.CARRIED_FROM] == old.name for entry in pages.values())
    assert set_index.load(new)["architect.pdf#2"]["prompt_version"] == set_index.PROMPT_VERSION
    assert _read(new, "page_match.json")["answers"]["p1.v0"]["matches"] == ["p1001"]
    assert _read(new, "blind_picks.json")["k1"]["answer"]["pick"] == "p2.D3"


def test_an_older_version_further_back_is_found(tmp_path: Path) -> None:
    old = _pack(tmp_path, set_pack.PACK_VERSION - 3)
    _answers(old)
    new = _pack(tmp_path, set_pack.PACK_VERSION)
    assert pack_carry.earlier_pack(new, LISTING) == old
    # A newer one that was not finished is passed over.
    unfinished = _pack(tmp_path, set_pack.PACK_VERSION - 1)
    _write(unfinished / "status.json", {"state": "building"})
    assert pack_carry.earlier_pack(new, LISTING) == old


def test_a_changed_drawing_asks_everything_again(tmp_path: Path) -> None:
    old = _pack(tmp_path, set_pack.PACK_VERSION - 1)
    _answers(old)
    new = _pack(tmp_path, set_pack.PACK_VERSION)
    _write(new / "architect_drawings_only.pdf", b"%PDF another drawing")
    report = pack_carry.carry_over(new, LISTING)
    assert report.source is None and report.skipped == ["the drawings-only copies differ"]
    for name in ("set_index.json", "page_match.json", "blind_picks.json"):
        assert not (new / name).exists()


def test_a_changed_picture_asks_that_page_and_the_matcher_again(tmp_path: Path) -> None:
    old = _pack(tmp_path, set_pack.PACK_VERSION - 1)
    _answers(old)
    new = _pack(tmp_path, set_pack.PACK_VERSION)
    _write(new / "pages" / "architect" / "p-002.png", b"png drawn another way")
    report = pack_carry.carry_over(new, LISTING)
    assert report.index_pages == 2 and "architect.pdf#2" not in set_index.load(new)
    assert report.match_answers == 0 and not (new / "page_match.json").exists()
    assert report.picks == 0  # a pick's key holds no pixels: asked again
    assert not (new / "blind_picks.json").exists()


def test_a_changed_printed_text_asks_that_page_again(tmp_path: Path) -> None:
    old = _pack(tmp_path, set_pack.PACK_VERSION - 1)
    _answers(old)
    new = _pack(tmp_path, set_pack.PACK_VERSION, texts={("vendor.pdf", 1): "BATH VANITY"})
    report = pack_carry.carry_over(new, LISTING)
    assert report.index_pages == 2 and "vendor.pdf#1" not in set_index.load(new)


def test_an_older_question_is_asked_again(tmp_path: Path) -> None:
    old = _pack(tmp_path, set_pack.PACK_VERSION - 1)
    _answers(old, prompt_version=set_index.PROMPT_VERSION - 1)
    new = _pack(tmp_path, set_pack.PACK_VERSION)
    assert pack_carry.carry_over(new, LISTING).index_pages == 0


def test_answers_the_new_pack_has_are_kept(tmp_path: Path) -> None:
    old = _pack(tmp_path, set_pack.PACK_VERSION - 1)
    _answers(old)
    new = _pack(tmp_path, set_pack.PACK_VERSION)
    mine = {"file": "vendor.pdf", "page": 1, "prompt_version": set_index.PROMPT_VERSION}
    _write(new / "set_index.json", {"version": 1, "pages": {"vendor.pdf#1": mine}})
    _write(new / "page_match.json", {"version": 1, "answers": {"p1.v0": {"matches": []}}})
    pack_carry.carry_over(new, LISTING)
    assert set_index.load(new)["vendor.pdf#1"] == mine
    assert _read(new, "page_match.json")["answers"]["p1.v0"]["matches"] == []


@pytest.mark.parametrize("state", [None, "failed"])
def test_no_finished_earlier_pack_carries_nothing(tmp_path: Path, state: str | None) -> None:
    if state is not None:
        old = _pack(tmp_path, set_pack.PACK_VERSION - 1)
        _answers(old)
        _write(old / "status.json", {"state": state})
    new = _pack(tmp_path, set_pack.PACK_VERSION)
    report = pack_carry.carry_over(new, LISTING)
    assert report == pack_carry.CarryReport()
    assert not (new / "set_index.json").exists()
