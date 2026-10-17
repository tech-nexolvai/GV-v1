"""Poppler's repeated warnings are folded out of a command's output ($0, no model, no sandbox).

A drawing set whose vendor drawings use a font the reader has no data for makes `pdftotext` print
the same warnings for every text run. On real runs they crowded the output the model reads. The
output keeps each distinct warning once and says how many repeats were left out; everything else
stays exactly as it was. The sandbox image also carries poppler's character maps, so the
"Missing language pack" warning (and the text it dropped) goes away at the source.
"""

from __future__ import annotations

from pathlib import Path

from app.review.assistant.sandbox import quiet_pdf_warnings

_DOCKERFILE = Path(__file__).parents[3] / "deploy" / "assistant_sandbox" / "Dockerfile"


def test_repeats_are_folded_and_counted() -> None:
    noise = [
        "Syntax Error: Missing language pack for 'Adobe-GB1' mapping",
        "Syntax Error: Unknown font tag 'MadeUpFont'",
        "Syntax Error (8309949): No font in show/space",
        "Syntax Error: Unknown font tag 'MadeUpFont'",
        "Syntax Error (8309953): No font in show/space",
        "Syntax Error: Unknown font tag 'MadeUpFont'",
        "Syntax Error (8309955): No font in show/space",
    ]
    text = "== 10\nMade-up heading\n" + "\n".join(noise) + "\nmade-up words\n"
    quiet = quiet_pdf_warnings(text)
    lines = quiet.splitlines()
    assert lines[:2] == ["== 10", "Made-up heading"]
    assert "made-up words" in lines
    assert sum("Unknown font tag" in line for line in lines) == 1
    assert sum("No font in show/space" in line for line in lines) == 1
    assert sum("Missing language pack" in line for line in lines) == 1
    assert lines[-1] == ("[4 repeated PDF reader warnings left out; they do not stop the command]")
    assert quiet.endswith("\n")


def test_output_without_repeats_is_untouched() -> None:
    for text in (
        "",
        "Pages: 60\n",
        "Syntax Error: Unknown font tag 'MadeUpFont'\nok",
        "error: a line that only looks alike\nerror: a line that only looks alike",
    ):
        assert quiet_pdf_warnings(text) == text


def test_the_sandbox_image_has_poppler_character_maps() -> None:
    assert "poppler-data" in _DOCKERFILE.read_text()
