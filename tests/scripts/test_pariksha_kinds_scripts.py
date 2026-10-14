"""`scripts/pariksha_kinds.py` and `scripts/pariksha_kind_key.py` (#1250).

Synthetic stored runs on the test database and an invented form key only. No model call, no client
value, no PDF.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eval.pariksha.keys import KeyFormatError
from eval.pariksha.kinds import KINDS_AGENT, load_kinds_key
from tests.eval.test_pariksha_kinds import synthetic_pieces_run

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pariksha_kind_key
import pariksha_kinds

pytest_plugins = ("tests.app.postgres_fixture",)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine(postgres_engine: Engine) -> Engine:
    from alembic import command
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return postgres_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    opened = session_factory(engine)()
    try:
        yield opened
    finally:
        opened.close()


def _key(tmp_path: Path, second: str = "cabinet") -> Path:
    path = tmp_path / f"kind-key-{second}.json"
    path.write_text(
        json.dumps(
            {
                "format": "pariksha-key/1",
                "agent": KINDS_AGENT,
                "name": "synthetic",
                "version": second,
                "cases": [
                    {
                        "family": "piece-kind",
                        "page_sha256": "1" * 64,
                        "question_id": f"p0:piece{slot}",
                        "pieces": 2,
                        "row_widths": ["1", "3"],
                        "answer": {"kinds": [kind]},
                    }
                    for slot, kind in ((0, "filler"), (1, second))
                ],
            }
        )
    )
    return path


def _score(engine: Engine, key: Path, out: Path, source: str, code: str = "a1b2c3d") -> int:
    return pariksha_kinds.main(
        [
            "score",
            "--source",
            source,
            "--db",
            engine.url.render_as_string(hide_password=False),
            "--key",
            str(key),
            "--code-version",
            code,
            "--out",
            str(out),
        ]
    )


def test_score_both_sources_and_compare_them(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    synthetic_pieces_run(session)
    key = _key(tmp_path)
    product, tags = tmp_path / "product.json", tmp_path / "tags.json"

    assert _score(engine, key, product, "product") == 0
    printed = capsys.readouterr().out
    assert "right: 1/2" in printed and "kind source: product" in printed
    assert _score(engine, key, tags, "face-tag") == 0
    capsys.readouterr()

    # The product names piece 0, the face tag piece 1: one fixed, one broke, nothing better.
    assert pariksha_kinds.main(["compare", str(product), str(tags)]) == 1
    assert "REJECT (not better" in capsys.readouterr().out


def test_a_new_wrong_kind_is_rejected(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    synthetic_pieces_run(session)
    key = _key(tmp_path, "panel")  # the tag over piece 1 names a cabinet: wrong for a panel
    product, tags = tmp_path / "product.json", tmp_path / "tags.json"
    assert _score(engine, key, product, "product") == 0
    assert _score(engine, key, tags, "face-tag") == 0
    capsys.readouterr()

    assert pariksha_kinds.main(["compare", str(product), str(tags)]) == 1
    assert "1 new wrong" in capsys.readouterr().out


def test_refusals_exit_2(
    engine: Engine, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    synthetic_pieces_run(session)
    assert _score(engine, _key(tmp_path), REPO / "kinds-card.json", "product") == 2
    assert not (REPO / "kinds-card.json").exists()
    assert _score(engine, _key(tmp_path), tmp_path / "x.json", "product", "main") == 2
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert _score(engine, _key(tmp_path), first, "product") == 0
    assert _score(engine, _key(tmp_path, "panel"), second, "product") == 0
    assert pariksha_kinds.main(["compare", str(first), str(second)]) == 2
    assert "different answer keys" in capsys.readouterr().err


# --- the key builder ----------------------------------------------------------------------------

FORM_KEY = {
    "countertops": [
        {"page": 2, "pieces": [['2"', "filler"], ['20"', "cabinet"], ['1"', "end panel"]]},
        {"page": 3, "present": False},
        {
            "page": 4,
            "pieces": [
                {"text": "5 in", "kind": "dishwasher space", "order": "2"},
                {"text": "1 in", "kind": "countertop_segment", "order": "1"},
            ],
            "superseded": {"entry": {"pieces": [['9"', "cabinet"]]}},
        },
    ]
}
HASHES = {page: str(page) * 64 for page in range(1, 6)}


def test_a_form_key_becomes_a_key_the_scorer_reads() -> None:
    document = pariksha_kind_key.kind_key_document(
        FORM_KEY, HASHES, name="synthetic", version="1", source="invented form key"
    )

    key = load_kinds_key(document)
    # Each case carries its row's widths, so a scorer can check the run read the same row.
    assert [case.row_widths for case in key.cases] == [
        (Fraction(2), Fraction(20), Fraction(1)),
    ] * 3 + [(Fraction(1), Fraction(5))] * 2
    shown = [(c.page_sha256[0], c.question_id, c.pieces, str(c.truth)) for c in key.cases]
    assert shown == [
        ("2", "p1:piece0", 3, "filler"),
        ("2", "p1:piece1", 3, "cabinet"),
        ("2", "p1:piece2", 3, "panel"),
        # In the key's own left-to-right order; the superseded entry is history.
        ("4", "p3:piece0", 2, "not scored (countertop_segment)"),
        ("4", "p3:piece1", 2, "appliance_space"),
    ]


def test_a_page_map_keys_a_pdf_that_repeats_the_drawings() -> None:
    document = pariksha_kind_key.kind_key_document(
        FORM_KEY, HASHES, name="synthetic", version="1", source="invented", page_map={5: 2}
    )

    key = load_kinds_key(document)
    assert {case.question_id for case in key.cases} == {"p4:piece0", "p4:piece1", "p4:piece2"}
    assert {case.page_sha256 for case in key.cases} == {"5" * 64}


def test_a_form_key_with_nothing_to_key_is_refused() -> None:
    with pytest.raises(KeyFormatError, match="no piece"):
        pariksha_kind_key.kind_key_document(
            {"countertops": [{"page": 1, "present": False}]},
            HASHES,
            name="synthetic",
            version="1",
            source="invented",
        )


@pytest.mark.parametrize(
    ("text", "inches"),
    [
        ('2"', Fraction(2)),
        ('11 3/8"', Fraction(91, 8)),
        ("28 in", Fraction(28)),
        ("1 3/4 in", Fraction(7, 4)),
        ("203 mm [8 in]", Fraction(8)),  # the inch half is the authority
        ("64 [2 1/2]", Fraction(5, 2)),
        ('4"+1" Filler', Fraction(5)),  # two fillers drawn as one piece
        ('97"(6EQ)', Fraction(97)),
        ("203 mm", None),  # millimetres alone are never read as inches
        ("unknown", None),
        ("", None),
    ],
)
def test_the_form_keys_width_text_becomes_exact_inches(text: str, inches: Fraction | None) -> None:
    assert pariksha_kind_key.width_inches(text) == inches
