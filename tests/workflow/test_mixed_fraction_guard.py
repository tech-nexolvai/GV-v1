"""The agreement gate's whole-number-and-fraction guard (#924).

**The admin's standing rule** (2026-10-03, #728): where two readers ever agree on a wrong number of
some kind, that whole kind goes to a person. Two readers of different vendors have twice agreed on a
wrong whole number and a fraction — a stacked `3/4"` read as `3 3/4"` (#726), and a two-line label
of millimetres over bracketed inches read as one mixed number, as `13 [1/2]` read as `13 1/2"` would be
(#924) — so an agreement on one confirms nothing: it stays a pre-fill a person ticks, counted on the
page result with its reason, as #901's and #919's guards count theirs.

The drawings are made up here. No model is called and no client drawing is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from fractions import Fraction

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import ExtractionRun, ObservationCandidate
from storage.local import LocalStore
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_cross_route_corroboration import (
    _drawing,
    _reader,
    _revision,
    _upgrade,
)
from tests.workflow.test_cut_label_guard import stated_geometry
from units.measurement import Measurement, Unit
from workflow.stages import (
    MIXED_FRACTION_REASON,
    DatabaseStages,
    _MixedFractionGuard,
    mixed_fraction_refusal,
)

pytest_plugins = ("tests.app.postgres_fixture",)


class _LetsEveryValueThrough(_MixedFractionGuard):
    def _reason(self, region: ObservationCandidate) -> str | None:
        del region
        return None


def fractions_let_through() -> _MixedFractionGuard:
    """The guard with its test switched off, for the tests of other rules that call
    `_apply_cross_route_corroboration` directly and agree on a whole number and a fraction."""
    return _LetsEveryValueThrough()


def _inches(value: Fraction | int, text: str) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, text)


# ---------------------------------------------------------------------------
# What the guard covers: the value, however it was written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('13 1/2"', Fraction(27, 2)),  # the #924 shape: `13 [1/2]` read as one number
        ('3 3/4"', Fraction(15, 4)),  # the #726 case: a stacked `3/4"` with a `3` before it
        ('1 1/2"', Fraction(3, 2)),  # a real label it also stops: the cost of the kind
        ('13-1/2"', Fraction(27, 2)),
        ('13½"', Fraction(27, 2)),
        ('13.5"', Fraction(27, 2)),
        ("1'-1 1/2\"", Fraction(27, 2)),
    ],
)
def test_a_whole_number_and_a_fraction_is_held_back_however_written(
    text: str, value: Fraction
) -> None:
    assert mixed_fraction_refusal(_inches(value, text), text) == MIXED_FRACTION_REASON


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('3/4"', Fraction(3, 4)),  # below one: no whole number to promote a fraction into
        ('36"', Fraction(36)),
        ("2'-6\"", Fraction(30)),
        ("13 [1/2]", Fraction(1, 2)),  # the #924 shape read right: its inches are 1/2
        ("914 mm", Fraction(914 * 5, 127)),  # millimetres: a conversion, never written so
    ],
)
def test_other_values_are_not_this_kind(text: str, value: Fraction) -> None:
    assert mixed_fraction_refusal(_inches(value, text), text) is None


def test_a_dual_label_agreed_on_both_halves_is_let_through() -> None:
    """**The millimetres cross-checked it.** `597 [23 1/2]`: 23 1/2" is 596.9 mm, inside the
    rounding of 597 — the evidence a plain `23 1/2"` reading has none of."""
    assert mixed_fraction_refusal(_inches(Fraction(47, 2), "597 [23 1/2]"), "597 [23 1/2]") is None


@pytest.mark.parametrize("text", ["570 [23 1/2]", "570 mm [23 1/2]", '570mm [23 1/2"]'])
def test_a_dual_label_whose_millimetres_do_not_agree_is_held_back(text: str) -> None:
    """`570 [23 1/2]`: 570 mm is not 23 1/2" by any rounding, so nothing cross-checks the fraction —
    however the millimetres are written: a dual label is never taken for millimetres alone."""
    assert mixed_fraction_refusal(_inches(Fraction(47, 2), text), text) == MIXED_FRACTION_REASON


def test_no_value_is_not_held_back() -> None:
    assert mixed_fraction_refusal(None, "13") is None


# ---------------------------------------------------------------------------
# In the stage
# ---------------------------------------------------------------------------


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _extract(session: Session, store: LocalStore, label: str, reading: str) -> dict[str, object]:
    """The file's own text says `label`; a vision reader of another vendor reads `reading`."""
    revision = _revision(
        session, store, data=_drawing(f"BT /F1 10 Tf 1 0 0 1 20 70 Tm ({label}) Tj ET\n".encode())
    )
    (result,) = DatabaseStages(
        store,
        vision_readers=(_reader("bedrock-nova-pro", "amazon.nova-pro-v1:0", reading=reading),),
        missing_space=MISSING_SPACE,
        **stated_geometry("bedrock-nova-pro"),  # type: ignore[arg-type]
    ).extract_pages(session, revision.id)
    return dict(result.payload)


def _lanes(session: Session) -> set[tuple[str, str, str | None, str | None]]:
    return {
        (run.extractor, row.raw_text, row.corroboration_status, row.corroboration_lane)
        for row, run in session.execute(
            select(ObservationCandidate, ExtractionRun).join(
                ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id
            )
        )
    }


def test_two_routes_agreeing_on_a_whole_number_and_a_fraction_do_not_confirm_it(
    session: Session, store: LocalStore
) -> None:
    """**The guard in production.** The file's own `13.5"` and a vision reader's `13.5"` agree;
    neither takes the second-reader lane, and the page result counts the refusal and says why."""
    payload = _extract(session, store, '13.5"', '13.5"')

    assert _lanes(session) == {
        ("pdfplumber", '13.5"', None, None),
        ("bedrock-nova-pro", '13.5"', None, None),
    }
    assert payload["agreement_refusals"] == 1
    assert payload["agreement_refusal_reasons"] == [f"1 × {MIXED_FRACTION_REASON}"]


def test_the_same_agreement_on_a_whole_number_is_confirmed(
    session: Session, store: LocalStore
) -> None:
    """The control: it is the value's kind that holds the agreement back, not the routes."""
    payload = _extract(session, store, '13"', '13"')

    assert _lanes(session) == {
        ("pdfplumber", '13"', "RAW_CANDIDATE", "SECOND_READER"),
        ("bedrock-nova-pro", '13"', "RAW_CANDIDATE", "SECOND_READER"),
    }
    assert payload["agreement_refusals"] == 0


def test_a_disagreement_on_a_whole_number_and_a_fraction_is_still_a_conflict(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Only stricter.** The guard is asked only about an agreement, so a conflict stays one."""

    def never(self: _MixedFractionGuard, region: ObservationCandidate) -> str | None:
        raise AssertionError("the guard is asked only about an agreement")

    monkeypatch.setattr(_MixedFractionGuard, "_reason", never)

    payload = _extract(session, store, '13.5"', '13.25"')

    assert {(status, lane) for *_, status, lane in _lanes(session)} == {
        ("CONFLICTING", "SECOND_READER")
    }
    assert payload["agreement_refusals"] == 0


def test_the_guard_is_asked_about_a_reading_that_agreed(
    session: Session, store: LocalStore
) -> None:
    """A reading with no value abstains (#924), and may come first in its region: the guard reads
    the value of one that agreed, so `13 1/2"` agreed beside a blank `13` is still held back."""
    from app.evidence.record import open_extraction_run
    from tests.workflow.test_cross_route_corroboration import (
        NOVA_FORCED,
        NOVA_TAUGHT,
        QWEN,
        _HoldsNothingBack,
    )

    revision = _revision(session, store)
    DatabaseStages(store, missing_space=MISSING_SPACE).extract_pages(session, revision.id)
    template = session.execute(select(ObservationCandidate)).scalars().one()
    first = session.get(ExtractionRun, template.extraction_run_id)
    assert first is not None
    region = [[10, 10], [20, 10], [20, 20], [10, 20]]
    rows = []
    for (extractor, model_id), text, inches in (
        (NOVA_FORCED, "13", None),
        (QWEN, '13 1/2"', Fraction(27, 2)),
        (NOVA_TAUGHT, '13 1/2"', Fraction(27, 2)),
    ):
        run = open_extraction_run(
            session,
            task_run_id=first.task_run_id,
            extractor=extractor,
            extractor_version=model_id,
            config_hash="test",
            dpi=first.dpi,
        )
        rows.append(
            ObservationCandidate(
                document_version_id=template.document_version_id,
                page_id=template.page_id,
                extraction_run_id=run.id,
                raw_text=text,
                value_numerator=None if inches is None else inches.numerator,
                value_denominator=None if inches is None else inches.denominator,
                unit=None if inches is None else "in",
                unit_guess=None if inches is None else "in",
                semantic_guess=None,
                polygon=region,
                coordinate_space="image",
                confidence=None,
                ambiguity_flags=[] if inches is not None else ["unparsed"],
            )
        )
    session.add_all(rows)
    guard = _MixedFractionGuard()

    DatabaseStages._apply_cross_route_corroboration(
        session,
        page_index=0,
        candidates=rows,
        gv_mark=_HoldsNothingBack(),  # type: ignore[arg-type]
        cut_label=_HoldsNothingBack(),  # type: ignore[arg-type]
        mixed_fraction=guard,
    )

    assert [(row.corroboration_status, row.corroboration_lane) for row in rows] == [
        (None, None)
    ] * 3
    assert list(guard.refused.values()) == [MIXED_FRACTION_REASON]
