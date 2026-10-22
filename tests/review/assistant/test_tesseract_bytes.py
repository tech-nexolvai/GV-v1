"""#1866: a Tesseract failure says Tesseract's words, never "UnicodeDecodeError".

pytesseract 0.3.13 decodes Tesseract's error message and output file as strict UTF-8: one byte
that is not text raised `UnicodeDecodeError`, and the drawn-ink check and `verify_value` logged
only that name (18 Oct). A fake `tesseract` program (a shell script) stands in for the real one.

Verification for: `verify_value.lenient_tesseract` (used by `TesseractReader` and the tag reader).
"""

from __future__ import annotations

import stat
from pathlib import Path

import numpy as np
import pytest

from app.review.assistant.verify_value import lenient_tesseract

pytesseract = pytest.importorskip("pytesseract")


def _program(tmp_path: Path, body: str) -> str:
    path = tmp_path / "tesseract"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    lenient_tesseract(pytesseract)
    return tmp_path


def _read() -> str:
    picture = np.full((20, 40), 255, dtype=np.uint8)
    return str(pytesseract.image_to_string(picture, config="--psm 7", timeout=10))


def test_a_failure_whose_message_is_not_text_says_tesseract_s_words(
    fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _program(fake, 'printf "Error: cannot write \\377\\376 output\\n" >&2\nexit 1\n')
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", command)
    with pytest.raises(pytesseract.TesseractError) as failed:
        _read()
    assert "Error: cannot write" in str(failed.value) and "output" in str(failed.value)


def test_an_output_with_a_byte_that_is_not_text_is_still_read(
    fake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tesseract's arguments: <image> <output base> ...; it writes <output base>.txt.
    command = _program(fake, 'printf "36 1/4\\377\\n" > "$2.txt"\nexit 0\n')
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", command)
    assert _read().startswith("36 1/4")


def test_a_valid_output_reads_as_before(fake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    command = _program(fake, 'printf \'24 3/8"\\n\' > "$2.txt"\nexit 0\n')
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", command)
    assert _read() == '24 3/8"\n'
    # Made lenient once per process.
    assert lenient_tesseract(pytesseract) is pytesseract
