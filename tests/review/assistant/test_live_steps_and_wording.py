"""The assistant panel looks and reads like claude.ai (owner, 2026-10-16). Scripted models, $0.

- Each tool the assistant uses streams as a `step` event the moment it finishes (its command and
  output, or a small picture for a `view`), the lead's with `agent` null and a helper's with its
  description, so the panel grows the step list live instead of showing only "Thinking".
- The code-written subject "On page N," never leaves a capital behind it ("On page 3, The …"),
  never goes on a table line, and no space is left before a full stop.
- The AI page review's `clouded` flag reaches the model as what it means: OUR cloud PDF will cloud
  the row. GV's own markup is "GV's reviewer comments".
- The footer names the model that actually leads (Opus 5.5 when the tool-using assistant runs).

All names, numbers and pages here are made up.
"""

from __future__ import annotations

from tests.review.assistant.test_agent import (
    workspace,  # noqa: F401 - the fixture
)

# ---- live steps ----------------------------------------------------------------------------------


# ---- wording -------------------------------------------------------------------------------------


def test_ai_review_rows_reach_the_model_with_clouded_spelled_out() -> None:
    from app.review.recorded_results import ai_review_row_for_model

    row = ai_review_row_for_model({"key": "made-up", "result": "different", "clouded": True})
    assert "clouded" not in row
    assert row["will_be_clouded_on_our_cloud_pdf"] is True


# ---- footer --------------------------------------------------------------------------------------
