"""One policy for reviewer notes and finding decisions before sign-off."""

BLOCKING_OUTCOMES = frozenset({"FAIL", "REVIEW_REQUIRED", "NOT_FOUND"})
NOTE_OUTCOMES = frozenset({"REVIEW_REQUIRED", "NOT_FOUND"})


def needs_note(outcome: str, action: str) -> bool:
    return outcome in NOTE_OUTCOMES and action in {"confirm", "dismiss"}
