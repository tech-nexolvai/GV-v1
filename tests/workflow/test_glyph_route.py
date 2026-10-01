"""The shape reader as a pipeline route (#756 phase E).

Verification for: `workflow/glyph_route.py`, and its wiring in `workflow/stages.py`.

The one that matters most is that a glyph reading **never seals**: until the admin decides what it
may seal (#756 D2), it is a candidate a reviewer confirms, and it never reaches cross-route
corroboration or the bounded agent — checked by watching what those two steps are handed, not by
trusting that the rows happen to sit in different regions.

The drawing is a real one-page PDF with a stamp, drawn in the made-up font of
`tests/extraction/test_glyph_reader.py`. No client drawing is read and no model is called.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import ObservationCandidate
from app.models.runs import ExtractionRun
from extraction.annotations import read_annotation_layers
from extraction.glyph_reader import ReaderSettings, TemplateSet, described
from storage.local import LocalStore
from tests.extraction.test_annotations import _appearance, _pdf, _stamp
from tests.extraction.test_glyph_reader import SHAPE, _row
from tests.workflow.test_association import LOCALIZED, SETTINGS, _revision, _upgrade
from tests.workflow.test_markup_route import _SilentOcr
from workflow.glyph_route import (
    GLYPH_EXTRACTOR,
    GLYPH_TEMPLATES_ENV,
    GlyphRoute,
    glyph_route_from_environment,
)
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

ASSOCIATION = replace(SETTINGS, proximity_limit=Decimal("0.9"))


def _content(paths: list, *, width: str = "0.2") -> bytes:  # type: ignore[type-arg]
    """Paths as an appearance stream: each sub-path a move and its lines, stroked."""
    out = [f"{width} w".encode()]
    for path in paths:
        parts: list[str] = []
        for segment in path.segments:
            x, y = segment.point
            parts.append(f"{x} {y} {'m' if segment.kind.value == 'move' else 'l'}")
        out.append((" ".join(parts) + " S").encode())
    return b"\n".join(out) + b"\n"


#: A dimension line with a `12"` above it, in appearance space (page = appearance − (50, 450)).
LABEL, NAMES = _row('12"', 110, 520, scale=0.5)
SHEET = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[_appearance(b"1 w 105 515 m 205 515 l S\n" + _content(LABEL))],
)


def _templates() -> TemplateSet:
    """Label the sheet's own four characters — the one-page stand-in for a person's labelling."""
    layers = read_annotation_layers(
        SHEET,
        0,
        document_version_id=UUID(int=0),
        dpi=150,
        line_minimum_pt=ASSOCIATION.line_minimum_pt,
        glyph_maximum_pt=ASSOCIATION.glyph_maximum_pt,
        glyph_gap_pt=ASSOCIATION.glyph_gap_pt,
    )
    paths = list(layers.glyph_paths)
    assert len(paths) == len(NAMES)
    settings = _settings()
    _, _, shapes = described(paths, settings=settings)
    return TemplateSet(
        set_hash="e" * 64,
        shape_settings=SHAPE.config_hash,
        glyph_gap_pt=ASSOCIATION.glyph_gap_pt,
        labels=tuple(NAMES),
        shapes=tuple(shape for shape in shapes if shape is not None),
    )


def _settings() -> ReaderSettings:
    return ReaderSettings(
        shape=SHAPE,
        maximum_distance=Decimal("0.5"),
        minimum_margin=Decimal("0.1"),
        maximum_size_ratio=Decimal("1.3"),
        label_gap_pt=Decimal(2),
        maximum_label_pt=Decimal(40),
        glyph_gap_pt=ASSOCIATION.glyph_gap_pt,
    )


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
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _stages(
    store: LocalStore, route: GlyphRoute | None, vision_readers: tuple[object, ...] = ()
) -> DatabaseStages:
    return DatabaseStages(
        store,
        dpi=150,
        association=ASSOCIATION,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        localized_ocr=LOCALIZED,
        vision_readers=vision_readers,  # type: ignore[arg-type]
        glyph_route=route,
    )


def _glyph_rows(session: Session) -> list[ObservationCandidate]:
    runs = {
        run.id
        for run in session.execute(select(ExtractionRun)).scalars()
        if run.extractor == GLYPH_EXTRACTOR
    }
    return [
        row
        for row in session.execute(select(ObservationCandidate)).scalars()
        if row.extraction_run_id in runs
    ]


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def test_a_label_is_read_from_its_shapes_and_recorded_under_its_own_reader(
    session: Session, store: LocalStore
) -> None:
    """Outcome: one candidate, `12"`, 12 inches exactly, under `extraction.glyph_reader`."""
    revision = _revision(session, store, data=SHEET)
    session.commit()

    results = _stages(store, GlyphRoute(_templates(), _settings())).extract_pages(
        session, revision.id
    )
    session.commit()

    (row,) = _glyph_rows(session)
    assert row.raw_text == '12"'
    assert (row.value_numerator, row.value_denominator, row.unit) == (12, 1, "in")
    (payload,) = [result.payload for result in results]
    assert payload["glyph_readings"] == 1
    assert payload["glyph_abstentions"] == 0


def _watch_agent(monkeypatch: pytest.MonkeyPatch) -> list[ObservationCandidate]:
    """Record every candidate handed to the bounded agent."""
    agent: list[ObservationCandidate] = []
    original_agent = DatabaseStages._run_bounded_agent_for_ambiguous_regions

    def watching_agent(self: DatabaseStages, session: Session, **kwargs: object) -> object:
        agent.extend(kwargs["candidates"])  # type: ignore[call-overload]
        return original_agent(self, session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(DatabaseStages, "_run_bounded_agent_for_ambiguous_regions", watching_agent)
    return agent


def _read_with_vision(
    session: Session, store: LocalStore, readers: tuple[object, ...]
) -> ObservationCandidate:
    revision = _revision(session, store, data=SHEET)
    session.commit()
    _stages(store, GlyphRoute(_templates(), _settings()), readers).extract_pages(
        session, revision.id
    )
    session.commit()
    (row,) = _glyph_rows(session)
    return row


def test_a_glyph_reading_alone_is_not_confirmed(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**#756 D2, decided 2026-10-01: never confirmed alone.** Outcome: with no other reader, the
    reading is a value for a person to confirm — no agreement lane — and the bounded agent is never
    handed it."""
    agent = _watch_agent(monkeypatch)

    row = _read_with_vision(session, store, ())

    assert row.corroboration_lane is None
    assert row.corroboration_status != "CORROBORATED"
    assert all(candidate.id != row.id for candidate in agent)


def test_a_glyph_reading_is_confirmed_by_another_reader_agreeing_on_its_box(
    session: Session, store: LocalStore
) -> None:
    """**The second witness (#756 D2).** The page asks the vision readers about the glyph
    reading's own box; one that did not use the templates agreeing on `12"` is what confirms it."""
    from evidence.canonical import CorroborationLane
    from tests.evidence.test_bridge import _AgreeingVisionReader

    row = _read_with_vision(session, store, (_AgreeingVisionReader('12"'),))

    assert row.corroboration_lane == CorroborationLane.SECOND_READER.value
    assert row.corroboration_status != "CONFLICTING"


def test_a_glyph_reading_another_reader_disagrees_with_goes_to_a_person(
    session: Session, store: LocalStore
) -> None:
    from evidence.canonical import EvidenceStatus
    from tests.evidence.test_bridge import _AgreeingVisionReader

    row = _read_with_vision(session, store, (_AgreeingVisionReader('13"'),))

    assert row.corroboration_status == EvidenceStatus.CONFLICTING.value


def test_a_glyph_reading_attaches_to_the_line_it_labels(
    session: Session, store: LocalStore
) -> None:
    """Outcome: the reading reaches `associate` — with the geometry the reader established — so
    the auto-filled form can use it (#712, #759)."""
    from app.models import ObservationAssociation

    revision = _revision(session, store, data=SHEET)
    session.commit()

    _stages(store, GlyphRoute(_templates(), _settings())).extract_pages(session, revision.id)
    session.commit()

    (row,) = _glyph_rows(session)
    associations = [
        association
        for association in session.execute(select(ObservationAssociation)).scalars()
        if association.candidate_id == row.id
    ]
    assert len(associations) == 1


def test_the_route_is_off_by_default(session: Session, store: LocalStore) -> None:
    """Outcome: no route, no glyph run, and the page says the step did not run — not that it read
    nothing."""
    revision = _revision(session, store, data=SHEET)
    session.commit()

    results = _stages(store, None).extract_pages(session, revision.id)
    session.commit()

    assert _glyph_rows(session) == []
    (payload,) = [result.payload for result in results]
    assert payload["glyph_readings"] is None


def test_a_rerun_reads_nothing_twice(session: Session, store: LocalStore) -> None:
    revision = _revision(session, store, data=SHEET)
    session.commit()
    route = GlyphRoute(_templates(), _settings())

    _stages(store, route).extract_pages(session, revision.id)
    session.commit()
    _stages(store, route).extract_pages(session, revision.id)
    session.commit()

    assert len(_glyph_rows(session)) == 1


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _write_set(root: Path, templates: TemplateSet) -> Path:
    digest = "f" * 64
    folder = root / digest[:12]
    folder.mkdir()
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "shape_settings": templates.shape_settings,
                "reader_settings": {"GV_READER_GLYPH_GAP_PT": str(templates.glyph_gap_pt)},
            }
        ),
        encoding="utf-8",
    )
    np.savez_compressed(
        folder / "templates.npz",
        rasters=np.stack([shape.raster for shape in templates.shapes]),
        labels=np.array(templates.labels),
        relative_heights=np.array([str(shape.relative_height) for shape in templates.shapes]),
        relative_widths=np.array([str(shape.relative_width) for shape in templates.shapes]),
        dots=np.array([shape.dot for shape in templates.shapes]),
    )
    return folder


ENVIRONMENT = {
    "GV_GLYPH_MAX_DISTANCE": "0.5",
    "GV_GLYPH_MARGIN": "0.1",
    "GV_GLYPH_SIZE_RATIO": "1.3",
    "GV_GLYPH_LABEL_GAP_PT": "2",
    "GV_GLYPH_MAX_LABEL_PT": "40",
}


def test_unset_means_no_route() -> None:
    assert glyph_route_from_environment({}) is None
    assert glyph_route_from_environment({GLYPH_TEMPLATES_ENV: "  "}) is None


def test_a_route_without_its_settings_is_refused(tmp_path: Path) -> None:
    """Outcome: every missing setting named; none is defaulted."""
    folder = _write_set(tmp_path, _templates())

    with pytest.raises(ValueError, match="GV_GLYPH_MARGIN"):
        glyph_route_from_environment(
            {GLYPH_TEMPLATES_ENV: str(folder), "GV_GLYPH_MAX_DISTANCE": "0.5"}
        )


def test_a_configured_route_reads_with_the_sets_own_sizing(tmp_path: Path) -> None:
    folder = _write_set(tmp_path, _templates())

    route = glyph_route_from_environment({GLYPH_TEMPLATES_ENV: str(folder), **ENVIRONMENT})

    assert route is not None
    assert route.settings.shape == SHAPE
    assert route.settings.glyph_gap_pt == ASSOCIATION.glyph_gap_pt
    assert route.settings.minimum_margin == Decimal("0.1")
    assert route.templates.set_hash in route.config_hash


@pytest.mark.parametrize(
    ("written", "flags", "lane", "status"),
    [
        ("381 [15]", [], "DUAL_UNIT", "RAW_CANDIDATE"),
        ("381 [16]", [], "DUAL_UNIT", "CONFLICTING"),
        ('15"', [], None, None),
        ("381 [15]", ["stacked_fraction"], None, None),
    ],
)
def test_a_glyph_label_stating_millimetres_and_inches_is_its_own_witness(
    written: str, flags: list[str], lane: str | None, status: str | None
) -> None:
    """**#756 D2's other witness.** A label whose millimetres agree with its inches is checked by
    the dual lane like any reading; halves that disagree are a conflict for a person; a label with no
    millimetres has no lane; and nothing marked stacked is ever agreed (#726)."""
    from app.evidence.record import dual_unit_lane
    from workflow.glyph_route import GLYPH_EXTRACTOR

    row = ObservationCandidate(
        raw_text=written,
        unit_guess="in",
        polygon=[[0, 0], [10, 0], [10, 5], [0, 5]],
        confidence=None,
        ambiguity_flags=flags,
    )
    run = ExtractionRun(extractor=GLYPH_EXTRACTOR, extractor_version="a6e5e73d269f")

    assert dual_unit_lane(row, run=run, page_index=0) == (status, lane)
