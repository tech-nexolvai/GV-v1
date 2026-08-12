"""Licence policy: no AGPL dependency may enter this project.

`AGENTS.md` §2.8 forbids AGPL dependencies. That rule was previously enforced only by a
comment in `pyproject.toml`, which is to say not enforced at all — and the two most
tempting libraries for this problem domain (PyMuPDF for PDFs, Ultralytics YOLO for
detection) are exactly the AGPL ones an agent or a hurried developer would reach for
first.

This test reads the metadata of everything actually installed, so it catches a
transitive AGPL dependency too, not just one named in `pyproject.toml`.
"""

from __future__ import annotations

import re
from importlib.metadata import distributions

# Substrings that indicate a copyleft licence we cannot ship in a commercial product.
FORBIDDEN_LICENCE_PATTERNS = (
    r"\bAGPL\b",
    r"Affero",
)

# Belt and braces: these are known-forbidden by name regardless of what their metadata
# claims, because packaging metadata is inconsistent and these two are specifically
# called out in AGENTS.md §2.8.
FORBIDDEN_PACKAGES = {
    "pymupdf",
    "fitz",
    "pymupdf4llm",
    "ultralytics",
}

# Packages whose metadata mentions AGPL only because they *interoperate* with an AGPL
# tool, or that are dual-licensed in our favour. Each entry needs a written reason.
ALLOWED_EXCEPTIONS: dict[str, str] = {}


def _licence_text(dist) -> str:  # type: ignore[no-untyped-def]
    meta = dist.metadata
    parts = [meta.get("License") or ""]
    parts.extend(v for k, v in meta.items() if k == "Classifier" and "License" in v)
    return " ".join(parts)


def test_no_forbidden_packages_installed() -> None:
    installed = {(d.metadata["Name"] or "").lower() for d in distributions()}
    offenders = sorted(installed & FORBIDDEN_PACKAGES)
    assert not offenders, (
        f"Forbidden package(s) installed: {offenders}. "
        "AGENTS.md §2.8 prohibits AGPL dependencies (PyMuPDF, Ultralytics YOLO). "
        "Use pdfplumber / pypdfium2 / pikepdf instead."
    )


def test_no_agpl_licences_installed() -> None:
    offenders: list[str] = []
    for dist in distributions():
        name = (dist.metadata["Name"] or "").lower()
        if not name or name in ALLOWED_EXCEPTIONS:
            continue
        text = _licence_text(dist)
        if any(re.search(p, text, re.IGNORECASE) for p in FORBIDDEN_LICENCE_PATTERNS):
            offenders.append(f"{name} ({text.strip()[:80]})")

    assert not offenders, (
        "AGPL-licensed distribution(s) found:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nAGENTS.md §2.8 prohibits AGPL dependencies. Remove it, or — if it is a "
        "false positive — add it to ALLOWED_EXCEPTIONS with a written reason."
    )


def test_policy_actually_detects_agpl() -> None:
    """The guard must be proven, not assumed.

    A licence check that silently matches nothing looks identical to a clean project.
    This asserts the patterns fire on representative AGPL metadata strings.
    """
    samples = [
        "AGPL-3.0",
        "GNU Affero General Public License v3",
        "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",
    ]
    for s in samples:
        assert any(
            re.search(p, s, re.IGNORECASE) for p in FORBIDDEN_LICENCE_PATTERNS
        ), f"licence policy failed to flag {s!r}"

    assert "pymupdf" in FORBIDDEN_PACKAGES
    assert "ultralytics" in FORBIDDEN_PACKAGES
