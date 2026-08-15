# CLIENT_FACTS — moved

The authority is **[`docs/CLIENT_FACTS.md`](docs/CLIENT_FACTS.md)**.

`scripts/client_facts.py` reads that file, and `scripts/issue_gate.py` decides whether a story is
blocked from it. Editing this one changes nothing.

Two files claiming to be the single source of truth is exactly the problem `CLIENT_FACTS.md` was
created to end, so this is a pointer rather than a copy.
