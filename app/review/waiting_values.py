"""What a check waiting on a value is waiting for, as fields a reviewer can fill (#1181).

Six checks (countertop depth, the two sink cut-out sizes, the sink front offset, the back-offset
minimum and the sink cabinet width) end "Waiting on a value" on every real set: their inputs are on
no shop drawing and nothing reads them. The reason said so in prose — "could not resolve required
value 'countertop_overhang'" — and named only the first value the arithmetic tripped on, so a
reviewer typing it was then told about the next one.

**Worked out from the rule and from what the run had, never from the reason's words.** For one
revision-wide `NOT_FOUND` result:

* a **setting** is missing when the rule declares it with no default and none of the settings
  versions the run cited holds it (the company's, the project's, this package's);
* a **drawing value** is missing when the run recorded no qualified operand for it
  (`verdict_inputs`). It is listed with `askable: false`, never as a box: people confirm readings,
  they do not fill in measurements (GV's rule; Anant, 2026-10-12), so the check waits for the
  drawing to be read or supplied.

**The two cabinet checks ask too (#1205).** Their reason named one value (the first the arithmetic
tripped on) while up to four lists, two reviewer entries and six width limits were missing, and the
list said nothing. Their values are of a third kind: a **reviewer's entry on Measurements** (the
cabinet lists, each cabinet's type, the site width; GV's method has the reviewer enter these),
listed with where it is entered and in what order, never a box here. Their six cabinet width
limits are project settings, asked like any other.

Only these checks ask (`WAITING_RULES`). Nothing is filled in, defaulted or treated as zero
here: a missing value is listed, and the check keeps waiting until it is supplied and the checks run
again. A setting the rulebook gives a
default (the 4" front offset, the ¼" clearance) is never listed, because the run had it.

**`saved` says when a value has been typed since.** The result still waits — only a new check run
uses a value — so the screen says "run checks" rather than asking a second time. Read from the
layers a run would read now: the company's newest settings, the project's, this package's.

Read-only, and no import reaches the code that runs a check (`tests/api/test_no_heavy_work.py`).

Source: issue #1181. Verification: `tests/api/test_waiting_values.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RuleSnapshot, VerdictInput
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.review.settings_in_force import SINK_SETTINGS
from app.schemas.findings import MissingInputOut, SavedInputOut
from app.verdicts.rulebook import UnreadableSnapshot, from_row
from rules.parameter_sources import SOURCE_GUIDANCE, allowed_sources
from rules.parameters import ParameterLayer
from rules.required_inputs import BLOCKED_PARAMETERS
from rules.schema import Cardinality, Rule
from rules.snapshot import SnapshotIntegrityError
from units.imperial import format_inches
from units.measurement import Unit
from verdict.outcomes import Outcome
from vocabulary.semantic_types import CLIENT_CODES, SemanticType
from vocabulary.setting_names import setting_name
from workflow.measurements import DRAWN_SOURCES, drawn_input

__all__ = [
    "DRAWING_GUIDE",
    "SETTING_GUIDE",
    "SINK_SETTINGS",
    "WAITING_RULES",
    "WaitingFinding",
    "missing_inputs_for",
]

Scope = Literal["project", "sink", "drawing", "measurements"]


class WaitingFinding(Protocol):
    """What `missing_inputs_for` reads of a result: a listed `FindingOut`, or the per-countertop
    check plan's own record of a live whole-package result (`workflow/item_plan.py`, #1245)."""

    @property
    def id(self) -> UUID: ...
    @property
    def check_run_id(self) -> UUID: ...
    @property
    def package_revision_id(self) -> UUID: ...
    @property
    def rule_id(self) -> str: ...
    @property
    def rule_snapshot_id(self) -> UUID: ...
    @property
    def outcome(self) -> Outcome: ...
    @property
    def scope_item_id(self) -> UUID | None: ...
    @property
    def scope_row_candidate_id(self) -> UUID | None: ...
    @property
    def parameter_set_versions(self) -> Mapping[str, str]: ...


#: The checks this asks for: the six of #1181 and the two cabinet checks (#1205). Every other
#: check's waiting is answered elsewhere: a countertop's on its own card.
WAITING_RULES: Final[frozenset[str]] = frozenset(
    {
        "CT-DEPTH-001",
        "CT-SINK-CUTOUT-WIDTH-001",
        "CT-SINK-CUTOUT-DEPTH-001",
        "CT-SINK-OFFSET-FRONT-001",
        "CT-BACK-OFFSET-MIN-001",
        "CT-SINK-CABINET-WIDTH-001",
        "CAB-FILLER-001",
        "CAB-ARCH-VS-SHOP-001",
    }
)

#: The five depth and sink checks, which the depth and sink reader scopes to each countertop when it
#: is on (#1290). Such a countertop's own result still waits on the same settings, so it asks for
#: them too; what was read for it is shown beside them (`app/review/depth_readings.py`, #1295).
#: The same five as `workflow.depth_evidence.DEPTH_RULE_IDS`, held so by a test: importing it here
#: would loop back through `workflow.item_plan`.
PER_COUNTERTOP_RULES: Final[frozenset[str]] = frozenset(
    {
        "CT-DEPTH-001",
        "CT-SINK-CUTOUT-WIDTH-001",
        "CT-SINK-CUTOUT-DEPTH-001",
        "CT-SINK-OFFSET-FRONT-001",
        "CT-BACK-OFFSET-MIN-001",
    }
)

#: Where a list a check lacks is entered (a row of cabinets keeps its order there).
LIST_ON_MEASUREMENTS: Final = "Entered on Measurements, where a row keeps its order."

#: Where a list is entered for a check that compares it member by member, by the check's operation:
#: the order is what the check compares, so it is said (#1205).
LIST_ORDER: Final[Mapping[str, str]] = {
    "cabinet_run_distribution": "Entered on Measurements, in wall order from left to right.",
    "pairwise_within_tolerance": (
        "Entered on Measurements, in the same order as the other list of cabinets."
    ),
}


@dataclass(frozen=True, slots=True)
class Guide:
    """What to call a value and where a reviewer finds it, in plain words."""

    label: str
    where_to_find: str


#: Each setting a waiting check most often lacks, named the way the client's checklist names it, with
#: where it is found. A setting not listed is named from its code name, and told where by its
#: allowed sources (`SOURCE_GUIDANCE`), so a rule gaining a setting is still asked for.
SETTING_GUIDE: Final[Mapping[str, Guide]] = {
    "countertop_overhang": Guide(
        "Countertop overhang",
        "The architect's countertop section or the project spec: how far the stone runs past the "
        "cabinet front.",
    ),
    "backsplash_thickness": Guide(
        "Backsplash thickness",
        "The architect's countertop section or the project spec.",
    ),
    "cabinet_depth": Guide(
        "Cabinet depth",
        "The architect's cabinet section or the project spec: the cabinet box plus its door.",
    ),
    "cabinet_side_thickness": Guide(
        "Cabinet side panel thickness",
        "The project spec or the cabinet maker's standard: one side panel of the sink cabinet.",
    ),
    "sink_interior_width": Guide(
        "Sink inside width",
        "The sink's spec sheet: the bowl's inside width, side to side.",
    ),
    "sink_interior_depth": Guide(
        "Sink inside depth",
        "The sink's spec sheet: the bowl's inside depth, front to back.",
    ),
}

#: Each drawn dimension a waiting check may lack, by the client's code, and the view it is on (the
#: client lead: plan for the cut-out and offsets, elevation for widths, section for overhang).
DRAWING_GUIDE: Final[Mapping[str, Guide]] = {
    SemanticType.CT004.value: Guide(
        "Sink cabinet width", "The vendor's elevation: the sink cabinet's width."
    ),
    SemanticType.CT007.value: Guide(
        "Sink front offset, as drawn",
        "The vendor's plan view: from the cabinet front to the front edge of the sink cut-out.",
    ),
    SemanticType.CT008.value: Guide(
        "Sink cut-out depth, as drawn",
        "The vendor's plan view: the sink cut-out, front to back.",
    ),
    SemanticType.CT010.value: Guide(
        "Countertop depth, as drawn",
        "The vendor's plan view or section: the countertop, from its front edge to the wall.",
    ),
    SemanticType.CT011.value: Guide(
        "Left clearance to the cut-out, as drawn",
        "The vendor's plan view: from the sink cabinet's left inside face to the cut-out.",
    ),
    SemanticType.CT012.value: Guide(
        "Sink cut-out width, as drawn",
        "The vendor's plan view: the sink cut-out, side to side.",
    ),
    SemanticType.CT013.value: Guide(
        "Right clearance to the cut-out, as drawn",
        "The vendor's plan view: from the cut-out to the sink cabinet's right inside face.",
    ),
}

#: Each value a cabinet check takes from the reviewer, by source and type (#1205): the cabinet lists,
#: each cabinet's type and the site width. Entered on Measurements; GV's method has the reviewer
#: enter them (the client lead's deck, slide 11: the reviewer marks each cabinet's category).
REVIEWER_GUIDE: Final[Mapping[tuple[str, str], Guide]] = {
    ("USER_INPUT", SemanticType.FIELD_DIMENSION.value): Guide(
        "Site width, wall to wall",
        "Entered on Measurements: the wall-to-wall width measured on site for this run.",
    ),
    ("USER_INPUT", SemanticType.CABINET_CATEGORY.value): Guide(
        "Each cabinet's type",
        "Marked on Measurements, cabinet by cabinet: single door, double door or drawer, or an "
        "equipment cabinet, whose width never changes.",
    ),
    ("ARCH", SemanticType.CABINET_WIDTH.value): Guide(
        "The architect's cabinet widths", LIST_ON_MEASUREMENTS
    ),
    ("SHOP", SemanticType.CABINET_WIDTH.value): Guide(
        "The vendor's cabinet widths", LIST_ON_MEASUREMENTS
    ),
    ("ARCH", SemanticType.FILLER_WIDTH.value): Guide(
        "The architect's filler widths", LIST_ON_MEASUREMENTS
    ),
    ("SHOP", SemanticType.FILLER_WIDTH.value): Guide(
        "The vendor's filler widths", LIST_ON_MEASUREMENTS
    ),
}

#: The one value a check may lack that is not a length: each cabinet's type.
_NOT_A_LENGTH: Final = frozenset({SemanticType.CABINET_CATEGORY.value})

_DRAWING_SIDE: Final[Mapping[str, str]] = {
    "SHOP": "The vendor's drawing",
    "ARCH": "The architect's drawing",
}

_LAYER_WORD: Final[Mapping[str, Literal["company", "project", "run"]]] = {
    ParameterLayer.GLOBAL.value: "company",
    ParameterLayer.PROJECT.value: "project",
    ParameterLayer.RUN.value: "run",
}


def _sentence(name: str) -> str:
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _setting_guide(name: str) -> Guide:
    if name in SETTING_GUIDE:
        return SETTING_GUIDE[name]
    guidance = " ".join(SOURCE_GUIDANCE[source] for source in allowed_sources(name))
    return Guide(setting_name(name), guidance or "The project spec.")


def where_drawn(source: str, semantic_type: str) -> str:
    """Where a drawn number is read, in plain words: the view when it is known, else the drawing.

    `DRAWING_GUIDE` names the vendor's views (the client lead: plan for the cut-out and offsets,
    elevation for widths), so it is used only for the vendor's drawing. One sentence for both the
    Measure page and the waiting card (#1262), and never a claim that a drawing was not supplied.
    """
    if source == "SHOP" and semantic_type in DRAWING_GUIDE:
        return DRAWING_GUIDE[semantic_type].where_to_find
    drawing = DRAWN_SOURCES.get(source, "the drawing")
    return f"{drawing[:1].upper()}{drawing[1:]}."


def _drawing_guide(source: str, semantic_type: str) -> Guide:
    """A drawn dimension's plain name, never its code."""
    if semantic_type in DRAWING_GUIDE:
        return DRAWING_GUIDE[semantic_type]
    code = CLIENT_CODES.get(semantic_type)
    label = _sentence(code.description) if code is not None else "A dimension"
    side = _DRAWING_SIDE.get(source, "The drawing")
    return Guide(f"{label}, as drawn", f"{side}.")


def _text(row: StoredParameterValue) -> str:
    exact = row.exact_value
    return f'{format_inches(exact)}"' if row.unit == Unit.INCH.value else f"{exact} {row.unit}"


def _saved(row: StoredParameterValue, layer: str, *, setting: bool) -> SavedInputOut:
    source: str | None = None
    if setting:
        source = (
            row.provenance
            if row.source_reference is None
            else (f"{row.provenance}: {row.source_reference}")
        )
    return SavedInputOut(
        text=_text(row),
        set_by=row.set_by,
        set_at=row.set_at,
        source=source,
        layer=_LAYER_WORD[layer],
    )


def _latest_set_ids(
    session: Session, project_id: UUID, revision_ids: Sequence[UUID]
) -> tuple[UUID | None, UUID | None, dict[UUID, UUID]]:
    """The newest company, project and per-revision RUN settings versions a run would read now."""

    def newest(*conditions: object) -> UUID | None:
        return session.execute(
            select(StoredParameterSet.id)
            .where(*conditions)  # type: ignore[arg-type]
            .order_by(StoredParameterSet.version.desc())
            .limit(1)
        ).scalar_one_or_none()

    company = newest(
        StoredParameterSet.layer == ParameterLayer.GLOBAL.value,
        StoredParameterSet.project_id.is_(None),
    )
    project = newest(
        StoredParameterSet.layer == ParameterLayer.PROJECT.value,
        StoredParameterSet.project_id == project_id,
    )
    runs: dict[UUID, UUID] = {}
    for revision_id in revision_ids:
        run = newest(
            StoredParameterSet.layer == ParameterLayer.RUN.value,
            StoredParameterSet.package_revision_id == revision_id,
        )
        if run is not None:
            runs[revision_id] = run
    return company, project, runs


def missing_inputs_for(
    session: Session, project_id: UUID, items: Sequence[WaitingFinding]
) -> dict[UUID, list[MissingInputOut]]:
    """For each revision-wide `NOT_FOUND` result in `items`, the values it could not run without.

    A result that decided, or abstained for another reason, or belongs to one countertop row (those
    are asked on the countertop's own card), gets nothing. **One countertop's own depth or sink
    result** (`PER_COUNTERTOP_RULES`, the depth reader's scope, #1290) asks for its settings only:
    what was read for its drawn values is listed beside them, per countertop
    (`app/review/depth_readings.py`). A fixed handful of queries for the page.
    """
    waiting = [
        item
        for item in items
        if item.outcome is Outcome.NOT_FOUND
        and item.rule_id in WAITING_RULES
        and (item.scope_item_id is None or item.rule_id in PER_COUNTERTOP_RULES)
        and item.scope_row_candidate_id is None
    ]
    if not waiting:
        return {}

    # The rule as the run read it: its own published snapshot, never today's rulebook.
    # A snapshot that will not rebuild asks for nothing: the list of results must still load, and
    # that snapshot's own integrity failure is reported where it is run, not here.
    snapshots: dict[UUID, Rule] = {}
    for row in session.scalars(
        select(RuleSnapshot).where(RuleSnapshot.id.in_({item.rule_snapshot_id for item in waiting}))
    ):
        try:
            snapshots[row.id] = from_row(row).rule
        except (UnreadableSnapshot, SnapshotIntegrityError):
            continue

    # What the run had: its qualified operands, and the names in each settings version it cited.
    had_operands: dict[UUID, set[str]] = {}
    for check_run_id, name in session.execute(
        select(VerdictInput.check_run_id, VerdictInput.operand_name).where(
            VerdictInput.check_run_id.in_({item.check_run_id for item in waiting})
        )
    ):
        had_operands.setdefault(check_run_id, set()).add(name)
    cited_hashes = {
        str(set_id) for item in waiting for set_id in (item.parameter_set_versions or {}).values()
    }
    names_in_set: dict[str, set[str]] = {}
    for set_hash, name in session.execute(
        select(StoredParameterSet.set_id, StoredParameterValue.name)
        .join(StoredParameterValue, StoredParameterValue.parameter_set_id == StoredParameterSet.id)
        .where(StoredParameterSet.set_id.in_(cited_hashes))
    ):
        names_in_set.setdefault(set_hash, set()).add(name)

    # What is on record now, in the order a run resolves it: company, then project, then package.
    revision_ids = sorted({item.package_revision_id for item in waiting})
    company, project, runs = _latest_set_ids(session, project_id, revision_ids)
    current_ids = [set_id for set_id in (company, project, *runs.values()) if set_id is not None]
    rows_by_set: dict[UUID, dict[str, StoredParameterValue]] = {}
    for value in session.scalars(
        select(StoredParameterValue).where(StoredParameterValue.parameter_set_id.in_(current_ids))
    ):
        rows_by_set.setdefault(value.parameter_set_id, {})[value.name] = value

    def setting_now(name: str, revision_id: UUID) -> SavedInputOut | None:
        found: SavedInputOut | None = None
        for layer, set_id in (
            (ParameterLayer.GLOBAL.value, company),
            (ParameterLayer.PROJECT.value, project),
            (ParameterLayer.RUN.value, runs.get(revision_id)),
        ):
            row = rows_by_set.get(set_id, {}).get(name) if set_id is not None else None
            if row is not None:
                found = _saved(row, layer, setting=True)
        return found

    def reading_now(key: str, revision_id: UUID) -> SavedInputOut | None:
        set_id = runs.get(revision_id)
        row = rows_by_set.get(set_id, {}).get(key) if set_id is not None else None
        return None if row is None else _saved(row, ParameterLayer.RUN.value, setting=False)

    answers: dict[UUID, list[MissingInputOut]] = {}
    # A setting is for the job, not for one countertop: among one check's per-countertop results it
    # is asked once, on the first, as the check's one whole-package result asks it (#1296).
    asked_for_countertops: set[tuple[UUID, str, str]] = set()
    for item in waiting:
        rule = snapshots.get(item.rule_snapshot_id)
        if rule is None:  # pragma: no cover - the foreign key guarantees the snapshot
            continue
        cited = set().union(
            *(names_in_set.get(str(h), set()) for h in (item.parameter_set_versions or {}).values())
        )
        operands = had_operands.get(item.check_run_id, set())
        needs: list[MissingInputOut] = []
        # Settings by name, then drawn values by the client's code: one order on every screen.
        for name, declared in sorted(rule.parameters.items()):
            if declared.default is not None or name in cited or name in BLOCKED_PARAMETERS:
                continue
            if not allowed_sources(name):
                # No honest source is recorded for it, so the save path would refuse it: not asked.
                continue
            if item.scope_item_id is not None:
                key = (item.package_revision_id, item.rule_id, name)
                if key in asked_for_countertops:
                    continue
                asked_for_countertops.add(key)
            guide = _setting_guide(name)
            scope: Scope = "sink" if name in SINK_SETTINGS else "project"
            needs.append(
                MissingInputOut(
                    key=name,
                    label=guide.label,
                    unit="in",
                    where_to_find=guide.where_to_find,
                    scope=scope,
                    sources=(
                        [source.value for source in allowed_sources(name)]
                        if scope == "project"
                        else []
                    ),
                    askable=True,
                    saved=setting_now(name, item.package_revision_id),
                )
            )
        # **A drawn number is never a box** (GV's admin: people confirm, they do not fill in
        # measurements; Anant, 2026-10-12). It is listed, not askable, so the screen can say plainly
        # that the check waits for the drawing; a reader reading it, or a value already confirmed on
        # Measurements, is what supplies it. **A reviewer's entry** — a list of cabinets, each
        # cabinet's type, the site width (#1205) — is not a box here either: it is entered on
        # Measurements, where a run keeps its order, and the line says so.
        drawn: list[MissingInputOut] = []
        for input_name, selector in rule.inputs.items():
            if input_name in operands or item.scope_item_id is not None:
                continue
            source = getattr(selector.source, "value", str(selector.source))
            semantic = getattr(selector.semantic_type, "value", str(selector.semantic_type))
            is_list = selector.cardinality is not Cardinality.ONE
            entered = source == "USER_INPUT" or is_list
            guide = REVIEWER_GUIDE.get((source, semantic)) or _drawing_guide(source, semantic)
            where = guide.where_to_find
            if is_list:
                where = LIST_ORDER.get(rule.operation.type, LIST_ON_MEASUREMENTS)
            elif not entered:
                # The Measure page's own sentence (#1262), so both screens name the same view.
                where = where_drawn(source, semantic)
            drawn.append(
                MissingInputOut(
                    key=f"{source}:{semantic}",
                    label=guide.label,
                    unit=None if semantic in _NOT_A_LENGTH else "in",
                    where_to_find=where,
                    scope="measurements" if entered else "drawing",
                    askable=False,
                    # A drawn value typed before confirm-only no longer counts (#1303), so it is never
                    # "typed since": the check waits for a confirmed reading, not a new run.
                    saved=(
                        None
                        if is_list or drawn_input(selector)
                        else reading_now(f"{rule.id}:{input_name}", item.package_revision_id)
                    ),
                )
            )
        answers[item.id] = [*needs, *sorted(drawn, key=lambda need: need.key)]
    return answers
