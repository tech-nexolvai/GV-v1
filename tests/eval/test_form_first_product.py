"""Synthetic audit fixtures: no private reader answer or drawing value is checked in."""

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import eval.form_first_product as product
from eval.form_first_product import _complete_slot_row, _key_inputs, audit_saved_attempts
from eval.form_first_safety import SafetyCase, WidthInputs

MODELS = ("maker.first", "maker.second")


def _attempt(
    model_id: str,
    *,
    prompt_id: str = "slot-crop-v1",
    raw: str | None = '{"text":"8"}',
    outcome: str = "ok",
    page: int | None = 1,
    number: int | None = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        model_id=model_id,
        prompt_id=prompt_id,
        private_raw_response=raw,
        outcome=outcome,
        reader_page_index=page,
        reader_attempt_number=number,
    )


def _candidate() -> SimpleNamespace:
    return SimpleNamespace(ambiguity_flags=["reader:maker.first:8", "reader:maker.second:8"])


def _wall_candidate() -> tuple[int, SimpleNamespace]:
    return (
        1,
        SimpleNamespace(
            ambiguity_flags=[
                "wall-reader:maker.first:left=yes,right=yes,behind=yes,view=plan",
                "wall-reader:maker.second:left=yes,right=yes,behind=yes,view=plan",
            ]
        ),
    )


def test_both_saved_label_answers_complete_the_audit() -> None:
    result = audit_saved_attempts(
        (_attempt(MODELS[0]), _attempt(MODELS[1])),
        (_candidate(),),
        page_index=1,
        model_ids=MODELS,
        wall_layout_used=False,
    )

    assert result.complete
    assert result.label_attempts == 2


def test_missing_or_unparseable_raw_answer_is_unaccounted() -> None:
    result = audit_saved_attempts(
        (_attempt(MODELS[0]), _attempt(MODELS[1], raw=None)),
        (_candidate(),),
        page_index=1,
        model_ids=MODELS,
        wall_layout_used=False,
    )

    assert not result.complete
    assert result.reason is not None


def test_repeated_equal_labels_require_a_separate_stored_answer_per_crop() -> None:
    result = audit_saved_attempts(
        (_attempt(MODELS[0]), _attempt(MODELS[1])),
        (_candidate(), _candidate()),
        page_index=1,
        model_ids=MODELS,
        wall_layout_used=False,
    )

    assert not result.complete
    assert result.reason == "a sealed label is not backed by a stored reader answer"


def test_wall_layout_needs_both_readers_private_answers() -> None:
    wall = '{"left":"yes","right":"yes","behind":"yes","view":"plan"}'
    result = audit_saved_attempts(
        (
            _attempt(MODELS[0]),
            _attempt(MODELS[1]),
            _attempt(MODELS[0], prompt_id="slot-walls-v1", raw=wall),
        ),
        (_candidate(),),
        page_index=1,
        model_ids=MODELS,
        wall_layout_used=True,
        wall_candidates=(_wall_candidate(),),
    )

    assert not result.complete
    assert result.reason == "a sealed wall is not backed by stored raw answers"


def test_rejected_malformed_retry_is_accounted_but_not_used_as_a_value() -> None:
    result = audit_saved_attempts(
        (
            _attempt(MODELS[0], raw="bad", outcome="rejected"),
            _attempt(MODELS[0], number=2),
            _attempt(MODELS[1]),
        ),
        (_candidate(),),
        page_index=1,
        model_ids=MODELS,
        wall_layout_used=False,
    )

    assert result.complete
    assert result.attempts == 3


def test_private_key_shapes_use_printed_text_and_keep_product_settings_separate() -> None:
    older = _key_inputs(
        {"overall": '8"', "pieces": [['6"', "cabinet"], ['2"', "filler"]]},
        wall_layout="back_only",
        field_cut=Fraction(1),
    )
    newer = _key_inputs(
        {"overall": {"text": '8"'}, "pieces": [{"text": '6"'}, {"text": '2"'}]},
        wall_layout="back_only",
        field_cut=Fraction(1),
    )

    assert older == newer
    assert older.pieces == (Fraction(6), Fraction(2))


def test_unreadable_key_piece_is_not_guessed_from_product_proposal() -> None:
    truth = _key_inputs(
        {"overall": {"text": '8"'}, "pieces": [{"text": '6"'}, {"text": None}]},
        wall_layout="back_only",
        field_cut=Fraction(1),
    )

    assert truth.pieces is None
    assert not truth.complete


def test_confirmed_dual_unit_key_uses_its_explicit_inches_but_vif_stays_unknown() -> None:
    assert product._key_value({"text": "254 mm [10 in]"}) == Fraction(10)
    assert product._key_value({"text": "254 mm [10 in] VIF"}) is None


def test_shortened_offered_chain_is_not_counted_as_a_complete_product_row() -> None:
    candidates = (
        SimpleNamespace(ambiguity_flags=["slot-reader", "slot:overall"]),
        SimpleNamespace(ambiguity_flags=["slot-reader", "slot:0"]),
        SimpleNamespace(ambiguity_flags=["slot-reader", "slot:1"]),
    )

    assert _complete_slot_row(candidates, (0, 1))
    assert not _complete_slot_row(candidates, (0,))


def test_key_replay_never_borrows_product_wall_answer_as_human_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "form_key.json"
    key.write_text(
        json.dumps({"countertops": [{"page": 1, "overall": '8"', "pieces": [["cabinet", '8"']]}]}),
        encoding="utf-8",
    )
    proposal = WidthInputs(
        overall=Fraction(8),
        cabinets=None,
        fillers=None,
        wall_layout="back_only",
        field_cut=Fraction(1),
        pieces=(Fraction(8),),
    )

    def saved_case(*_args: object, **kwargs: object) -> tuple[SafetyCase, product.AttemptAudit]:
        return (
            SafetyCase(str(kwargs["case_id"]), None, proposal, raw_attempts_complete=True),
            product.AttemptAudit(2, 1, 1, True, None),
        )

    monkeypatch.setattr(product, "product_case_for_page", saved_case)
    report, _audits = product.evaluate_keyed_product_run(
        SimpleNamespace(),  # type: ignore[arg-type]
        package_revision_id=uuid4(),
        key_path=key,
        truth_wall_layouts={},
    )

    assert report.unaccounted == 1
    assert report.false_passes == 0
    assert not report.zero_false_pass


def test_product_projection_reads_exact_saved_row_and_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    revision_id = uuid4()
    proposal_id = uuid4()
    package = SimpleNamespace(product_type="countertop", project_id=uuid4())
    revision = SimpleNamespace(package_id=uuid4())
    run = SimpleNamespace(id=run_id)
    overall = SimpleNamespace(
        ambiguity_flags=[
            "slot-reader",
            "slot:overall",
            "reader:maker.first:8",
            "reader:maker.second:8",
        ],
        corroboration_status="CORROBORATED",
        value_numerator=8,
        value_denominator=1,
        unit="in",
    )
    piece = SimpleNamespace(
        ambiguity_flags=["slot-reader", "slot:0", "reader:maker.first:8", "reader:maker.second:8"],
        corroboration_status="CORROBORATED",
        value_numerator=8,
        value_denominator=1,
        unit="in",
    )
    wall = SimpleNamespace(
        ambiguity_flags=[
            "wall-reader:maker.first:left=no,right=no,behind=yes,view=plan",
            "wall-reader:maker.second:left=no,right=no,behind=yes,view=plan",
        ]
    )
    proposals = [
        (
            SimpleNamespace(
                proposal_id=proposal_id,
                field_key=product.OVERALL_FIELD,
                position=0,
                model_id="maker.first + maker.second",
            ),
            overall,
            run,
        ),
        (
            SimpleNamespace(
                proposal_id=proposal_id,
                field_key=product.PIECE_FIELD,
                position=0,
                model_id="maker.first + maker.second",
            ),
            piece,
            run,
        ),
    ]
    wall_raw = '{"left":"no","right":"no","behind":"yes","view":"plan"}'
    attempts = (
        _attempt(MODELS[0], page=0),
        _attempt(MODELS[1], page=0),
        _attempt(MODELS[0], page=0),
        _attempt(MODELS[1], page=0),
        _attempt(MODELS[0], prompt_id="slot-walls-v1", raw=wall_raw, page=0),
        _attempt(MODELS[1], prompt_id="slot-walls-v1", raw=wall_raw, page=0),
    )

    class FakeSession:
        execute_count = 0
        scalars_count = 0

        def get(self, model: object, _key: object) -> SimpleNamespace:
            return revision if model is product.PackageRevision else package

        def execute(self, _query: object) -> list[object]:
            self.execute_count += 1
            return (
                SimpleNamespace(all=lambda: proposals) if self.execute_count == 1 else [(0, wall)]
            )

        def scalars(self, _query: object) -> tuple[object, ...]:
            self.scalars_count += 1
            return attempts if self.scalars_count == 1 else (overall, piece)

    monkeypatch.setattr(
        product,
        "reader_sealed_wall_config",
        lambda *_args: SimpleNamespace(value="back_only", extraction_run_id=run_id),
    )
    monkeypatch.setattr(product, "_field_cut", lambda *_args: Fraction(1))

    case, audit = product.product_case_for_page(
        FakeSession(),  # type: ignore[arg-type]
        package_revision_id=revision_id,
        page_number=1,
        truth=None,
        case_id="one",
    )

    assert audit.complete
    assert audit.label_attempts == 4
    assert audit.wall_attempts == 2
    assert case.proposed is not None
    assert case.proposed.pieces == (Fraction(8),)
    assert case.proposed.wall_layout == "back_only"
    assert case.proposed.field_cut == Fraction(1)
