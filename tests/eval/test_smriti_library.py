"""The key library: one local list of every answer key (#1268).

Verification for: `eval/smriti/library.py`. Invented keys in temporary folders only; no client
value, no model call, no database.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eval.pariksha.keys import KEY_FORMAT
from eval.smriti.library import (
    KEY_KINDS,
    LIBRARY_FORMAT,
    LIBRARY_SETTING,
    Library,
    LibraryError,
    describe,
    key_pages,
    library_path,
    load_entry_key,
    read_library,
    register,
    write_library,
)
from tests.eval.test_pariksha_depth import depth_key, view_key
from tests.eval.test_pariksha_kinds import key_document as kinds_key_document
from tests.eval.test_pariksha_walls import key_document as walls_key_document

REPO = Path(__file__).resolve().parents[2]
PAGE = "a" * 64
OTHER_PAGE = "b" * 64
WHO = "a made-up person"
LABEL = "invented for a test, not human-confirmed"


def _label_key(
    name: str = "made-up labels", text: str = '3/4"', page: str = PAGE
) -> dict[str, Any]:
    return {
        "format": KEY_FORMAT,
        "agent": "akshara",
        "name": name,
        "version": "1",
        "cases": [
            {
                "family": "claude-slot-span",
                "page_sha256": page,
                "question_id": "p1:slot0:0",
                "slot_box": "10,20,30,40",
                "answer": {"texts": [text]},
            }
        ],
    }


def _write(folder: Path, name: str, document: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(document))
    return path


def _registered(library: Library, path: Path, *, label: str = LABEL) -> Library:
    entry, key = describe(path, confirmed_by=WHO, label=label)
    return register(library, entry, key)


def _empty(tmp_path: Path) -> Library:
    return read_library(tmp_path / "shelf" / "library.json", missing_ok=True)


# --- what an entry records -------------------------------------------------------------------


def test_an_entry_records_agent_path_kind_who_confirmed_it_and_its_label(tmp_path: Path) -> None:
    key = _write(tmp_path / "keys", "labels.json", _label_key())
    library = _registered(_empty(tmp_path), key)

    (entry,) = library.entries
    assert entry.agent == "akshara"
    assert entry.kind == KEY_KINDS["akshara"] == "labels"
    assert entry.path == str(key.resolve())
    assert entry.confirmed_by == WHO
    assert entry.label == LABEL
    assert entry.name == "made-up labels"
    assert entry.version == "1"
    assert entry.cases == 1
    assert len(entry.sha256) == 64


def test_the_library_round_trips_through_its_file_and_nothing_moves(tmp_path: Path) -> None:
    key = _write(tmp_path / "keys", "labels.json", _label_key())
    before = key.read_bytes()
    library = _registered(_empty(tmp_path), key)
    write_library(library)

    again = read_library(library.path)
    assert again.entries == library.entries
    assert json.loads(library.path.read_text())["format"] == LIBRARY_FORMAT
    # Registering never moves or rewrites the key itself.
    assert key.read_bytes() == before
    assert load_entry_key(again.entries[0]).sha256 == again.entries[0].sha256


@pytest.mark.parametrize(
    ("name", "document", "agent"),
    [
        ("walls.json", walls_key_document(), "rekha-walls"),
        ("kinds.json", kinds_key_document(), "bala-kinds"),
        ("depth.json", depth_key(), "akshara-depth"),
        ("views.json", view_key(), "rekha-view"),
        (
            "architect.json",
            {
                "format": KEY_FORMAT,
                "agent": "vastu",
                "name": "made-up architect",
                "version": "1",
                "document_sha256": "c" * 64,
                "pages": [{"page": 1, "arch_widths": [{"text": "2'-0\"", "in": 24}]}],
            },
            "vastu",
        ),
    ],
)
def test_every_kind_of_key_registers_through_its_own_loader(
    tmp_path: Path, name: str, document: dict[str, Any], agent: str
) -> None:
    library = _registered(_empty(tmp_path), _write(tmp_path / "keys", name, document))

    (entry,) = library.entries
    assert entry.agent == agent
    assert entry.kind == KEY_KINDS[agent]
    assert entry.cases > 0
    assert key_pages(load_entry_key(entry))


def test_a_file_that_is_no_pariksha_key_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path / "keys", "other.json", {"drawing": "made-up.pdf", "pages": []})
    with pytest.raises(LibraryError, match="pariksha-key/1"):
        describe(path, confirmed_by=WHO, label=LABEL)


@pytest.mark.parametrize("field", ["confirmed_by", "label"])
def test_who_confirmed_it_and_its_label_are_required(tmp_path: Path, field: str) -> None:
    path = _write(tmp_path / "keys", "labels.json", _label_key())
    given = {"confirmed_by": WHO, "label": LABEL, field: " "}
    with pytest.raises(LibraryError, match=field):
        describe(path, **given)


# --- where the list lives --------------------------------------------------------------------


def test_the_library_path_has_no_default(tmp_path: Path) -> None:
    with pytest.raises(LibraryError, match="no default"):
        library_path(None, environ={})
    assert library_path(None, environ={LIBRARY_SETTING: str(tmp_path / "l.json")}) == (
        tmp_path / "l.json"
    )
    assert library_path(tmp_path / "given.json", environ={LIBRARY_SETTING: "x"}) == (
        tmp_path / "given.json"
    )


def test_an_output_path_inside_a_git_checkout_is_refused(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    key = _write(tmp_path / "keys", "labels.json", _label_key())
    for inside in (checkout / "deep" / "library.json", REPO / "library.json"):
        # Reading (list, compare) is refused as well as writing (register).
        with pytest.raises(LibraryError, match="inside a git"):
            read_library(inside, missing_ok=True)
        library = Library(path=inside, entries=_empty(tmp_path).entries)
        library = _registered(library, key)
        with pytest.raises(LibraryError, match="inside a git"):
            write_library(library)
        assert not inside.exists()


def test_a_key_inside_a_git_checkout_is_refused(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    inside = _write(checkout / "ignored", "labels.json", _label_key())
    with pytest.raises(LibraryError, match="inside a git"):
        describe(inside, confirmed_by=WHO, label=LABEL)

    # A hand edit pointing an entry at it is refused when the list is read.
    outside = _write(tmp_path / "keys", "labels.json", _label_key())
    library = _registered(_empty(tmp_path), outside)
    write_library(library)
    document = json.loads(library.path.read_text())
    document["entries"][0]["path"] = str(inside.resolve())
    library.path.write_text(json.dumps(document))
    with pytest.raises(LibraryError, match="inside a git"):
        read_library(library.path)


def test_a_hand_edited_library_with_two_answers_to_one_question_is_refused(
    tmp_path: Path,
) -> None:
    library = _registered(_empty(tmp_path), _write(tmp_path / "a", "k.json", _label_key()))
    clash = _write(tmp_path / "b", "k.json", _label_key(name="another", text='1/2"'))
    clash_entry, _key = describe(clash, confirmed_by=WHO, label=LABEL)
    write_library(library)
    document = json.loads(library.path.read_text())
    added = {name: getattr(clash_entry, name) for name in document["entries"][0]}
    document["entries"].append(added)
    library.path.write_text(json.dumps(document))
    with pytest.raises(LibraryError, match="different answer"):
        read_library(library.path)


# --- conflicts -------------------------------------------------------------------------------


def test_registering_the_same_key_again_changes_nothing(tmp_path: Path) -> None:
    key = _write(tmp_path / "keys", "labels.json", _label_key())
    library = _registered(_empty(tmp_path), key)
    assert _registered(library, key) == library


def test_the_same_key_with_another_label_is_refused(tmp_path: Path) -> None:
    key = _write(tmp_path / "keys", "labels.json", _label_key())
    library = _registered(_empty(tmp_path), key)
    with pytest.raises(LibraryError, match="already registered"):
        _registered(library, key, label="another label")


def test_a_key_file_changed_since_it_was_registered_is_refused(tmp_path: Path) -> None:
    key = _write(tmp_path / "keys", "labels.json", _label_key())
    library = _registered(_empty(tmp_path), key)
    key.write_text(json.dumps(_label_key(text='1/2"')))

    with pytest.raises(LibraryError, match="changed since"):
        load_entry_key(library.entries[0])
    with pytest.raises(LibraryError, match="changed since"):
        _registered(library, key)


def test_one_name_and_version_with_other_answers_is_refused(tmp_path: Path) -> None:
    library = _registered(_empty(tmp_path), _write(tmp_path / "a", "k.json", _label_key()))
    other = _write(tmp_path / "b", "k.json", _label_key(text='1/2"', page=OTHER_PAGE))
    with pytest.raises(LibraryError, match="name and version"):
        _registered(library, other)


def test_the_same_key_at_a_second_path_is_refused(tmp_path: Path) -> None:
    library = _registered(_empty(tmp_path), _write(tmp_path / "a", "k.json", _label_key()))
    with pytest.raises(LibraryError, match="same key"):
        _registered(library, _write(tmp_path / "b", "copy.json", _label_key()))


def test_two_keys_giving_one_question_different_answers_are_refused(tmp_path: Path) -> None:
    library = _registered(_empty(tmp_path), _write(tmp_path / "a", "k.json", _label_key()))
    conflicting = _write(tmp_path / "b", "k.json", _label_key(name="another", text='1/2"'))
    with pytest.raises(LibraryError, match="different answer"):
        _registered(library, conflicting)

    agreeing = _write(tmp_path / "c", "k.json", {**_label_key(name="agreeing"), "version": "2"})
    assert len(_registered(library, agreeing).entries) == 2


def test_an_unsettled_wall_end_is_no_conflict(tmp_path: Path) -> None:
    settled = walls_key_document()
    unsettled = json.loads(json.dumps(settled))
    unsettled["name"] = "the same rows, one end unsettled"
    unsettled["cases"][0]["left"] = {"drawn": "key_unsure", "site": "key_unsure"}
    library = _registered(_empty(tmp_path), _write(tmp_path / "a", "w.json", settled))
    assert len(_registered(library, _write(tmp_path / "b", "w.json", unsettled)).entries) == 2

    different = json.loads(json.dumps(settled))
    different["name"] = "the same rows, one end otherwise"
    left = different["cases"][0]["left"]
    left["drawn"] = "open" if left["drawn"] != "open" else "wall"
    with pytest.raises(LibraryError, match="different answer"):
        _registered(library, _write(tmp_path / "c", "w.json", different))


def test_a_hand_edited_library_with_conflicting_entries_is_refused(tmp_path: Path) -> None:
    library = _registered(_empty(tmp_path), _write(tmp_path / "a", "k.json", _label_key()))
    write_library(library)
    document = json.loads(library.path.read_text())
    document["entries"].append(dict(document["entries"][0], sha256="d" * 64))
    library.path.write_text(json.dumps(document))
    with pytest.raises(LibraryError, match="twice"):
        read_library(library.path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": "pariksha-library/0"}, "format"),
        ({"surprise": 1}, "unknown"),
    ],
)
def test_a_library_that_is_not_one_is_refused(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    path = tmp_path / "library.json"
    path.write_text(json.dumps({"format": LIBRARY_FORMAT, "entries": [], **change}))
    with pytest.raises(LibraryError, match=message):
        read_library(path)


def test_an_entry_whose_kind_does_not_match_its_agent_is_refused(tmp_path: Path) -> None:
    library = _registered(_empty(tmp_path), _write(tmp_path / "a", "k.json", _label_key()))
    write_library(library)
    document = json.loads(library.path.read_text())
    document["entries"][0]["kind"] = "walls"
    library.path.write_text(json.dumps(document))
    with pytest.raises(LibraryError, match="kind"):
        read_library(library.path)


def test_a_missing_library_is_refused_unless_it_is_being_started(tmp_path: Path) -> None:
    with pytest.raises(LibraryError, match="no library"):
        read_library(tmp_path / "missing.json")
    assert read_library(tmp_path / "missing.json", missing_ok=True).entries == ()
