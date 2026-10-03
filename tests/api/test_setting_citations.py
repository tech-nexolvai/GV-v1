"""A setting found in the architect's drawing is confirmed by typing it without seeing it (#866).

Verification for: `ParameterEntry.citation` and `SettingPointerOut` in `app/schemas/measurements.py`;
`enter_measurements`, `read_required_inputs` and `setting_passage_crop` in `app/api/measurements.py`;
`confirm_typed_value` and `live_parameter_proposals` in `workflow/parameter_citations.py`; and
migration `0059_parameter_value_citations`.

The tests follow the issue's "Done when": a hand-crafted citation of a vendor passage is refused, a
mismatch is refused with nothing stored, a match is stored with its citation, the free-text path is
unchanged, and the override report shows the citation. The pointer is asserted to carry no number on
the schema and on a live response. The last test is the end-to-end false PASS the issue names.

Every sheet is a synthetic one, read by the real extraction stage, as in
`tests/workflow/test_parameter_proposals.py`: the left half of the page is the architect's drawing
and the right half the vendor's.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    CheckRun,
    DocumentVersion,
    EvidenceArtifact,
    Finding,
    Package,
    Page,
    ParameterProposal,
    ParameterValueCitation,
    ParameterValueCitationRun,
    TextPhrase,
    ViewRole,
)
from app.models.parameters import ParameterValue as StoredValueRow
from app.models.parameters import load_parameter_sets
from app.models.rules import RuleDefinition, RuleSnapshot
from app.schemas.measurements import SettingPointerOut
from app.verdicts.rulebook import from_row
from retrieval.package_text import PhraseGrouping, build_package_phrases, search_package_text
from rules.overrides import as_text, override_report
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity, Rule
from rules.snapshot import publish
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_parameter_proposals import LEFT, RIGHT, _drawings, _read, _run, _sheet
from units.measurement import Measurement, Unit
from workflow.parameter_citations import (
    CitationRefusal,
    CitationRefusalReason,
    confirm_typed_value,
)
from workflow.parameter_proposals import PROPOSER_VERSION, ProposalOutcome, propose_setting
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

OVERHANG = "countertop_overhang"

#: The architect's note, and its number as a reviewer would type it. `1 7/16"` is in no other
#: response field, so finding it anywhere in a body means the number leaked.
NOTE = 'OVERHANG 1 7/16" TYP.'
SEEN = '1 7/16"'
NUMBER_TRACES = ("7/16", "23/16", "1.4375")

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
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


class _Review:
    """One package whose sheet a person has split: the left half confirmed as the architect's
    drawing, the right half as the vendor's. With a client for its project."""

    def __init__(self, session: Session, store: LocalStore, sheet: bytes) -> None:
        from fastapi.testclient import TestClient

        from app.auth import Principal, Role, authenticate

        self.revision = _read(session, store, sheet, kind="shop")
        self.left, self.right = _drawings(
            session, self.revision, sheet, ViewRole.ARCH, ViewRole.SHOP
        )
        package = session.get(Package, self.revision.package_id)
        assert package is not None
        self.project = package.project_id
        self.url = f"/api/v1/projects/{self.project}/packages/{package.id}"
        session.commit()

        principal = Principal(
            id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({self.project})
        )
        app = create_app(_settings())
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_artifact_store] = lambda: store
        app.dependency_overrides[authenticate] = lambda: principal
        self.client = TestClient(app, raise_server_exceptions=False)

    def enter(self, *parameters: dict[str, Any], **body: Any) -> Any:
        return self.client.post(
            f"{self.url}/measurements", json={"parameters": list(parameters), **body}
        )


def _found(session: Session, store: LocalStore) -> tuple[_Review, UUID]:
    """A package whose architect's half states the overhang, and the pointer the app filed to it."""
    review = _Review(session, store, _sheet((LEFT, 70, NOTE), (RIGHT, 70, "ELEVATION")))
    proposed = propose_setting(session, review.revision.id, OVERHANG)
    assert proposed.outcome is ProposalOutcome.PROPOSED, proposed.note
    assert proposed.proposal_id is not None
    session.commit()
    return review, proposed.proposal_id


def _publish(session: Session, *files: str) -> None:
    for name in files:
        rule = Rule.model_validate(yaml.safe_load((RULEBOOK / name).read_text(encoding="utf-8")))
        snapshot = publish(rule)
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)
        session.flush()
        session.add(
            RuleSnapshot(
                rule_definition_id=definition.id,
                snapshot_id=snapshot.snapshot_id,
                version=rule.version,
                canonical_json=snapshot.canonical_json,
                product_type=rule.product_type.value,
                check_type=rule.check_type.value,
                unconfirmed_tolerance_count=0,
            )
        )
    session.commit()


def _stored(session: Session) -> int:
    return session.execute(select(func.count()).select_from(StoredValueRow)).scalar_one()


def _citations(session: Session) -> int:
    return session.execute(select(func.count()).select_from(ParameterValueCitation)).scalar_one()


def _overhang_rows(session: Session) -> list[StoredValueRow]:
    return list(
        session.scalars(
            select(StoredValueRow)
            .where(StoredValueRow.name == OVERHANG)
            .order_by(StoredValueRow.created_at)
        )
    )


# ---------------------------------------------------------------------------
# The pointer: a page and a crop, never the number
# ---------------------------------------------------------------------------


def test_the_pointer_schema_has_no_place_for_the_number() -> None:
    """**The schema is the guarantee.** An id to send back, a page, which upload, and whether a crop
    exists. No value, no text and no run ids, which `GET .../candidates` would turn back into the
    value. A new field fails here first, in the model and in the published API document alike."""
    expected = {"proposal_id", "page_index", "document_kind", "has_crop"}
    model = SettingPointerOut.model_json_schema()["properties"]
    published = create_app(_settings()).openapi()["components"]["schemas"]["SettingPointerOut"]

    assert set(model) == set(published["properties"]) == expected
    assert model["page_index"]["type"] == "integer" and model["has_crop"]["type"] == "boolean"
    assert {model["proposal_id"]["format"], model["document_kind"]["type"]} == {"uuid", "string"}


def test_the_form_points_at_the_page_and_never_states_the_number(
    session: Session, store: LocalStore
) -> None:
    """Outcome: the overhang names the page and the upload, and nothing in the whole response holds
    the passage's number or the ids of the runs that print it."""
    review, proposal_id = _found(session, store)
    _publish(session, "ct_depth_001.yaml")

    response = review.client.get(f"{review.url}/required-inputs")

    assert response.status_code == 200, response.text
    by_name = {parameter["name"]: parameter for parameter in response.json()["parameters"]}
    assert by_name[OVERHANG]["found"] == {
        "proposal_id": str(proposal_id),
        "page_index": 0,
        "document_kind": "shop",
        "has_crop": False,
    }
    assert by_name["cabinet_depth"]["found"] is None
    for trace in NUMBER_TRACES:
        assert trace not in response.text, trace
    assert str(_run(session, review.revision, SEEN).id) not in response.text


def test_the_crop_shows_the_passage_once_it_is_cut(session: Session, store: LocalStore) -> None:
    """Outcome: no crop before the crops are cut, and afterwards the stored crop of the number's
    own run, byte for byte."""
    review, proposal_id = _found(session, store)
    _publish(session, "ct_depth_001.yaml")
    crop_url = f"{review.url}/parameter-proposals/{proposal_id}/crop"
    assert review.client.get(crop_url).status_code == 404

    DatabaseStages(store).validate_evidence(session, review.revision.id)
    session.commit()

    by_name = {
        parameter["name"]: parameter
        for parameter in review.client.get(f"{review.url}/required-inputs").json()["parameters"]
    }
    assert by_name[OVERHANG]["found"]["has_crop"] is True
    response = review.client.get(crop_url)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    artifact = session.execute(
        select(EvidenceArtifact).where(
            EvidenceArtifact.candidate_id == _run(session, review.revision, SEEN).id
        )
    ).scalar_one()
    assert response.content == store.get(artifact.storage_key).read()
    assert review.client.get(f"{review.url}/parameter-proposals/{uuid4()}/crop").status_code == 404


# ---------------------------------------------------------------------------
# Done when: a vendor passage, a mismatch, a match
# ---------------------------------------------------------------------------


def test_a_hand_crafted_citation_of_a_vendor_passage_is_refused(
    session: Session, store: LocalStore
) -> None:
    """**Q10.** The architect says nothing; the vendor's half states the overhang. The proposer
    refuses it, so the pointer here is written by hand, as a forged request would name one.
    Outcome: 422 naming the vendor's drawing, even with the vendor's own number typed, and nothing
    stored."""
    review = _Review(session, store, _sheet((LEFT, 70, "ELEVATION"), (RIGHT, 70, NOTE)))
    assert propose_setting(session, review.revision.id, OVERHANG).outcome is (
        ProposalOutcome.NOT_FOUND
    )
    (hit,) = search_package_text(session, review.revision.id, "overhang")
    forged = ParameterProposal(
        package_revision_id=review.revision.id,
        setting_name=OVERHANG,
        phrase_id=hit.phrase_id,
        first_member=1,
        last_member=1,
        claimed_source=Provenance.GC_CLIENT.value,
        proposer="a hand-crafted request",
        proposer_version=PROPOSER_VERSION,
    )
    session.add(forged)
    session.commit()

    response = review.enter(
        {"name": OVERHANG, "value": SEEN, "citation": str(forged.id)},
        {"name": "cabinet_depth", "value": '24"'},
    )

    assert response.status_code == 422, response.text
    assert "vendor's drawing" in response.json()["message"]
    assert (_stored(session), _citations(session)) == (0, 0)


@pytest.mark.parametrize("typed", ['1 1/2"', '1 3/8"', '17/16"'])
def test_a_mismatch_is_refused_and_nothing_is_stored(
    session: Session, store: LocalStore, typed: str
) -> None:
    """**The admin's decision: a mismatch stops the save.** Outcome: 422 naming the page and saying
    nothing about which number the passage holds, and nothing stored — not the overhang, and not
    the cabinet depth typed beside it."""
    review, proposal_id = _found(session, store)

    response = review.enter(
        {"name": OVERHANG, "value": typed, "citation": str(proposal_id)},
        {"name": "cabinet_depth", "value": '24"'},
    )

    assert response.status_code == 422, response.text
    message = response.json()["message"]
    assert "not the one in the architect's drawing on page 1" in message
    for trace in NUMBER_TRACES:
        assert trace not in message, trace
    assert (_stored(session), _citations(session)) == (0, 0)


@pytest.mark.parametrize("typed", [SEEN, '1.4375"', "1 7/16 in"])
def test_a_match_is_stored_with_its_citation(
    session: Session, store: LocalStore, typed: str
) -> None:
    """Outcome: the typed value, exactly, as G.C / Client, with a reference naming the page and the
    document, and the passage kept beside it: document version, sha256, page and runs. The same
    number written another way is the same number."""
    review, proposal_id = _found(session, store)

    response = review.enter({"name": OVERHANG, "value": typed, "citation": str(proposal_id)})

    assert response.status_code == 201, response.text
    (echoed,) = response.json()["parameters"]
    page, version = _passage(session, proposal_id)
    reference = (
        f"Architect's drawing, page 1 of the shop document {version.sha256[:12]}: typed without "
        "being shown the number, and it matched"
    )
    assert (echoed["numerator"], echoed["denominator"]) == ("23", "16")
    assert (echoed["source"], echoed["reference"], echoed["citation"]) == (
        "G.C / Client",
        reference,
        str(proposal_id),
    )

    (row,) = _overhang_rows(session)
    assert (row.exact_value, row.provenance, row.source_reference) == (
        Fraction(23, 16),
        "G.C / Client",
        reference,
    )
    citation = session.execute(
        select(ParameterValueCitation).where(ParameterValueCitation.parameter_value_id == row.id)
    ).scalar_one()
    assert (
        citation.parameter_proposal_id,
        citation.document_version_id,
        citation.document_sha256,
        citation.page_id,
    ) == (proposal_id, version.id, version.sha256, page.id)
    assert _citation_runs(session, citation.id) == [_run(session, review.revision, SEEN).id]


def _passage(session: Session, proposal_id: UUID) -> tuple[Page, DocumentVersion]:
    """The page a pointer's passage is on, and the document version that page belongs to."""
    proposal = session.get(ParameterProposal, proposal_id)
    phrase = None if proposal is None else session.get(TextPhrase, proposal.phrase_id)
    page = None if phrase is None else session.get(Page, phrase.page_id)
    version = None if page is None else session.get(DocumentVersion, page.document_version_id)
    assert page is not None and version is not None
    return page, version


def _citation_runs(session: Session, citation_id: UUID) -> list[UUID]:
    return list(
        session.scalars(
            select(ParameterValueCitationRun.candidate_id)
            .where(ParameterValueCitationRun.citation_id == citation_id)
            .order_by(ParameterValueCitationRun.position)
        )
    )


# ---------------------------------------------------------------------------
# Every other way a citation is refused
# ---------------------------------------------------------------------------


def test_millimetres_are_refused_even_when_they_convert_exactly(
    session: Session, store: LocalStore
) -> None:
    """36.5125 mm is exactly 1 7/16". Outcome: refused all the same, because the passage is in
    inches (Q12) and a converted number is not one read off it."""
    review, proposal_id = _found(session, store)

    response = review.enter({"name": OVERHANG, "value": "36.5125 mm", "citation": str(proposal_id)})

    assert response.status_code == 422, response.text
    assert "type the number in inches" in response.json()["message"]
    assert _stored(session) == 0


def test_a_measurement_in_millimetres_is_never_compared_as_inches(
    session: Session, store: LocalStore
) -> None:
    """A caller handing over 23/16 *millimetres* must not match a passage of 23/16 inches."""
    review, proposal_id = _found(session, store)

    refused = confirm_typed_value(
        session,
        package_revision_id=review.revision.id,
        proposal_id=proposal_id,
        setting=OVERHANG,
        typed=Measurement(Fraction(23, 16), Unit.MM, None),
    )

    assert isinstance(refused, CitationRefusal)
    assert refused.reason is CitationRefusalReason.TYPED_MILLIMETRES


def test_a_pointer_a_newer_one_replaced_is_refused(session: Session, store: LocalStore) -> None:
    """The phrases are rebuilt under another gap and the proposer files a newer pointer to the same
    words. Outcome: the form offers the newer one, the older one's crop is no longer served, and a
    request still citing it is refused even with the right number, so a value is never held to a
    pointer the form has dropped.
    """
    review, older = _found(session, store)
    DatabaseStages(store).validate_evidence(session, review.revision.id)
    build_package_phrases(
        session, review.revision.id, PhraseGrouping(gap_line_heights=Decimal("0.35"))
    )
    newer = propose_setting(session, review.revision.id, OVERHANG).proposal_id
    session.commit()
    assert newer is not None and newer != older
    crop = f"{review.url}/parameter-proposals/{{}}/crop"
    assert review.client.get(crop.format(newer)).status_code == 200
    assert review.client.get(crop.format(older)).status_code == 404

    response = review.enter({"name": OVERHANG, "value": SEEN, "citation": str(older)})

    assert response.status_code == 422, response.text
    assert "a newer passage has replaced this one" in response.json()["message"]
    assert _stored(session) == 0
    accepted = review.enter({"name": OVERHANG, "value": SEEN, "citation": str(newer)})
    assert accepted.status_code == 201, accepted.text


def test_a_pointer_withdrawn_since_is_refused_and_no_longer_offered(
    session: Session, store: LocalStore
) -> None:
    """The architect's half is later confirmed as the vendor's. Outcome: the form stops offering the
    passage, its crop is not served, and a request still citing it is refused."""
    review, proposal_id = _found(session, store)
    _publish(session, "ct_depth_001.yaml")
    DatabaseStages(store).validate_evidence(session, review.revision.id)
    confirm_view_role(session, view=review.left, role=ViewRole.SHOP, actor="a reviewer")
    session.commit()

    required = review.client.get(f"{review.url}/required-inputs").json()
    crop = review.client.get(f"{review.url}/parameter-proposals/{proposal_id}/crop")
    response = review.enter({"name": OVERHANG, "value": SEEN, "citation": str(proposal_id)})

    assert {p["name"]: p["found"] for p in required["parameters"]}[OVERHANG] is None
    assert crop.status_code == 404
    assert response.status_code == 422, response.text
    assert "vendor's drawing" in response.json()["message"]
    assert _stored(session) == 0


@pytest.mark.parametrize(
    ("setting", "citation", "words"),
    [
        (OVERHANG, "unknown", "no passage stating countertop_overhang was found"),
        ("cabinet_depth", "real", "no passage stating cabinet_depth was found"),
    ],
)
def test_a_citation_this_package_did_not_file_for_the_setting_is_refused(
    session: Session, store: LocalStore, setting: str, citation: str, words: str
) -> None:
    """An id from nowhere, and the overhang's pointer sent for another setting."""
    review, proposal_id = _found(session, store)
    cited = str(uuid4()) if citation == "unknown" else str(proposal_id)

    response = review.enter({"name": setting, "value": SEEN, "citation": cited})

    assert response.status_code == 422, response.text
    assert words in response.json()["message"]
    assert _stored(session) == 0


@pytest.mark.parametrize(
    ("extra", "words"),
    [
        ({"reference": "Architect A-501"}, "takes its reference from that passage"),
        ({"source": "Company standard"}, "comes from 'G.C / Client'"),
    ],
)
def test_a_citation_with_its_own_source_or_reference_is_refused(
    session: Session, store: LocalStore, extra: dict[str, str], words: str
) -> None:
    """The passage says where the number is, so a cited entry may not say otherwise."""
    review, proposal_id = _found(session, store)

    response = review.enter(
        {"name": OVERHANG, "value": SEEN, "citation": str(proposal_id), **extra}
    )

    assert response.status_code == 422, response.text
    assert words in response.json()["message"]
    assert _stored(session) == 0


# ---------------------------------------------------------------------------
# The free-text path, a carried value, and the override report
# ---------------------------------------------------------------------------


def test_the_free_text_path_is_unchanged(session: Session, store: LocalStore) -> None:
    """With a live pointer to the overhang, an entry with no citation is stored as #827 stores it:
    the reviewer's reference, their word for the number, and no citation. Even a number that is not
    the passage's, because the free path never reads the passage."""
    review, _ = _found(session, store)

    response = review.enter(
        {"name": OVERHANG, "value": '1 1/2"', "reference": "Architect A-501, section 3"}
    )

    assert response.status_code == 201, response.text
    (echoed,) = response.json()["parameters"]
    assert (echoed["source"], echoed["reference"], echoed["citation"]) == (
        "G.C / Client",
        "Architect A-501, section 3",
        None,
    )
    (row,) = _overhang_rows(session)
    assert row.exact_value == Fraction(3, 2)
    assert _citations(session) == 0


def test_a_carried_value_keeps_its_citation(session: Session, store: LocalStore) -> None:
    """**#799's carry-forward.** A later save of the cabinet depth copies the overhang into a new
    version. Outcome: the copy carries the same passage, so the version the checks read still says
    where the number came from. A free entry sent in the same request as a cited one for the same
    setting replaces it, passage and all."""
    review, proposal_id = _found(session, store)
    assert (
        review.enter({"name": OVERHANG, "value": SEEN, "citation": str(proposal_id)}).status_code
        == 201
    )

    later = review.enter({"name": "cabinet_depth", "value": '24"'})

    assert later.status_code == 201, later.text
    first, carried = _overhang_rows(session)
    citations = {
        citation.parameter_value_id: citation
        for citation in session.scalars(select(ParameterValueCitation))
    }
    assert set(citations) == {first.id, carried.id}
    assert citations[carried.id].parameter_proposal_id == proposal_id
    assert _citation_runs(session, citations[carried.id].id) == _citation_runs(
        session, citations[first.id].id
    )

    replaced = review.enter(
        {"name": OVERHANG, "value": SEEN, "citation": str(proposal_id)},
        {"name": OVERHANG, "value": '1 1/2"'},
    )
    assert replaced.status_code == 201, replaced.text
    assert _overhang_rows(session)[-1].id not in {
        citation.parameter_value_id for citation in session.scalars(select(ParameterValueCitation))
    }


def test_the_override_report_shows_the_citation(session: Session, store: LocalStore) -> None:
    """Q10's report, built from what was stored: the project's overhang displaces a company value,
    and the line says the number was typed from the architect's drawing, which page, and that it
    matched."""
    review, proposal_id = _found(session, store)
    review.enter({"name": OVERHANG, "value": SEEN, "citation": str(proposal_id)})
    (project,) = load_parameter_sets(session, review.project)
    company = ParameterSet(
        project_id=None,
        layer=ParameterLayer.GLOBAL,
        version=1,
        parameters={
            OVERHANG: ParameterValue(
                value=Quantity(value=Fraction(1), unit=Unit.INCH),
                provenance=Provenance.COMPANY_STANDARD,
                set_by="admin",
                set_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        },
    )

    text = as_text(override_report([], company, project))

    _, version = _passage(session, proposal_id)
    assert (
        f"countertop_overhang = 1 7/16 in (project, G.C / Client: Architect's drawing, page 1 of "
        f"the shop document {version.sha256[:12]}: typed without being shown the number, and it "
        "matched, set by anant); overrides 1 in (global, Company standard)"
    ) in text


# ---------------------------------------------------------------------------
# End to end: the vendor's note never decides
# ---------------------------------------------------------------------------


def test_the_vendors_note_equal_to_its_drawn_value_gives_not_found_never_pass(
    session: Session, store: LocalStore
) -> None:
    """**The false PASS this issue exists to close.** The vendor's drawing says `OVERHANG 1 7/16"`
    and draws a countertop 25 7/16" deep: 24" of cabinet plus exactly that overhang. The architect's
    drawing says nothing about the overhang. Taking the vendor's note would PASS the vendor against
    themselves. Outcome: nothing is proposed, a forged citation of the note is refused, and the
    depth check says NOT_FOUND because the overhang is missing."""
    review = _Review(session, store, _sheet((LEFT, 70, "ID SET ELEVATION"), (RIGHT, 70, NOTE)))
    _publish(session, "ct_depth_001.yaml")

    proposed = propose_setting(session, review.revision.id, OVERHANG)
    assert proposed.outcome is ProposalOutcome.NOT_FOUND
    (hit,) = search_package_text(session, review.revision.id, "overhang")
    forged = ParameterProposal(
        package_revision_id=review.revision.id,
        setting_name=OVERHANG,
        phrase_id=hit.phrase_id,
        first_member=1,
        last_member=1,
        claimed_source=Provenance.GC_CLIENT.value,
        proposer="a hand-crafted request",
        proposer_version=PROPOSER_VERSION,
    )
    session.add(forged)
    session.commit()
    drawn_depth = {"rule_id": "CT-DEPTH-001", "name": "countertop_depth", "value": '25 7/16"'}

    refused = review.enter(
        {"name": OVERHANG, "value": SEEN, "citation": str(forged.id)},
        {"name": "cabinet_depth", "value": '24"'},
        measurements=[drawn_depth],
    )
    entered = review.enter({"name": "cabinet_depth", "value": '24"'}, measurements=[drawn_depth])
    DatabaseStages().run_checks(session, review.revision.id)

    assert refused.status_code == 422, refused.text
    assert entered.status_code == 201, entered.text
    outcomes = {
        from_row(snapshot).rule.id: finding
        for finding, snapshot in session.execute(
            select(Finding, RuleSnapshot)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .where(Finding.package_revision_id == review.revision.id)
        ).all()
    }
    depth = outcomes["CT-DEPTH-001"]
    assert str(depth.outcome) == "NOT_FOUND"
    assert "'countertop_overhang'" in (depth.reason or "")
