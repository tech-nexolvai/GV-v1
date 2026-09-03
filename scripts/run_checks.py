"""Run the rule checks against one package revision, on a laptop.

**Why this exists rather than a workflow trigger.** Nothing starts the review workflow end to end
today: `"ingest_document_version"` is enqueued when a document is confirmed but no workflow of that
name is registered, and the one that *is* registered is only enqueued by `supersede()`, which has no
caller. Building that chain is a separate piece of work, and until it exists the checks stage would
be finished code that nobody could run.

So this calls the stage directly. It is deliberately not a route: `app/api/` is tested never to do
heavy work, and running the rulebook against a package is exactly that.

**It publishes the rulebook if the database has none.** Authoring rules and publishing them is D6's
job and goes through approval and a gold-set regression; on a laptop there is no D6, and a store with
nothing in it makes the stage answer "no rules are published" forever. The `--publish` flag loads the
authored YAML the same way the tests do, so what runs is the real rulebook rather than a fixture.

Usage:

    python scripts/run_checks.py <package-revision-uuid> [--publish]
    python scripts/run_checks.py --package <package-uuid> [--publish]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from uuid import UUID

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

RULEBOOK = pathlib.Path(__file__).resolve().parent.parent / "rules" / "rulebook"


def _publish_rulebook(session: object) -> int:
    """Publish every authored rule that is not already published.

    Skips rules already present so the script can be run twice without minting a second definition
    for the same rule id, which the unique index would refuse.
    """
    import yaml
    from sqlalchemy import select

    from app.models import RuleDefinition, RuleSnapshot
    from rules.schema import Rule
    from rules.snapshot import publish

    existing = {
        rule_id for rule_id in session.execute(select(RuleDefinition.rule_id)).scalars()  # type: ignore[attr-defined]
    }
    published = 0
    for path in sorted(RULEBOOK.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if rule.id in existing:
            continue
        snapshot = publish(rule)
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)  # type: ignore[attr-defined]
        session.flush()  # type: ignore[attr-defined]
        session.add(  # type: ignore[attr-defined]
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
        published += 1
    return published


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="?", help="the package revision to check")
    parser.add_argument("--package", help="a package id; its current revision is used")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="publish the authored rulebook first, if it is not already published",
    )
    args = parser.parse_args()

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.config import Settings
    from app.models import PackageRevision
    from workflow.stages import DatabaseStages

    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings.database_url)

    with Session(engine) as session:
        if args.package:
            # The current revision is the highest-numbered one, which is how `app/api/packages.py`
            # resolves it. Derived rather than read off a column, because there is no column — a
            # package's "current" revision is a fact about the set, not a pointer it holds.
            revision = session.execute(
                select(PackageRevision)
                .where(PackageRevision.package_id == UUID(args.package))
                .order_by(PackageRevision.revision_number.desc())
                .limit(1)
            ).scalar_one_or_none()
            if revision is None:
                print(f"no revision for package {args.package}", file=sys.stderr)
                return 2
            revision_id = revision.id
        elif args.revision:
            revision_id = UUID(args.revision)
        else:
            parser.error("give a package revision id, or --package")

        if args.publish:
            count = _publish_rulebook(session)
            print(f"published {count} rule(s)")

        result = DatabaseStages().run_checks(session, revision_id)
        session.commit()

    print(json.dumps(dict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
