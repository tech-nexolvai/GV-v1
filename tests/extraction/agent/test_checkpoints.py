"""The agent checkpoint tables' shape (#247).

The reading agent's node checkpoints (`extraction/agent/checkpoints.py`) were removed on 2026-10-28:
nothing imported them and their claims table never had a row. The tables stay (a table is never
dropped), so their shape is still checked here.
"""

from __future__ import annotations

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base


def test_schema_has_a_separate_claim_and_direct_candidate_link() -> None:
    """Input: ORM metadata. Output: mutable claim plus append-only invocation attribution."""

    claim = Base.metadata.tables["agent_node_invocation_claims"]
    invocation = Base.metadata.tables["model_invocations"]

    assert {"node_invocation_key", "status", "model_invocation_id", "candidate_id"} <= set(
        claim.columns.keys()
    )
    assert invocation.columns["node_invocation_key"].unique is True
    assert invocation.columns["candidate_id"].unique is True
