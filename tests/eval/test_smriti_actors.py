"""Smriti's one set of exclusion rules: who decided, and whether it can ever be a key case (#1267).

Verification for: `eval/smriti/actors.py`. Invented names only; the real reviewer list is a local
file under `~/gv-local`, never in the repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.smriti.actors import (
    CODE,
    CODE_ACTOR_PREFIX,
    CONFIRMERS_ENV,
    CONFIRMERS_FORMAT,
    MACHINE_SOURCES,
    REVIEWER,
    REVIEWERS_ENV,
    REVIEWERS_FORMAT,
    UNKNOWN,
    ConfirmersFileError,
    Reviewers,
    ReviewersFileError,
    confirmers_from_environment,
    is_code_actor,
    is_machine_source,
    load_confirmers,
    load_reviewers,
    not_a_person,
    reviewers_from_environment,
)

#: The confirmers file format, spelled out so the parametrized cases are built at import time.
CONFIRMERS_FORMAT_FOR_TEST = "smriti-confirmers/1"


def test_the_code_actor_prefix_is_the_products_own_and_covers_every_code_confirmer() -> None:
    from workflow.parts import BALA_CONFIRMER
    from workflow.parts import CODE_ACTOR_PREFIX as PRODUCT_PREFIX
    from workflow.view_roles import CODE_CONFIRMERS

    assert CODE_ACTOR_PREFIX == PRODUCT_PREFIX
    # A new code confirmer in the product cannot slip in as a person: every one is a code actor.
    for confirmer in (*CODE_CONFIRMERS, BALA_CONFIRMER):
        assert is_code_actor(confirmer), confirmer


@pytest.mark.parametrize("actor", ["code:bala-two-judgments/v1", " code:anything", "CODE:x"])
def test_any_code_actor_is_code(actor: str) -> None:
    assert is_code_actor(actor)
    assert Reviewers(names=frozenset({actor.strip()}), source="test").status(actor) == CODE


@pytest.mark.parametrize("actor", ["reviewer-a", "decoder", "", None])
def test_a_name_is_not_code(actor: str | None) -> None:
    assert not is_code_actor(actor)


def test_every_machine_source_is_listed_and_a_reviewer_is_not() -> None:
    for source in ("code", "code+ais", "both-ais", "none", "automatic", "carried", "deterministic"):
        assert is_machine_source(source)
    assert MACHINE_SOURCES >= {"code", "code+ais", "both-ais", "automatic", "carried"}
    assert not is_machine_source("reviewer")
    assert not is_machine_source("human")


def test_only_a_name_on_the_list_is_a_reviewer() -> None:
    people = Reviewers(names=frozenset({"reviewer-a"}), source="test")
    assert people.status("reviewer-a") == REVIEWER
    assert people.status("  Reviewer-A ") == REVIEWER  # the same name, spaced or cased otherwise
    assert people.status("ui reviewer") == UNKNOWN
    assert people.status("proof-person@local (scratch copy only)") == UNKNOWN
    assert "not on the local list" in (people.why_not("ui reviewer") or "")
    assert people.why_not("reviewer-a") is None


def test_with_no_list_nobody_is_a_known_reviewer() -> None:
    nobody = Reviewers(names=None, source=None)
    assert nobody.status("reviewer-a") == UNKNOWN
    assert REVIEWERS_ENV in (nobody.why_not("reviewer-a") or "")


def test_the_list_is_read_from_a_local_file(tmp_path: Path) -> None:
    path = tmp_path / "people.json"
    path.write_text(
        json.dumps({"format": REVIEWERS_FORMAT, "reviewers": ["reviewer-a", "reviewer-b"]})
    )
    people = load_reviewers(path)
    assert people.names == frozenset({"reviewer-a", "reviewer-b"})
    assert people.status("reviewer-b") == REVIEWER


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps(["reviewer-a"]),
        json.dumps({"format": "other", "reviewers": ["reviewer-a"]}),
        json.dumps({"format": REVIEWERS_FORMAT, "reviewers": "reviewer-a"}),
        json.dumps({"format": REVIEWERS_FORMAT, "reviewers": [""]}),
        json.dumps({"format": REVIEWERS_FORMAT, "reviewers": ["code:bala-two-judgments/v1"]}),
        json.dumps({"format": REVIEWERS_FORMAT, "reviewers": ["reviewer-a"], "extra": 1}),
    ],
)
def test_a_malformed_list_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "people.json"
    path.write_text(content)
    with pytest.raises(ReviewersFileError):
        load_reviewers(path)


def test_a_list_inside_a_git_working_tree_is_refused() -> None:
    # Real names stay local: a list inside any repository is refused before it is read.
    inside = Path(__file__).resolve().parents[2] / "smriti-reviewers-should-never-exist.json"
    with pytest.raises(ReviewersFileError, match="git working tree"):
        load_reviewers(inside)


def test_the_setting_names_the_list_and_has_no_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(REVIEWERS_ENV, raising=False)
    unset = reviewers_from_environment()
    assert unset.names is None and unset.status("reviewer-a") == UNKNOWN

    path = tmp_path / "people.json"
    path.write_text(json.dumps({"format": REVIEWERS_FORMAT, "reviewers": ["reviewer-a"]}))
    monkeypatch.setenv(REVIEWERS_ENV, str(path))
    assert reviewers_from_environment().status("reviewer-a") == REVIEWER

    monkeypatch.setenv(REVIEWERS_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(ReviewersFileError):
        reviewers_from_environment()


# --- review of #1278: a separate confirmers list, and a stricter name check (#1282) --------------


@pytest.mark.parametrize(
    "name",
    [
        "claudebot",
        "ClaudeCode",
        "gptreviewer",
        "opus",
        "MyOpusChecker",
        "AIReviewer",
        "ReviewBot",
        "review-bot",
        "code:anything",
        "\u0441laude",  # a Cyrillic letter that looks like a Latin "c"
        "reviewer-\u0430",  # a Cyrillic letter that looks like a Latin "a"
        "Jos\u00e9",  # not plain ASCII, so a look-alike could pass for another name
        "reviewer\u200b-a",  # an invisible zero-width space
    ],
)
def test_an_ai_name_inside_a_word_or_a_look_alike_letter_is_not_a_person(name: str) -> None:
    assert not_a_person(name) is not None


@pytest.mark.parametrize("name", ["reviewer-a", "Kai Abbott", "Sai Talbot", "Abigail Rain"])
def test_a_plain_name_is_a_person_even_when_a_short_word_sits_inside_it(name: str) -> None:
    assert not_a_person(name) is None


@pytest.mark.parametrize("name", ["claudebot", "ClaudeCode", "gptreviewer", "\u0441laude"])
def test_the_reviewer_list_refuses_ai_names_inside_a_word_and_look_alike_letters(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / "people.json"
    path.write_text(json.dumps({"format": REVIEWERS_FORMAT, "reviewers": ["reviewer-a", name]}))
    with pytest.raises(ReviewersFileError, match="person"):
        load_reviewers(path)


def test_the_confirmers_list_is_its_own_local_file_with_its_own_setting_and_no_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len({CONFIRMERS_ENV, REVIEWERS_ENV}) == 2
    assert len({CONFIRMERS_FORMAT, REVIEWERS_FORMAT}) == 2
    assert CONFIRMERS_FORMAT == CONFIRMERS_FORMAT_FOR_TEST
    monkeypatch.delenv(CONFIRMERS_ENV, raising=False)
    unset = confirmers_from_environment()
    assert unset.names is None and not unset.knows("confirmer-b")

    path = tmp_path / "confirmers.json"
    path.write_text(json.dumps({"format": CONFIRMERS_FORMAT, "confirmers": ["confirmer-b"]}))
    monkeypatch.setenv(CONFIRMERS_ENV, str(path))
    confirmers = confirmers_from_environment()
    assert confirmers.knows(" Confirmer-B ") and not confirmers.knows("reviewer-a")
    # The reviewer list is not the confirmers list: a reviewers file is refused here.
    path.write_text(json.dumps({"format": REVIEWERS_FORMAT, "reviewers": ["confirmer-b"]}))
    with pytest.raises(ConfirmersFileError):
        load_confirmers(path)

    monkeypatch.setenv(CONFIRMERS_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(ConfirmersFileError, match="does not exist"):
        confirmers_from_environment()


def test_a_confirmers_list_inside_a_git_working_tree_is_refused() -> None:
    inside = Path(__file__).resolve().parents[2] / "smriti-confirmers-should-never-exist.json"
    with pytest.raises(ConfirmersFileError, match="git working tree"):
        load_confirmers(inside)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"format": CONFIRMERS_FORMAT_FOR_TEST, "confirmers": "confirmer-b"}),
        json.dumps({"format": CONFIRMERS_FORMAT_FOR_TEST, "confirmers": [""]}),
        json.dumps({"format": CONFIRMERS_FORMAT_FOR_TEST, "confirmers": ["claudebot"]}),
        json.dumps({"format": CONFIRMERS_FORMAT_FOR_TEST, "confirmers": ["\u0441onfirmer"]}),
        json.dumps({"format": CONFIRMERS_FORMAT_FOR_TEST, "confirmers": ["code:x"]}),
        json.dumps({"format": CONFIRMERS_FORMAT_FOR_TEST, "confirmers": ["b"], "extra": 1}),
    ],
)
def test_a_malformed_confirmers_list_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "confirmers.json"
    path.write_text(content)
    with pytest.raises(ConfirmersFileError):
        load_confirmers(path)


# --- review of #1287: swapped digits and separators, more model names, app names ------------------

#: Every AI name the review of #1287 found getting through, and the model names it asked to add.
SWAPPED_OR_SPLIT = (
    "c1aude",
    "cl4ude",
    "0pus",
    "s0nnet",
    "g3mini",
    "c0dex",
    "anthr0pic",
    "copi1ot",
    "c l a u d e",
    "cla-ude",
    "G.P.T",
    "A.I.",
    "A I",
    "cl@ude",
    "50nnet",
    "gr0k",
)
MODEL_NAMES = (
    "llama",
    "mistral",
    "grok",
    "deepseek",
    "qwen",
    "gemma",
    "fable",
    "mythos",
    "argon",
    "haiku",
    "opus",
    "sonnet",
    "gpt",
    "kimi",
    "glm",
    "Kimi Reviewer",
    "the GLM desk",
    "Deep-Seek",
    "MyLlamaChecker",
    "Claudette",
    "Haikuo",
)


@pytest.mark.parametrize("name", [*SWAPPED_OR_SPLIT, *MODEL_NAMES])
def test_a_swapped_split_or_model_name_is_not_a_person(name: str) -> None:
    assert not_a_person(name) is not None


@pytest.mark.parametrize(
    "name", ["reviewer2", "a_b", "a  b", "a--b", "-ab", "ab-", "o'", "a.b", "a@b", "a/b"]
)
def test_only_letters_with_single_spaces_hyphens_or_apostrophes_are_a_name(name: str) -> None:
    assert "plain name" in (not_a_person(name) or "")


@pytest.mark.parametrize(
    "name", ["Allman", "Hillman", "Spillman", "Mary O'Brien", "Anne-Marie Allman", "Kai", "Abbott"]
)
def test_a_short_token_inside_a_real_name_is_not_refused(name: str) -> None:
    assert not_a_person(name) is None


@pytest.mark.parametrize("name", [*SWAPPED_OR_SPLIT, "deepseek", "kimi", "glm"])
@pytest.mark.parametrize("which", ["reviewers", "confirmers"])
def test_both_lists_refuse_swapped_split_and_model_names(
    tmp_path: Path, name: str, which: str
) -> None:
    path = tmp_path / f"{which}.json"
    fmt = REVIEWERS_FORMAT if which == "reviewers" else CONFIRMERS_FORMAT
    path.write_text(json.dumps({"format": fmt, which: ["reviewer-a", name]}))
    load = load_reviewers if which == "reviewers" else load_confirmers
    with pytest.raises(ReviewersFileError, match="person"):
        load(path)


def test_a_confirmer_may_list_the_names_they_use_in_the_app(tmp_path: Path) -> None:
    path = tmp_path / "confirmers.json"
    path.write_text(
        json.dumps(
            {
                "format": CONFIRMERS_FORMAT,
                "confirmers": [
                    {"name": "confirmer-b", "app_names": ["cb@example.test", "user 7"]},
                    "confirmer-c",
                ],
            }
        )
    )
    confirmers = load_confirmers(path)
    assert confirmers.knows("confirmer-b") and confirmers.knows("confirmer-c")
    # An app name is not a confirmer name: it only ever refuses.
    assert not confirmers.knows("cb@example.test")
    for decided_by in ("confirmer-b", " Confirmer-B", "CB@example.test", "user7", "USER  7"):
        assert confirmers.decided("Confirmer-B", decided_by), decided_by
    assert not confirmers.decided("confirmer-b", "reviewer-a")
    assert not confirmers.decided("confirmer-c", "cb@example.test")
    assert confirmers.decided("confirmer-c", "confirmer - c".replace(" - ", "-"))


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "confirmer-b", "app_names": "cb"},
        {"name": "confirmer-b", "app_names": [""]},
        {"name": "confirmer-b", "app_names": ["cb"], "extra": 1},
        {"app_names": ["cb"]},
        {"name": "c1aude", "app_names": ["cb"]},
    ],
)
def test_a_malformed_confirmer_entry_is_refused(tmp_path: Path, entry: dict[str, object]) -> None:
    path = tmp_path / "confirmers.json"
    path.write_text(json.dumps({"format": CONFIRMERS_FORMAT, "confirmers": [entry]}))
    with pytest.raises(ConfirmersFileError):
        load_confirmers(path)


def test_the_reviewers_list_takes_plain_names_only(tmp_path: Path) -> None:
    path = tmp_path / "people.json"
    path.write_text(json.dumps({"format": REVIEWERS_FORMAT, "reviewers": [{"name": "reviewer-a"}]}))
    with pytest.raises(ReviewersFileError, match="non-empty names"):
        load_reviewers(path)
