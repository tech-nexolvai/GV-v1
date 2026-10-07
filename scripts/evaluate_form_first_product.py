"""Read-only, counts-only replay of a private form key against a saved slot-reader run.

Example: DATABASE_URL=... python scripts/evaluate_form_first_product.py \
  --revision <uuid> --key data/goldset/.../form_key.json --truth-walls data/.../walls.json

The wall-truth file maps one-based page numbers to published wall_config names, as confirmed
by a person. No wall value is inferred from a model proposal. No raw answers or key values
are printed, and the database transaction is explicitly READ ONLY.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.form_first_product import audit_revision_attempts, evaluate_keyed_product_run
from eval.form_first_safety import release_gate_record
from eval.release_gates import GOLD_REGRESSION_GATE, run_gates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", type=UUID, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--truth-walls", type=Path, required=True)
    parser.add_argument("--gold-set-version", required=True)
    arguments = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required; use a private scratch database")
    raw_walls = json.loads(arguments.truth_walls.read_text(encoding="utf-8"))
    if not isinstance(raw_walls, dict):
        parser.error("truth-walls must be a JSON object keyed by one-based page number")
    truth_walls = {int(page): str(layout) for page, layout in raw_walls.items()}
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            with Session(bind=connection) as session:
                report, audits = evaluate_keyed_product_run(
                    session,
                    package_revision_id=arguments.revision,
                    key_path=arguments.key,
                    truth_wall_layouts=truth_walls,
                )
                run_audit = audit_revision_attempts(session, arguments.revision)
                gates = run_gates(
                    release_gate_record(report, gold_set_version=arguments.gold_set_version)
                )
                regression = next(
                    result for result in gates.results if result.gate_id == GOLD_REGRESSION_GATE
                )
                summary = {
                    "reader_path": report.reader_path,
                    "cases": len(report.scores),
                    "measured": report.measured,
                    "unaccounted": report.unaccounted,
                    "false_passes": report.false_passes,
                    "false_negatives": report.false_negatives,
                    "zero_false_pass": report.zero_false_pass,
                    "stored_label_attempts": sum(audit.label_attempts for audit in audits),
                    "stored_wall_attempts": sum(audit.wall_attempts for audit in audits),
                    "audit_complete_cases": sum(audit.complete for audit in audits),
                    "run_attempts": run_audit.attempts,
                    "run_label_attempts": run_audit.label_attempts,
                    "run_wall_attempts": run_audit.wall_attempts,
                    "run_form_attempts": run_audit.form_attempts,
                    "run_audit_complete": run_audit.complete,
                    "run_audit_reason": run_audit.reason,
                    "gold_regression_gate": regression.status.value,
                    "full_release_gate": "PASS" if gates.ships else "NOT_READY",
                    "unaccounted_reasons": {
                        score.case_id: score.reason for score in report.scores if not score.measured
                    },
                }
                print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
