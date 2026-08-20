"""The package lifecycle: the states a package revision may hold and the moves between them.

`docs/DESIGN_PLATFORM.md` §5. `states.py` owns the table and the only function that changes a state.
"""

from app.lifecycle.events import (
    STATE_PHRASES,
    ActorMissing,
    history,
    record,
    render_history,
)
from app.lifecycle.states import (
    ASSEMBLY_STATES,
    ENTRY_CONDITIONS,
    FAILURE_STATES,
    PROCESSING_STATES,
    RESUMABLE_STATES,
    REVIEW_OUTCOMES,
    SIDE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    EntryCondition,
    EntryConditionUnmet,
    IllegalTransition,
    UnknownRevision,
    begin,
    render_transition_table,
    transition,
)

__all__ = [
    "ASSEMBLY_STATES",
    "ENTRY_CONDITIONS",
    "FAILURE_STATES",
    "PROCESSING_STATES",
    "RESUMABLE_STATES",
    "REVIEW_OUTCOMES",
    "SIDE_STATES",
    "STATE_PHRASES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "ActorMissing",
    "EntryCondition",
    "EntryConditionUnmet",
    "IllegalTransition",
    "UnknownRevision",
    "begin",
    "history",
    "record",
    "render_history",
    "render_transition_table",
    "transition",
]
