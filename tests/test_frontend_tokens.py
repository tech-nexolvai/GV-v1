"""Every design token the frontend references must exist.

An undefined CSS custom property fails silently. `color: var(--color-danger)` where no such token
is defined does not error, does not warn, and does not fall back to something sensible — the
declaration is simply dropped and the element inherits. The result is a colour that looks almost
right, which is the hardest kind of visual bug to notice and the easiest to accumulate.

Seventeen of them had accumulated, across nine files. `--color-danger` and `--color-text-muted`
appeared in five stylesheets each, so danger colours and muted text were not applying anywhere they
were used. They came from a second naming vocabulary that had never existed — `--color-surface`,
`--font-body`, `--text-secondary` — sitting alongside the real one.

This test is cheap and would have caught all seventeen the day the first one was written.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "main" / "src"

#: A token definition: `--name:` at the start of a declaration.
DEFINITION = re.compile(r"^\s*(--[\w-]+)\s*:", re.MULTILINE)

#: A token reference inside `var()`.
REFERENCE = re.compile(r"var\(\s*(--[\w-]+)")


def _sources() -> list[Path]:
    return sorted(path for suffix in ("*.css", "*.tsx", "*.ts") for path in FRONTEND.rglob(suffix))


def test_every_referenced_design_token_is_defined() -> None:
    """**Input: every `var(--x)` in the frontend. Outcome: every one resolves.**

    Checked across `.tsx` as well as `.css`, because inline styles reference tokens too and are
    exactly as silent when the name is wrong.
    """
    defined: set[str] = set()
    for path in FRONTEND.rglob("*.css"):
        defined |= set(DEFINITION.findall(path.read_text(encoding="utf-8")))

    referenced: dict[str, set[str]] = {}
    for path in _sources():
        for token in REFERENCE.findall(path.read_text(encoding="utf-8")):
            referenced.setdefault(token, set()).add(path.name)

    missing = {token: sorted(files) for token, files in referenced.items() if token not in defined}

    assert not missing, (
        "these design tokens are used but never defined, so every declaration using them is "
        "silently dropped:\n  "
        + "\n  ".join(f"{token} — {', '.join(files)}" for token, files in sorted(missing.items()))
    )


def test_the_guard_can_actually_fail() -> None:
    """A guard that cannot fire looks exactly like a clean codebase.

    The test above passes trivially if the reference regex stops matching anything — a refactor to
    a different variable syntax, say. This proves both patterns still find what they are for.
    """
    # Shaped like the real stylesheets: a definition sits on its own line, which is what the
    # anchored pattern requires. An earlier version of this sample put it inline after `:root {`
    # and failed — proving the meta-test works, by catching its own bad fixture first.
    sample = ":root {\n  --real: 1px;\n}\n.x { border: var(--real) solid var(--imaginary); }"

    assert DEFINITION.findall(sample) == ["--real"]
    assert REFERENCE.findall(sample) == ["--real", "--imaginary"]
