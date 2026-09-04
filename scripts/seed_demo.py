"""Seed one package whose dimensions a reviewer supplied, and check it for real.

**What this demonstrates, and what it does not.** The product's claim is *"the AI reads, exact
arithmetic decides, a reviewer signs off"*. The deciding half is built and tested; the reading half is
blocked on drawings (#274) and on semantic typing (Q20). This puts a person in the reading seat —
which `CLIENT_FACTS` Q7 already blesses: *"the reviewer types the values into input fields for that
drawing set"* — so the arithmetic runs against real numbers and produces real verdicts.

So: no extraction, no model, no drawing. Every number below is one somebody typed, and the findings
that come out are the engine's own, computed exactly.

**Every value here is illustrative and none is a default.** The parameters land in a *project* layer,
which is the sanctioned place for a per-project reviewer answer — not the company layer, and not a
rulebook default. `CLIENT_FACTS` Q2 has no tolerance from the client, Q21's filler bounds are
unsettled, and `CT-BACK-OFFSET-MIN-001` says in its own text that its minimum has no default because
the vendor value is pending. Nothing in this file may be read as a client value.

    python scripts/seed_demo.py                 # a package that passes
    python scripts/seed_demo.py --fail          # the same package with one dimension wrong

Prints the package id and every finding. Publish the rulebook first if the database has none:
`python scripts/run_checks.py <revision> --publish`.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import UTC, datetime
from fractions import Fraction
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

#: The reviewer's own measurements, in inches, for one countertop assembly.
#:
#: `CT-DEPTH-001` computes `cabinet_depth + countertop_overhang` and compares it with the authored
#: shop depth, exactly — Q2 gives no tolerance, so the check is `equals` and 1/4" out is a FAIL.
CABINET_DEPTH = Fraction(24)
COUNTERTOP_OVERHANG = Fraction(3, 2)

#: What the reviewer read off the shop drawing. 25 1/2" agrees with 24 + 1 1/2 exactly.
SHOP_DEPTH_PASS = Fraction(51, 2)

#: A quarter inch out. Chosen because it is the shape of error this system exists to catch: a
#: plausible number, correctly read, that does not add up.
SHOP_DEPTH_FAIL = Fraction(101, 4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail",
        action="store_true",
        help="supply a shop depth that is a quarter inch out, so the check FAILs",
    )
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.config import Settings
    from app.models import Package, PackageRevision, PackageState, Project
    from app.models.parameters import to_rows
    from rules.parameters import (
        ParameterLayer,
        ParameterSet,
        ParameterValue,
        Provenance,
    )
    from rules.schema import Quantity
    from units.measurement import Measurement, Unit
    from verdict.operands import EvidenceStatus, VerdictOperand
    from workflow.stages import DatabaseStages

    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings.database_url)
    now = datetime.now(UTC)
    shop_depth = SHOP_DEPTH_FAIL if args.fail else SHOP_DEPTH_PASS

    with Session(engine) as session:
        project = Project(name="Reviewer-supplied demo")
        session.add(project)
        session.flush()
        package = Package(project_id=project.id, vendor="Apex Glass & Stone")
        session.add(package)
        session.flush()
        revision = PackageRevision(
            package_id=package.id,
            revision_number=1,
            state=PackageState.RUNNING_CHECKS,
        )
        session.add(revision)
        session.flush()

        # The project's own settings, as a reviewer would enter them for this job. `Provenance.SPECIFIED`
        # says a person supplied it: `rules/parameters.py` refuses any provenance a model could claim.
        parameters = ParameterSet(
            project_id=str(project.id),
            layer=ParameterLayer.PROJECT,
            version=1,
            parameters={
                "cabinet_depth": ParameterValue(
                    value=Quantity(value=CABINET_DEPTH, unit=Unit.INCH),
                    # MEASURED: a person measured or read it. `HUMAN_PROVENANCES` is
                    # `{MEASURED, GC_CLIENT}` and no member exists that a model could claim — the
                    # vocabulary is what keeps a model's number out, not a check somewhere.
                    provenance=Provenance.MEASURED,
                    set_by="demo reviewer",
                    set_at=now,
                ),
                "countertop_overhang": ParameterValue(
                    value=Quantity(value=COUNTERTOP_OVERHANG, unit=Unit.INCH),
                    provenance=Provenance.MEASURED,
                    set_by="demo reviewer",
                    set_at=now,
                ),
            },
        )
        stored, values = to_rows(parameters)
        session.add(stored)
        for value in values:
            session.add(value)
        session.flush()

        # The reading, by a person. HUMAN_CONFIRMED is one of the two statuses the evidence gate
        # accepts, and it is the honest one here: a reviewer read this off the drawing, not the system.
        operands = {
            "CT-DEPTH-001": {
                "countertop_depth": VerdictOperand(
                    name="countertop_depth",
                    value=Measurement(shop_depth, Unit.INCH, str(shop_depth)),
                    status=EvidenceStatus.HUMAN_CONFIRMED,
                    source="SHOP",
                    evidence_ref="reviewer:typed",
                )
            }
        }

        result = DatabaseStages(operands=operands).run_checks(session, revision.id)
        session.commit()

        print(f"package  {package.id}")
        print(f"revision {revision.id}")
        print(
            f"reviewer typed shop depth: {shop_depth} in   (expects {CABINET_DEPTH} + "
            f"{COUNTERTOP_OVERHANG} = {CABINET_DEPTH + COUNTERTOP_OVERHANG})"
        )
        print(f"checks: {dict(result)}")
        print()
        _print_findings(session, revision.id)
    return 0


def _print_findings(session: Session, revision_id: UUID) -> None:
    """Every finding, read back out of the database rather than off this script's hopes.

    The join is on `RuleSnapshot.id`, not `snapshot_id`: `CheckRun.rule_snapshot_id` is the row's
    UUID while `snapshot_id` is the content hash as text. Comparing them asks PostgreSQL for
    `varchar = uuid` and it says so, which is how this was caught.
    """
    from sqlalchemy import select

    from app.models import CheckRun, Finding, RuleSnapshot
    from app.verdicts.rulebook import from_row

    rule_of_run: dict[UUID, str] = {}
    rows = session.execute(
        select(CheckRun, RuleSnapshot)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(CheckRun.package_revision_id == revision_id)
    ).all()
    for run, snapshot in rows:
        rule_of_run[run.id] = from_row(snapshot).rule.id

    for finding in session.execute(
        select(Finding).where(Finding.package_revision_id == revision_id)
    ).scalars():
        rule_id = rule_of_run.get(finding.check_run_id, "?")
        trace = finding.trace or {}
        why = trace.get("comparison") or trace.get("reason") or ""
        print(f"  {finding.outcome:16} {rule_id:26} {str(why)[:76]}")


if __name__ == "__main__":
    raise SystemExit(main())
