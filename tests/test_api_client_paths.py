"""The front end's request paths are relative to the API base, and nothing checks that but this.

Verification for: `frontend/main/src/api/client.ts`.

`request` prepends `BASE`, which is `/api/v1`. A path written as `/api/v1/projects/...` therefore
resolves to `/api/v1/api/v1/projects/...` and 404s — at runtime, in the browser, for that one feature.

**Nothing else in the pipeline can see it.** TypeScript checks the type of a string, not its value;
`npm run build` succeeds; the generated schema uses full paths, so copying one from there is the
natural mistake to make. It shipped in #530 for exactly that reason and was found by eye while adding
the next endpoint.

A Python test reading a TypeScript file is unusual and deliberate: this repository has no JavaScript
test runner, and the alternative to an odd-looking guard is no guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[1] / "frontend" / "main" / "src" / "api" / "client.ts"

#: A path handed to `request(...)` or interpolated into a `fetch` of `BASE`, as written in the source.
#:
#: Matches the opening quote through the first quote of the same kind, which is enough: a path is a
#: single literal in every call, and a path assembled from pieces would not be one this can read —
#: `test_the_guard_can_see_the_paths` is what stops that going unnoticed.
_REQUEST_PATH = re.compile(r"request<[^>]*>\(\s*([`'\"])(/[^`'\"]*)\1", re.DOTALL)
_BASE_FETCH = re.compile(r"fetch\(\s*`\$\{BASE\}(/[^`]*)`", re.DOTALL)


def _paths() -> list[str]:
    source = CLIENT.read_text(encoding="utf-8")
    return [match.group(2) for match in _REQUEST_PATH.finditer(source)] + [
        match.group(1) for match in _BASE_FETCH.finditer(source)
    ]


def test_the_guard_can_see_the_paths() -> None:
    """A regex over source that matches nothing passes and proves nothing.

    The count is deliberately a floor rather than an exact number: this file gains calls, and a test
    that had to be edited every time one was added is a test somebody eventually edits without
    reading.
    """
    assert CLIENT.exists(), f"the API client moved: {CLIENT}"
    found = _paths()
    assert (
        len(found) >= 10
    ), f"the guard found only {len(found)} request paths; the pattern is stale"
    assert any(path.startswith("/projects/") for path in found), found


@pytest.mark.parametrize("path", _paths())
def test_a_request_path_is_relative_to_the_api_base(path: str) -> None:
    """No path repeats the prefix `request` already adds.

    Parametrised so a failure names the offending path rather than reporting that one of thirty is
    wrong — the whole difficulty with this bug is finding which call is broken.
    """
    assert not path.startswith("/api/"), (
        f"{path!r} repeats the API base that `request` prepends, so it would resolve to "
        f"'/api/v1{path}'. Write it relative: {path.removeprefix('/api/v1')!r}."
    )
