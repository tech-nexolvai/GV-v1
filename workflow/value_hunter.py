"""Look for each setting the rules still need, and point at the passage that states it (#881).

#849 can point at the passage in a package that states a setting, and #866 lets a reviewer confirm
the number by typing it without being shown it. Nothing filed the pointers, so the form's "found on
page N" box appeared on no package. This files them, in the worker, once the phrase index is built,
and only where the deployment turns it on (`GV_VALUE_HUNTER`). It is off by default: step 5.3 of
#798 measures it before anyone switches it on.

**Built like the reading agent's guardrails** (`extraction/agent/`): a sealed toolbox, bounded steps
and typed endings, in one run per setting.

- **The plan is a code-owned table, not a model.** The settings are the rules' outstanding ones
  (`rules.overrides.override_report`): declared by a published rule with no default, and set by none
  of the layers stored for the package. Each is looked for only where a package could honestly
  supply it (`rules.parameter_sources.CITABLE_SIDES`), and only with the words written for it
  (`vocabulary/parameter_terms.py`). A company standard, the field cut, the fabricator's clearance
  and a setting with no words written for it are never searched for; each ends at once, saying why.
  A setting with a default is not outstanding, so it is never looked for at all.
- **Three tools, and no fourth.** Search the package's own words (`retrieval.package_text`), which
  answers with passage ids and never their text; propose one of those passages, through #849's guard
  (`workflow.parameter_proposals.propose_passage`), the only way a pointer is filed; and end the
  setting as not found. The toolbox searches only the setting's own words, proposes only a passage
  one of its searches returned in this run, and tries each passage once.
- **Bounded by what it was given.** A run may take one search per word, one proposal per passage
  those searches returned, and one ending. A planner that asks for more is stopped there, and the
  setting ends not found. No step costs money, because no model is called: step 5.4 of #798 adds a
  model planner, and a budget with it, only if step 5.3 shows a need.
- **Two endings, and neither carries a number.** `Proposal(setting, phrase_id)` or
  `NotFound(setting, reason)`. The guard reads the passage's number to check that it is one inch
  dimension and that no other passage states a different one, and then drops it
  (`workflow/parameter_citations.py`); a person confirms it by typing it blind (#866). The reasons
  are the code's own words and quote nothing from the drawing.

**It never writes a setting.** `gv-proposer-never-writes-a-setting` covers this file, and a test
keeps it from reaching `app.api`. A pointer is not a setting and never reaches a check: the number
reaches one only when a person types it and it matches the passage.

Source: issue #881, step 5.1 of the plan on #798.
Verification: `tests/workflow/test_value_hunter.py`,
`tests/workflow/test_proposer_never_writes_a_setting.py`.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision
from app.models.parameters import load_parameter_sets
from app.verdicts.rulebook import snapshot_store
from retrieval.package_text import PhraseHit, search_package_text
from rules.overrides import override_report
from rules.parameter_sources import allowed_sources, citable_sources
from vocabulary.parameter_terms import search_terms
from workflow.measurements import run_parameters_for
from workflow.parameter_proposals import ProposalOutcome, propose_passage

__all__ = [
    "PERMITTED_TOOLS",
    "VALUE_HUNTER_ENV",
    "Ending",
    "Hunt",
    "HuntProgress",
    "HunterToolbox",
    "NotFound",
    "NotFoundArguments",
    "PlannedSetting",
    "Planner",
    "Proposal",
    "ProposeArguments",
    "Refused",
    "SearchArguments",
    "ToolCall",
    "ToolName",
    "hunt_setting",
    "hunt_values",
    "outstanding_settings",
    "plan_hunt",
    "plan_setting",
    "table_planner",
    "value_hunter_enabled",
]

#: The switch. Off unless it says `1`, `true` or `yes`, as the reading agent's does.
VALUE_HUNTER_ENV: Final = "GV_VALUE_HUNTER"


def value_hunter_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the deployment turned the hunter on. Unset, or anything else, is off."""
    values = os.environ if environ is None else environ
    return values.get(VALUE_HUNTER_ENV, "").strip().lower() in {"1", "true", "yes"}


def _require_text(value: object, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedSetting:
    """One outstanding setting and how it is looked for: its search words, or why it is not."""

    setting: str
    terms: tuple[str, ...]
    not_searched: str | None
    """Why the setting is not looked for, or `None` when it is. Set exactly when `terms` is empty."""

    def __post_init__(self) -> None:
        _require_text(self.setting, field="setting")
        if not isinstance(self.terms, tuple) or not all(
            isinstance(term, str) and term.strip() for term in self.terms
        ):
            raise TypeError("terms must be a tuple of non-empty strings")
        if (self.not_searched is None) == (not self.terms):
            raise ValueError(
                "a planned setting has either search words or a reason it is not searched, not both"
            )
        if self.not_searched is not None:
            _require_text(self.not_searched, field="not_searched")


def plan_setting(setting: str) -> PlannedSetting:
    """How `setting` is looked for, read from the code's own tables, or why it is not."""
    sources = allowed_sources(setting)
    if not sources:
        return PlannedSetting(
            setting,
            (),
            f"no source is assigned to {setting} (rules/parameter_sources.py), so where it may come "
            "from is unknown",
        )
    citable = citable_sources(setting)
    if not citable:
        named = " or ".join(source.value for source in sources)
        return PlannedSetting(
            setting, (), f"{setting} comes from {named}, which is never read off a package"
        )
    if len(citable) > 1:
        return PlannedSetting(
            setting,
            (),
            f"{setting} may be cited as more than one source, and a passage cannot say which",
        )
    terms = search_terms(setting)
    if not terms:
        return PlannedSetting(
            setting,
            (),
            f"no search words are written for {setting} (vocabulary/parameter_terms.py)",
        )
    return PlannedSetting(setting, terms, None)


def outstanding_settings(session: Session, package_revision_id: UUID) -> tuple[str, ...]:
    """The settings the published rules need and nobody has supplied for this revision, sorted.

    Declared by a published rule with no default, and set by none of the layers stored for it: GV's
    company standards, the project's settings, and this review's own, which are the layers the checks
    resolve from (`workflow.stages`). A rule's declared default is a real answer its author wrote
    down, so a setting that has one is never outstanding.
    """
    revision = session.get(PackageRevision, package_revision_id)
    if revision is None:
        raise ValueError(f"no package revision {package_revision_id}")
    package = session.get(Package, revision.package_id)
    if package is None:
        raise ValueError(f"package revision {package_revision_id} names no package")
    store = snapshot_store(session)
    rules = [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]
    layers = list(load_parameter_sets(session, package.project_id))
    run = run_parameters_for(session, package_revision_id)
    if run is not None:
        layers.append(run)
    return override_report(rules, *layers).outstanding


def plan_hunt(session: Session, package_revision_id: UUID) -> tuple[PlannedSetting, ...]:
    """Every outstanding setting of this revision, each with how it is looked for, sorted by name."""
    return tuple(
        plan_setting(setting) for setting in outstanding_settings(session, package_revision_id)
    )


# ---------------------------------------------------------------------------
# The toolbox
# ---------------------------------------------------------------------------


class ToolName(StrEnum):
    """The complete set of the hunter's capabilities."""

    SEARCH_PACKAGE_TEXT = "search_package_text"
    PROPOSE = "propose"
    NOT_FOUND = "not_found"


PERMITTED_TOOLS: Final = frozenset(ToolName)


@dataclass(frozen=True, slots=True)
class SearchArguments:
    """Search the package's own words for one of the setting's search words."""

    query: str

    def __post_init__(self) -> None:
        _require_text(self.query, field="query")


@dataclass(frozen=True, slots=True)
class ProposeArguments:
    """Propose one passage a search in this run returned."""

    phrase_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.phrase_id, UUID):
            raise TypeError("phrase_id must be a UUID")


@dataclass(frozen=True, slots=True)
class NotFoundArguments:
    """End the setting with no pointer, and say why."""

    reason: str

    def __post_init__(self) -> None:
        _require_text(self.reason, field="reason")


type ToolArguments = SearchArguments | ProposeArguments | NotFoundArguments


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One typed request from the fixed tool surface."""

    arguments: ToolArguments

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, (SearchArguments, ProposeArguments, NotFoundArguments)):
            raise TypeError("arguments must be one of the permitted tool argument types")

    @property
    def name(self) -> ToolName:
        """The tool, from the argument type and never from any text."""
        if isinstance(self.arguments, SearchArguments):
            return ToolName.SEARCH_PACKAGE_TEXT
        if isinstance(self.arguments, ProposeArguments):
            return ToolName.PROPOSE
        return ToolName.NOT_FOUND


@dataclass(frozen=True, slots=True)
class Proposal:
    """A pointer was filed: the setting, and the passage that states it. No number, by design."""

    setting: str
    phrase_id: UUID

    def __post_init__(self) -> None:
        _require_text(self.setting, field="setting")
        if not isinstance(self.phrase_id, UUID):
            raise TypeError("phrase_id must be a UUID")


@dataclass(frozen=True, slots=True)
class NotFound:
    """No pointer was filed, and why, in words that quote nothing from the drawing."""

    setting: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.setting, field="setting")
        _require_text(self.reason, field="reason")


type Ending = Proposal | NotFound


@dataclass(frozen=True, slots=True)
class Refused:
    """A call that did not end the run: a passage that may not be cited, or a call not carried out."""

    reason: str
    disagreement: bool = False
    """The package's passages state the setting with different values, so no passage of it can be
    proposed, and a reviewer decides."""

    def __post_init__(self) -> None:
        _require_text(self.reason, field="reason")


type ToolResult = tuple[PhraseHit, ...] | Refused | Ending


class HunterToolbox:
    """The three tools, for one setting in one package revision, fixed at construction.

    The tools are this class's own code: there is no `register`, no handler to pass in and none to
    swap, so a caller cannot add a fourth. Every call is recorded, in order, before it runs. What the
    searches returned is remembered, so a proposal of any other passage is refused, and so is a second
    search for the same words or a second proposal of the same passage.
    """

    __slots__ = (
        "_calls",
        "_found",
        "_package_revision_id",
        "_refusals",
        "_sealed",
        "_searched",
        "_session",
        "_setting",
        "_terms",
        "_tried",
    )

    _session: Session
    _package_revision_id: UUID
    _setting: str
    _terms: tuple[str, ...]
    _calls: list[ToolCall]
    _searched: list[str]
    _found: dict[UUID, None]
    _tried: list[UUID]
    _refusals: list[Refused]
    _sealed: bool

    def __init__(self, session: Session, package_revision_id: UUID, setting: str) -> None:
        if not isinstance(package_revision_id, UUID):
            raise TypeError("package_revision_id must be a UUID")
        _require_text(setting, field="setting")
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_package_revision_id", package_revision_id)
        object.__setattr__(self, "_setting", setting)
        # The code's table, not the plan's: a plan handed other words still searches only these.
        object.__setattr__(self, "_terms", search_terms(setting))
        object.__setattr__(self, "_calls", [])
        object.__setattr__(self, "_searched", [])
        # A dict, for its order: the passages in the order the searches first returned them.
        object.__setattr__(self, "_found", {})
        object.__setattr__(self, "_tried", [])
        object.__setattr__(self, "_refusals", [])
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("HunterToolbox is fixed at construction")
        object.__setattr__(self, name, value)

    @property
    def permitted_tools(self) -> frozenset[ToolName]:
        """The immutable, code-owned capability set."""
        return PERMITTED_TOOLS

    @property
    def calls(self) -> tuple[ToolCall, ...]:
        """Every call made, in order, whatever it returned."""
        return tuple(self._calls)

    @property
    def searched(self) -> tuple[str, ...]:
        """The search words searched in this run, in order."""
        return tuple(self._searched)

    @property
    def found(self) -> tuple[UUID, ...]:
        """The passages this run's searches returned, in the order first returned, each once."""
        return tuple(self._found)

    @property
    def tried(self) -> tuple[UUID, ...]:
        """The passages proposed through the guard in this run, in order."""
        return tuple(self._tried)

    @property
    def refusals(self) -> tuple[Refused, ...]:
        """Why each passage proposed through the guard filed nothing, in order."""
        return tuple(self._refusals)

    def invoke(self, call: ToolCall) -> ToolResult:
        """Record one call from the fixed set, then carry it out."""
        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        self._calls.append(call)
        arguments = call.arguments
        if isinstance(arguments, SearchArguments):
            return self._search(arguments.query)
        if isinstance(arguments, ProposeArguments):
            return self._propose(arguments.phrase_id)
        return NotFound(self._setting, arguments.reason)

    def _search(self, query: str) -> tuple[PhraseHit, ...] | Refused:
        # Not quoted back: the words are the planner's, and a refusal speaks only in the code's own.
        if query not in self._terms:
            return Refused(f"the query is not one of the search words written for {self._setting}")
        if query in self._searched:
            return Refused("these search words were already searched in this run")
        self._searched.append(query)
        hits = search_package_text(self._session, self._package_revision_id, query)
        for hit in hits:
            self._found.setdefault(hit.phrase_id, None)
        return hits

    def _propose(self, phrase_id: UUID) -> Proposal | Refused:
        if phrase_id not in self._found:
            return Refused("no search in this run returned this passage, so it may not be proposed")
        if phrase_id in self._tried:
            return Refused("this passage was already proposed in this run")
        self._tried.append(phrase_id)
        result = propose_passage(self._session, self._package_revision_id, self._setting, phrase_id)
        if result.outcome is ProposalOutcome.PROPOSED:
            return Proposal(self._setting, phrase_id)
        refused = Refused(result.note, disagreement=result.outcome is ProposalOutcome.REVIEW)
        self._refusals.append(refused)
        return refused


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HuntProgress:
    """Everything done so far for one setting: what a planner decides the next step from."""

    planned: PlannedSetting
    steps: int
    searched: tuple[str, ...]
    found: tuple[UUID, ...]
    """The passages the searches returned, in the order first returned. Ids only."""

    tried: tuple[UUID, ...]
    refusals: tuple[Refused, ...]
    """Why each passage tried filed nothing, in order."""


type Planner = Callable[[HuntProgress], ToolCall]


def _nothing_cited(planned: PlannedSetting, progress: HuntProgress) -> str:
    """Why no passage was proposed: none was found, or each found was refused, counted by reason."""
    words = ", ".join(planned.terms)
    if not progress.found:
        return f"no passage in this package's own words mentions {words}"
    counts = Counter(refusal.reason for refusal in progress.refusals)
    reasons = "; ".join(
        f"{count} × {reason}"
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    return f"{len(progress.found)} passage(s) mention {words}, and none may be cited: {reasons}"


def table_planner(progress: HuntProgress) -> ToolCall:
    """The production plan: a table read top to bottom, where the first row that applies decides.

    | Done so far | Next step |
    |---|---|
    | the setting is not looked for | not found, with the plan's reason |
    | a search word not yet searched | search it, in the vocabulary's order |
    | a proposal found the passages disagree | not found: a reviewer decides |
    | a passage found and not yet tried | propose it, in the order found |
    | nothing left | not found, saying what was found and why each was refused |
    """
    planned = progress.planned
    if planned.not_searched is not None:
        return ToolCall(NotFoundArguments(planned.not_searched))
    for term in planned.terms:
        if term not in progress.searched:
            return ToolCall(SearchArguments(term))
    if progress.refusals and progress.refusals[-1].disagreement:
        return ToolCall(NotFoundArguments(progress.refusals[-1].reason))
    for phrase_id in progress.found:
        if phrase_id not in progress.tried:
            return ToolCall(ProposeArguments(phrase_id))
    return ToolCall(NotFoundArguments(_nothing_cited(planned, progress)))


def _end(toolbox: HunterToolbox, reason: str) -> NotFound:
    """End the run through the not-found tool, so the ending is recorded like every other call."""
    ending = toolbox.invoke(ToolCall(NotFoundArguments(reason)))
    if not isinstance(ending, NotFound):  # pragma: no cover - the tool returns nothing else
        raise TypeError("the not_found tool returned something other than NotFound")
    return ending


def hunt_setting(
    session: Session,
    package_revision_id: UUID,
    planned: PlannedSetting,
    planner: Planner = table_planner,
) -> Ending:
    """Look for one planned setting, step by step, and end in exactly one `Proposal` or `NotFound`.

    The planner names each step and the toolbox carries it out. A run may take one search per search
    word, one proposal per passage its searches returned, and one ending; a planner that asks for
    more, fails, or names no tool call ends the run as not found. A database error is not caught: the
    session can no longer be trusted after one, and the worker takes back the whole hunt.
    """
    if not isinstance(planned, PlannedSetting):
        raise TypeError("planned must be a PlannedSetting")
    toolbox = HunterToolbox(session, package_revision_id, planned.setting)
    # The words the toolbox will search, from the code's table: a plan listing more gets no more.
    words = len(search_terms(planned.setting))
    steps = 0
    while True:
        # Grows only as the searches find passages, and the searches are bounded by the words.
        if steps >= words + len(toolbox.found) + 1:
            return _end(
                toolbox,
                f"the hunt for {planned.setting} asked for more than one search per word and one "
                "proposal per passage found, and was stopped",
            )
        progress = HuntProgress(
            planned=planned,
            steps=steps,
            searched=toolbox.searched,
            found=toolbox.found,
            tried=toolbox.tried,
            refusals=toolbox.refusals,
        )
        try:
            call = planner(progress)
        # A planner that fails ends the setting as not found, as a failed planner ends the reading
        # agent's region in an abstention: a third kind of ending would be one nobody handles.
        except Exception as error:  # noqa: BLE001
            return _end(toolbox, f"the plan for {planned.setting} failed: {type(error).__name__}")
        if not isinstance(call, ToolCall):
            return _end(toolbox, f"the plan for {planned.setting} named no tool call")
        result = toolbox.invoke(call)
        steps += 1
        if isinstance(result, (Proposal, NotFound)):
            return result


@dataclass(frozen=True, slots=True)
class Hunt:
    """Every outstanding setting's ending, in the plan's order."""

    endings: tuple[Ending, ...]

    def summary(self) -> dict[str, object]:
        """For the worker's log: the settings proposed, and why each other one was not found.

        No passage's words and no number, because no ending holds either.
        """
        return {
            "ran": True,
            "outstanding": len(self.endings),
            "proposed": [ending.setting for ending in self.endings if isinstance(ending, Proposal)],
            "not_found": {
                ending.setting: ending.reason
                for ending in self.endings
                if isinstance(ending, NotFound)
            },
        }


def hunt_values(session: Session, package_revision_id: UUID) -> Hunt:
    """Look for every outstanding setting of this revision, one bounded run each, with the table.

    Each pointer it files is the guard's (`propose_passage`). It writes nothing else.
    """
    return Hunt(
        tuple(
            hunt_setting(session, package_revision_id, planned)
            for planned in plan_hunt(session, package_revision_id)
        )
    )
