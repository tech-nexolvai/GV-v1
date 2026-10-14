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
    MACHINE_SOURCES,
    REVIEWER,
    REVIEWERS_ENV,
    REVIEWERS_FORMAT,
    UNKNOWN,
    Reviewers,
    ReviewersFileError,
    is_code_actor,
    is_machine_source,
    load_reviewers,
    reviewers_from_environment,
)


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
