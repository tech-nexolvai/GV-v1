"""The value-hunter looks for each setting the rules still need, and files pointers only through
the guard (#881).

Verification for `workflow/value_hunter.py` and `workflow.parameter_proposals.propose_passage`; the
worker's hook is in `tests/scripts/test_drain_outbox.py`.

The issue's tests come first, in its order: the toolbox is sealed; a passage no tool returned in this
run is refused; a company-standard or defaulted setting is never proposed; a vendor-side passage is
never proposed; every outstanding setting ends in exactly one `Proposal` or `NotFound`, with a
reason; and the hunter is off by default. The one that matters most is the vendor's: a passage on
the drawing under review must never become the setting that drawing is checked against.

The sheets are the synthetic ones `tests/workflow/test_parameter_proposals.py` reads through the real
extraction stage, and the rules are the rulebook's own files, published.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
import typing
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import Package, PackageRevision, ParameterProposal, ViewRole
from app.models.parameters import to_rows
from app.models.rules import RuleDefinition, RuleSnapshot
from retrieval.package_text import search_package_text
from rules.parameter_sources import ALLOWED_SOURCES, citable_sources
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity, Rule
from rules.snapshot import publish
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_parameter_proposals import LEFT, RIGHT, _drawings, _read, _run, _sheet
from units.measurement import Unit
from workflow import value_hunter
from workflow.parameter_citations import Citation, check_proposal, live_parameter_proposals
from workflow.parameter_proposals import ProposalOutcome, propose_passage
from workflow.value_hunter import (
    PERMITTED_TOOLS,
    VALUE_HUNTER_ENV,
    Hunt,
    HunterToolbox,
    HuntProgress,
    NotFound,
    NotFoundArguments,
    Planner,
    Proposal,
    ProposeArguments,
    Refused,
    SearchArguments,
    ToolCall,
    ToolName,
    hunt_setting,
    hunt_values,
    outstanding_settings,
    plan_setting,
    table_planner,
    value_hunter_enabled,
)

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]
RULEBOOK = REPO_ROOT / "rules" / "rulebook"

OVERHANG = "countertop_overhang"

SEARCH, PROPOSE, NOT_FOUND = ToolName.SEARCH_PACKAGE_TEXT, ToolName.PROPOSE, ToolName.NOT_FOUND


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _rule(
    name: str, *, version: str | None = None, undefault: tuple[str, ...] = (), **defaults: str
) -> Rule:
    """A rulebook file as a rule, with a default in inches declared for each setting named, and the
    default (and its note) removed from each setting in `undefault`."""
    data: dict[str, Any] = yaml.safe_load((RULEBOOK / name).read_text(encoding="utf-8"))
    if version is not None:
        data["version"] = version
    for setting in undefault:
        data["parameters"][setting].pop("default")
        data["parameters"][setting].pop("note", None)
    for setting, value in defaults.items():
        data["parameters"][setting]["default"] = {"value": value, "unit": "in"}
    return Rule.model_validate(data)


def _publish(session: Session, *rules: Rule) -> None:
    for rule in rules:
        snapshot = publish(rule)
        definition = session.scalar(select(RuleDefinition).where(RuleDefinition.rule_id == rule.id))
        if definition is None:
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
    session.flush()


def _store_set(
    session: Session, revision: PackageRevision, layer: ParameterLayer, name: str, value: Fraction
) -> None:
    """A setting a person typed for the project, or for this review alone."""
    package = session.get(Package, revision.package_id)
    assert package is not None
    parameters = ParameterSet(
        project_id=str(package.project_id),
        layer=layer,
        version=1,
        parameters={
            name: ParameterValue(
                value=Quantity(value=value, unit=Unit.INCH),
                provenance=Provenance.GC_CLIENT,
                set_by="a reviewer",
                set_at=datetime(2026, 10, 3, tzinfo=UTC),
            )
        },
    )
    stored, rows = to_rows(
        parameters, package_revision_id=revision.id if layer is ParameterLayer.RUN else None
    )
    session.add(stored)
    session.add_all(rows)
    session.flush()


def _pointers(session: Session) -> list[ParameterProposal]:
    return list(
        session.scalars(
            select(ParameterProposal).order_by(ParameterProposal.created_at, ParameterProposal.id)
        )
    )


def _passage(session: Session, revision: PackageRevision, query: str) -> UUID:
    """The one passage `query` finds, found outside any hunt."""
    (hit,) = search_package_text(session, revision.id, query)
    return hit.phrase_id


class _Recording:
    """A planner, with every call it names written down."""

    def __init__(self, planner: Planner = table_planner) -> None:
        self.calls: list[ToolCall] = []
        self._planner = planner

    def __call__(self, progress: HuntProgress) -> ToolCall:
        call = self._planner(progress)
        self.calls.append(call)
        return call

    @property
    def tools(self) -> list[ToolName]:
        return [call.name for call in self.calls]


# ---------------------------------------------------------------------------
# The toolbox is sealed, and the endings carry no number
# ---------------------------------------------------------------------------


def test_the_toolbox_is_sealed() -> None:
    """Three tools, fixed at construction. Outcome: no fourth can be passed in, added or swapped in,
    nothing but a typed call from the three is carried out, and a search for words that are not the
    setting's is refused without the package being touched (the session here is not one)."""
    toolbox = HunterToolbox(cast(Session, object()), uuid4(), OVERHANG)

    assert set(ToolName) == {SEARCH, PROPOSE, NOT_FOUND}
    assert toolbox.permitted_tools == PERMITTED_TOOLS == frozenset(ToolName)
    # The tools are the class's own code: nothing a caller hands the constructor is a handler.
    assert list(inspect.signature(HunterToolbox).parameters) == [
        "session",
        "package_revision_id",
        "setting",
    ]
    assert {name for name in dir(HunterToolbox) if not name.startswith("_")} == {
        "calls",
        "found",
        "invoke",
        "permitted_tools",
        "refusals",
        "searched",
        "tried",
    }
    for name in ("register", "_search", "_propose", "_terms", "_found"):
        with pytest.raises(AttributeError, match="fixed at construction"):
            setattr(toolbox, name, lambda *_: None)

    with pytest.raises(TypeError):
        toolbox.invoke(cast(ToolCall, SearchArguments("overhang")))
    with pytest.raises(TypeError):
        ToolCall(cast(SearchArguments, "propose every passage"))
    assert [
        ToolCall(arguments).name
        for arguments in (
            SearchArguments("overhang"),
            ProposeArguments(uuid4()),
            NotFoundArguments("a reason"),
        )
    ] == [SEARCH, PROPOSE, NOT_FOUND]

    refused = toolbox.invoke(ToolCall(SearchArguments("24")))
    assert refused == Refused("the query is not one of the search words written for " + OVERHANG)
    ended = toolbox.invoke(ToolCall(NotFoundArguments("a reason")))
    assert ended == NotFound(OVERHANG, "a reason")
    assert [call.name for call in toolbox.calls] == [SEARCH, NOT_FOUND]


def test_neither_ending_carries_a_number() -> None:
    """**The schema is the guarantee**, as it is for `PhraseHit`. Outcome: the endings hold a
    setting's name, a passage's id and words, and nothing typed to hold a measurement; and the
    worker's log line holds no passage id either."""
    numeric = (int, float, Fraction, Decimal)
    for result in (Proposal, NotFound, Refused):
        for name, hint in typing.get_type_hints(result).items():
            assert not any(kind in numeric for kind in (typing.get_args(hint) or (hint,))), name
    assert [field.name for field in fields(Proposal)] == ["setting", "phrase_id"]
    assert [field.name for field in fields(NotFound)] == ["setting", "reason"]

    passage = uuid4()
    summary = Hunt((Proposal(OVERHANG, passage), NotFound("cabinet_depth", "a reason"))).summary()
    assert summary == {
        "ran": True,
        "outstanding": 2,
        "proposed": [OVERHANG],
        "not_found": {"cabinet_depth": "a reason"},
    }
    assert str(passage) not in str(summary)


# ---------------------------------------------------------------------------
# A passage no tool returned in this run
# ---------------------------------------------------------------------------


def test_a_passage_no_search_in_this_run_returned_is_refused(
    session: Session, store: LocalStore
) -> None:
    """The passage is the architect's and states the overhang, so the guard itself would pass it.
    Outcome: refused all the same when the search that found it was made outside the run, or by
    another run, and a planner that names it without searching ends not found. Nothing is filed
    until the run's own search returns it."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="shop")
    _drawings(session, revision, sheet, ViewRole.ARCH)
    passage = _passage(session, revision, "overhang")

    toolbox = HunterToolbox(session, revision.id, OVERHANG)
    refused = toolbox.invoke(ToolCall(ProposeArguments(passage)))
    assert refused == Refused(
        "no search in this run returned this passage, so it may not be proposed"
    )

    other = HunterToolbox(session, revision.id, OVERHANG)
    hits = other.invoke(ToolCall(SearchArguments("overhang")))
    assert isinstance(hits, tuple) and [hit.phrase_id for hit in hits] == [passage]
    assert isinstance(toolbox.invoke(ToolCall(ProposeArguments(passage))), Refused)
    assert toolbox.tried == () and toolbox.refusals == ()

    recording = _Recording(lambda _: ToolCall(ProposeArguments(passage)))
    ending = hunt_setting(session, revision.id, plan_setting(OVERHANG), recording)
    assert isinstance(ending, NotFound)
    assert "was stopped" in ending.reason
    assert _pointers(session) == []

    filed = other.invoke(ToolCall(ProposeArguments(passage)))
    assert filed == Proposal(OVERHANG, passage)
    (row,) = _pointers(session)
    assert (row.setting_name, row.phrase_id) == (OVERHANG, passage)
    # Once tried, never again in the same run.
    assert other.invoke(ToolCall(ProposeArguments(passage))) == Refused(
        "this passage was already proposed in this run"
    )


# ---------------------------------------------------------------------------
# A company standard, a defaulted setting, the vendor's drawing
# ---------------------------------------------------------------------------


def test_a_company_standard_or_defaulted_setting_is_never_proposed(
    session: Session, store: LocalStore
) -> None:
    """The architect's confirmed drawing states the field cut and the overhang. The field cut is
    GV's own and — with its 1 in default removed, as before #991 — outstanding; the overhang has a
    declared default. Outcome: the field cut ends at
    once, without a search, saying it is never read off a package; the overhang is not looked for at
    all; nothing is filed. Then the rule drops the default, and the same sheet gets the overhang
    proposed: the default was the only thing keeping it out."""
    sheet = _sheet((LEFT, 80, 'FIELD CUT 1"'), (LEFT, 50, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="shop")
    _drawings(session, revision, sheet, ViewRole.ARCH)
    _publish(
        session,
        _rule("ct_width_001.yaml", undefault=("field_cut",)),
        _rule("ct_depth_001.yaml", countertop_overhang="1"),
    )

    assert outstanding_settings(session, revision.id) == ("cabinet_depth", "field_cut")
    recording = _Recording()
    ending = hunt_setting(session, revision.id, plan_setting("field_cut"), recording)
    assert ending == NotFound(
        "field_cut", "field_cut comes from Company standard, which is never read off a package"
    )
    assert recording.tools == [NOT_FOUND]
    hunt = hunt_values(session, revision.id)
    assert [ending.setting for ending in hunt.endings] == ["cabinet_depth", "field_cut"]
    assert not any(isinstance(ending, Proposal) for ending in hunt.endings)
    assert _pointers(session) == []

    # Every setting that may cite nothing, however it is asked for.
    field_cut = _passage(session, revision, '"field cut"')
    uncitable = sorted(setting for setting in ALLOWED_SOURCES if not citable_sources(setting))
    assert {"field_cut", "front_offset_required", "sink_cutout_clearance"} <= set(uncitable)
    for setting in uncitable:
        planned = plan_setting(setting)
        assert planned.terms == () and planned.not_searched is not None
        assert planned.not_searched.endswith("which is never read off a package"), setting
        toolbox = HunterToolbox(session, revision.id, setting)
        assert isinstance(toolbox.invoke(ToolCall(SearchArguments('"field cut"'))), Refused)
        assert isinstance(toolbox.invoke(ToolCall(ProposeArguments(field_cut))), Refused)
        result = propose_passage(session, revision.id, setting, field_cut)
        assert result.outcome is ProposalOutcome.REFUSED, setting
    assert _pointers(session) == []

    _publish(session, _rule("ct_depth_001.yaml", version="1.0.2"))
    hunt = hunt_values(session, revision.id)
    assert [ending.setting for ending in hunt.endings if isinstance(ending, Proposal)] == [OVERHANG]
    assert [row.setting_name for row in _pointers(session)] == [OVERHANG]


@pytest.mark.parametrize("drawings", ["confirmed", "none"])
def test_a_vendor_side_passage_is_never_proposed(
    session: Session, store: LocalStore, drawings: str
) -> None:
    """**The point of Q10.** The vendor's drawing states the overhang, and nothing else does,
    whether a person confirmed the drawing as the vendor's or the upload says the whole file is.
    Outcome: the hunt ends not found, saying the passage is on the vendor's drawing; proposing it
    straight after finding it is refused by the guard; nothing is filed."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="shop")
    if drawings == "confirmed":
        _drawings(session, revision, sheet, ViewRole.SHOP)
    _publish(session, _rule("ct_depth_001.yaml"))

    hunt = hunt_values(session, revision.id)

    overhang = {ending.setting: ending for ending in hunt.endings}[OVERHANG]
    assert isinstance(overhang, NotFound)
    assert overhang.reason.startswith("1 passage(s) mention overhang, and none may be cited: 1 × ")
    assert "vendor's drawing under review" in overhang.reason
    assert _pointers(session) == []

    toolbox = HunterToolbox(session, revision.id, OVERHANG)
    hits = toolbox.invoke(ToolCall(SearchArguments("overhang")))
    assert isinstance(hits, tuple)
    (hit,) = hits
    refused = toolbox.invoke(ToolCall(ProposeArguments(hit.phrase_id)))
    assert isinstance(refused, Refused) and not refused.disagreement
    assert "vendor's drawing under review" in refused.reason
    assert _pointers(session) == []


# ---------------------------------------------------------------------------
# Every outstanding setting ends exactly once
# ---------------------------------------------------------------------------


def test_every_outstanding_setting_ends_in_exactly_one_proposal_or_not_found(
    session: Session, store: LocalStore
) -> None:
    """The whole rulebook, published. The architect's half states the overhang and the vendor's half
    a different one; the project has set the cabinet depth, and this review the sink's width.
    Outcome: every outstanding setting ends exactly once, in the plan's order; the overhang in the
    one pointer filed, to the architect's passage, which the form then offers; every other ending
    with a reason that quotes nothing from the drawing. A setting someone set, and one with a
    default, is not looked for. Run again, it files nothing new."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2" TYP.'), (RIGHT, 70, 'OVERHANG 1"'))
    revision = _read(session, store, sheet, kind="shop")
    _drawings(session, revision, sheet, ViewRole.ARCH, ViewRole.SHOP)
    _publish(session, *(_rule(path.name) for path in sorted(RULEBOOK.glob("*.yaml"))))
    _store_set(session, revision, ParameterLayer.PROJECT, "cabinet_depth", Fraction(24))
    _store_set(session, revision, ParameterLayer.RUN, "sink_interior_width", Fraction(30))

    hunt = hunt_values(session, revision.id)

    settings = [ending.setting for ending in hunt.endings]
    assert settings == [
        "backsplash_thickness",
        "cabinet_side_thickness",
        OVERHANG,
        "double_door_cab_width_max",
        "double_door_cab_width_min",
        "drawer_cab_width_max",
        "drawer_cab_width_min",
        "single_door_cab_width_max",
        "single_door_cab_width_min",
        "sink_interior_depth",
    ]
    assert list(outstanding_settings(session, revision.id)) == settings
    assert all(isinstance(ending, Proposal | NotFound) for ending in hunt.endings)

    (proposal,) = (ending for ending in hunt.endings if isinstance(ending, Proposal))
    (row,) = _pointers(session)
    assert (row.setting_name, row.phrase_id) == (OVERHANG, proposal.phrase_id)
    citation = check_proposal(session, row)
    assert isinstance(citation, Citation)
    assert citation.candidate_ids == (_run(session, revision, '1 1/2"').id,)
    assert live_parameter_proposals(session, revision.id) == {OVERHANG: row}

    reasons = {
        ending.setting: ending.reason for ending in hunt.endings if isinstance(ending, NotFound)
    }
    assert set(reasons) == set(settings) - {OVERHANG}
    assert reasons["backsplash_thickness"] == (
        'no passage in this package\'s own words mentions backsplash, "back splash"'
    )
    # The field cut has GV's 1 in standard since #991, so it is not looked for at all.
    assert "field_cut" not in settings
    assert reasons["single_door_cab_width_min"] == (
        "no search words are written for single_door_cab_width_min (vocabulary/parameter_terms.py)"
    )
    assert not any("1/2" in reason or "TYP" in reason for reason in reasons.values())
    assert hunt.summary() == {
        "ran": True,
        "outstanding": len(settings),
        "proposed": [OVERHANG],
        "not_found": reasons,
    }

    assert hunt_values(session, revision.id) == hunt
    assert len(_pointers(session)) == 1


def test_two_architect_passages_that_disagree_end_not_found(
    session: Session, store: LocalStore
) -> None:
    """Outcome: not found, saying a reviewer decides, in words that quote neither value. One
    proposal is enough to learn that no passage can be proposed, so the run stops there."""
    sheet = _sheet((LEFT, 80, 'OVERHANG 1 1/2"'), (LEFT, 50, 'OVERHANG 1"'))
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)
    recording = _Recording()

    ending = hunt_setting(session, revision.id, plan_setting(OVERHANG), recording)

    assert isinstance(ending, NotFound)
    assert "do not all agree; a reviewer decides" in ending.reason
    assert '1"' not in ending.reason and "1/2" not in ending.reason
    assert recording.tools == [SEARCH, PROPOSE, NOT_FOUND]
    assert _pointers(session) == []


def test_a_planner_is_held_to_its_bounds(session: Session, store: LocalStore) -> None:
    """One search word, and one passage it finds. Outcome: a planner that searches for ever is
    stopped after three steps; one that fails, or names no tool call, ends the setting as not found;
    none files anything."""
    sheet = _sheet((LEFT, 70, "OVERHANG TYP."))
    revision = _read(session, store, sheet, kind="architectural")
    planned = plan_setting(OVERHANG)

    again = _Recording(lambda _: ToolCall(SearchArguments("overhang")))
    ending = hunt_setting(session, revision.id, planned, again)
    assert ending == NotFound(
        OVERHANG,
        "the hunt for countertop_overhang asked for more than one search per word and one "
        "proposal per passage found, and was stopped",
    )
    assert len(again.calls) == 3

    def _broken(_: HuntProgress) -> ToolCall:
        raise RuntimeError("a planner bug")

    assert hunt_setting(session, revision.id, planned, _broken) == NotFound(
        OVERHANG, "the plan for countertop_overhang failed: RuntimeError"
    )
    assert hunt_setting(
        session, revision.id, planned, lambda _: cast(ToolCall, "propose it")
    ) == NotFound(OVERHANG, "the plan for countertop_overhang named no tool call")
    assert _pointers(session) == []


# ---------------------------------------------------------------------------
# The door: `propose_passage`, and nothing else
# ---------------------------------------------------------------------------


def test_a_passage_its_settings_words_do_not_find_is_not_filed(
    session: Session, store: LocalStore
) -> None:
    """`propose_passage` holds on its own, whoever calls it. An architect's passage stating the
    cabinet depth, named for the overhang. Outcome: not filed; named for the cabinet depth, filed.
    """
    sheet = _sheet((LEFT, 70, 'CABINET DEPTH 24"'))
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)
    passage = _passage(session, revision, '"cabinet depth"')

    result = propose_passage(session, revision.id, OVERHANG, passage)

    assert result.outcome is ProposalOutcome.NOT_FOUND and result.proposal_id is None
    assert result.note == (
        "this passage is not one the search words for countertop_overhang find in this package's "
        "current phrases"
    )
    assert _pointers(session) == []
    filed = propose_passage(session, revision.id, "cabinet_depth", passage)
    assert filed.outcome is ProposalOutcome.PROPOSED
    assert [row.id for row in _pointers(session)] == [filed.proposal_id]


def test_the_hunter_reaches_the_pointer_table_only_through_the_guard() -> None:
    """**Structure as well as behaviour.** Outcome: the hunter imports `propose_passage`, nothing
    private from the proposer or the guard, and never names the pointer table or its writers."""
    tree = ast.parse(Path(value_hunter.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert ("workflow.parameter_proposals", "propose_passage") in imported
    assert not any(module == "app.models.parameter_proposals" for module, _ in imported)
    assert not any(
        name.startswith("_")
        for module, name in imported
        if module in {"workflow.parameter_proposals", "workflow.parameter_citations"}
    )
    assert not named & {"ParameterProposal", "_record", "_check", "propose_setting"}


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "on"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("no", False),
        ("on", False),
        ("1", True),
        ("true", True),
        (" YES ", True),
    ],
)
def test_the_hunter_is_off_by_default(stated: str | None, on: bool) -> None:
    """Outcome: off unless the switch says 1, true or yes, the words the reading agent's takes."""
    environ = {} if stated is None else {VALUE_HUNTER_ENV: stated}
    assert value_hunter_enabled(environ) is on


def test_the_demo_does_not_switch_it_on() -> None:
    """Step 5.3 of #798 measures it before anyone switches it on, and the demo is a deployment."""
    assert VALUE_HUNTER_ENV not in (REPO_ROOT / "scripts" / "demo.sh").read_text(encoding="utf-8")
