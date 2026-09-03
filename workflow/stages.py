"""The first real pipeline stage: running the rules and writing what they decided.

Until now the only implementation of `Stages` was `NoStages`, which answers `{"implemented": False}`
for every stage and is deliberately loud about it — a default returning `{}` would let a package walk
the whole pipeline and arrive at review looking processed. This replaces one of those six answers with
work, and leaves the other five saying exactly what they said before.

**Why `run_checks` first, when extraction is not built.** The engine has been finished and tested for
some time and has never had a caller: no code in the repository writes a `CheckRun` or a `Finding`, so
every finding anyone has seen was inserted by hand. Wiring the caller proves the spine — applicable
rules selected, parameters resolved, `execute()` run, rows written, findings visible to a reviewer —
and makes extraction a matter of supplying operands to something that already works, rather than
another layer with nothing downstream of it.

**Every finding will be `NOT_FOUND`, and that is the honest result.** There are no observations, so
there are no operands, so no check can decide anything. The alternative — waiting until extraction
exists — leaves the engine uncalled and the `CHECKS_HAVE_RUN` entry condition unsatisfiable, so no
package can legally reach `AWAITING_REVIEW` at all.

**Why here and not in `app/`.** The `Stages` protocol is handed a SQLAlchemy `Session`, and
`sqlalchemy` is in the banned set for `verdict/` and `rules/` — those packages could not implement
this protocol if they wanted to. `workflow/` is the sanctioned bridge: it already imports `app` models
and the domain layers side by side (`workflow/retry.py`), which is exactly what a stage has to do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.package import Package, PackageRevision
from app.models.parameters import declared_defaults, load_parameter_sets
from app.verdicts.record import record_finding, supersede_runs
from app.verdicts.rulebook import snapshot_store
from rules.applicability import Abstention, CheckContext, resolve
from rules.parameters import ParameterSet, resolve_all
from rules.project import ProjectScope
from rules.semantic_types import ProductType
from rules.snapshot import RuleSnapshot
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operations import register_all
from workflow.review import ENGINE_VERSION, PageResult

__all__ = ["DatabaseStages"]


class DatabaseStages:
    """The pipeline as far as it is built: checks run, everything else still says it did not.

    Deliberately not a subclass of `NoStages` and deliberately without a `__getattr__`. A catch-all
    once made `join_pages` count a phantom page, because `extract_pages` returned a mapping that a
    fall-through produced — so every unimplemented stage is written out, and adding a seventh stage to
    the protocol will fail loudly here instead of being silently answered.
    """

    def _not_built(self, stage: str) -> Mapping[str, object]:
        """The same answer `NoStages` gives, for the stages that are still not built.

        Repeated rather than delegated so the two cannot drift: a reader comparing this class with the
        protocol sees six methods and can tell at a glance which one does work.
        """
        return {"implemented": False, "stage": stage}

    def ingest(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._not_built("ingest")

    def extract_pages(self, session: Session, package_revision_id: UUID) -> Sequence[PageResult]:
        del session, package_revision_id
        return ()

    def match(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._not_built("match")

    def validate_evidence(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]:
        del session, package_revision_id
        return self._not_built("validate_evidence")

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._not_built("generate_outputs")

    def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Run every applicable rule against this revision and record what each decided.

        **`register_all()` first, every time.** The operation registry is global and empty until
        somebody fills it; today the only thing that does is importing `app/api/operations.py`, which
        a worker process never touches. Without this call every operation lookup fails and the engine
        converts the failure into `REVIEW_REQUIRED` — so the whole package would abstain, plausibly,
        for a reason that appears nowhere. It is idempotent.

        **Previous runs are superseded before new ones are written**, inside this transaction, so no
        reader ever sees two sets of findings for one revision.
        """
        register_all()

        revision = session.get(PackageRevision, package_revision_id)
        if revision is None:
            return {"implemented": True, "ran": False, "reason": "no such package revision"}

        package = session.get(Package, revision.package_id)
        if package is None:
            return {"implemented": True, "ran": False, "reason": "no such package"}

        store = snapshot_store(session)
        if not store.rule_ids():
            # Not a failure. Nothing is published, so there is nothing to check — and saying so is
            # different from running zero rules and reporting success.
            return {
                "implemented": True,
                "ran": False,
                "reason": "no rules are published; nothing to check",
            }

        stored_layers = load_parameter_sets(session, package.project_id)
        rules = [store.latest(rule_id) for rule_id in store.rule_ids()]
        defaults = declared_defaults(
            [snapshot.rule for snapshot in rules if snapshot is not None], when=utc_now()
        )
        layers = _layered(defaults, stored_layers)
        resolved = resolve_all(*layers)

        # `ProjectScope` wants the pinned project layer. Where a project has set nothing, the
        # rulebook's own defaults stand in — they are a real published answer, not a fabricated one.
        project_layer = next(
            (layer for layer in layers if layer.project_id == str(package.project_id)),
            None,
        )
        scope = ProjectScope(
            project_id=str(package.project_id),
            parameter_set=project_layer if project_layer is not None else _empty_project(package),
        )

        # **Every product type, not one.** The resolver keys candidates on an exact product-type
        # match, so asking about countertops alone would leave the cabinet rules unrun — and unrun is
        # indistinguishable from passing once the reviewer is looking at the list. A package carries
        # no product type today (there is no column for it), and guessing one from the vendor or the
        # filename would decide which checks apply by inference. Running the whole rulebook is the
        # honest reading until a package can say what is in it: a cabinet rule against a countertop
        # package abstains, which is visible, where omitting it is not.
        #
        # No discriminator can be established without extraction, so a rule that declares one
        # abstains rather than being resolved to a variant nobody read off a drawing.
        superseded = supersede_runs(session, package_revision_id)

        written = 0
        skipped = 0
        for product_type in ProductType:
            resolution = resolve(
                store,
                CheckContext(product_type=product_type, project=scope, discriminators={}),
            )
            # **A rule that could not even be attempted becomes a finding too.**
            # The resolver abstains when it cannot establish which variant applies — today that is
            # every rule with a discriminator, because nothing reads `wall_config` off a drawing. If
            # those were only counted, the reviewer would see the checks that ran and have no way to
            # learn that two more never started. Unrun and passed are indistinguishable on a list,
            # which is the failure this whole system is built to prevent, so they are recorded with
            # the resolver's own reason.
            for abstention in resolution.abstentions:
                if abstention.rule_id is None:
                    # Nothing to attribute it to, so nothing to write. Counted instead, and returned,
                    # rather than attached to an arbitrary rule.
                    skipped += 1
                    continue
                snapshot = store.latest(abstention.rule_id)
                if snapshot is None:
                    skipped += 1
                    continue
                record_finding(
                    session,
                    package_revision_id=package_revision_id,
                    finding=_unresolved(snapshot, abstention),
                    operands={},
                    parameter_set_ids={layer.layer.value: layer.set_id for layer in layers},
                )
                written += 1

            for applicable in resolution.applicable:
                finding = execute(applicable.snapshot, {}, resolved, discriminators={})
                record_finding(
                    session,
                    package_revision_id=package_revision_id,
                    finding=finding,
                    operands={},
                    parameter_set_ids={layer.layer.value: layer.set_id for layer in layers},
                    missing=_declared_inputs(applicable.snapshot.rule),
                )
                written += 1

        return {
            "implemented": True,
            "ran": True,
            "findings": written,
            "rules_published": len(store.rule_ids()),
            "superseded_runs": superseded,
            "not_applicable": skipped,
        }


def _unresolved(snapshot: RuleSnapshot, abstention: Abstention) -> Finding:
    """A rule the resolver could not even attempt, as the finding a reviewer will read.

    Built here rather than by the engine because the engine was never reached: applicability is
    decided before any arithmetic, and a rule whose variant is unknown has no operands to trace. The
    severity comes from the rule itself, so an unattempted critical check is still critical.
    """
    return Finding(
        rule_id=snapshot.rule.id,
        outcome=abstention.outcome,
        severity=snapshot.rule.severity,
        reason=abstention.reason,
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
    )


def _layered(defaults: ParameterSet, stored: Sequence[ParameterSet]) -> tuple[ParameterSet, ...]:
    """The rulebook defaults beneath whatever the database supplies.

    `rules.parameters.resolve` refuses two sets in one layer, and the defaults are GLOBAL — so a
    stored global set replaces them wholesale rather than merging. That is the correct reading: a
    company standard that has been recorded is the standard, and a rule author's default is what
    applies until somebody records one.
    """
    stored_layers = {layer.layer for layer in stored}
    if defaults.layer in stored_layers:
        return tuple(stored)
    return (defaults, *stored)


def _empty_project(package: Package) -> ParameterSet:
    """A project layer with nothing in it, for a project that has configured nothing.

    `ProjectScope` requires a pinned set and cannot take `None`. An empty one is truthful — this
    project has set no overrides — and resolution then falls through to the layers beneath it.
    """
    from rules.parameters import ParameterLayer

    return ParameterSet(
        project_id=str(package.project_id),
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={},
    )


def _declared_inputs(rule: object) -> dict[str, str]:
    """The operands a rule needs, so an abstention can name what was not read.

    Reported from the rule rather than from the empty operand mapping, because "nothing was supplied"
    is not useful and "no dimension was read for cutout_width (SHOP)" sends somebody to the drawing.
    """
    inputs = getattr(rule, "inputs", {})
    return {name: getattr(selector, "source", "?") for name, selector in inputs.items()}
