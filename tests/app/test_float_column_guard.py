"""Prove the Semgrep database-column guard rejects approximate persisted numbers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_semgrep(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    semgrep = shutil.which("semgrep")
    if semgrep is None:
        pytest.skip("semgrep is installed in the CI safety-guards job")
    app = tmp_path / "app"
    app.mkdir()
    (app / "model.py").write_text(source, encoding="utf-8")
    return subprocess.run(
        [semgrep, "--config", str(REPO_ROOT / ".semgrep" / "gv-rules.yaml"), "--error", "app"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_float_column_is_rejected(tmp_path: Path) -> None:
    """Input mapped_column(Float) must fail with the gv-no-float-column rule."""

    result = _run_semgrep(
        tmp_path,
        "from sqlalchemy import Float\nfrom sqlalchemy.orm import mapped_column\nx = mapped_column(Float)\n",
    )

    assert result.returncode != 0
    assert "gv-no-float-column" in result.stdout + result.stderr


def test_numeric_column_is_allowed(tmp_path: Path) -> None:
    """Input mapped_column(Numeric) is exact and must remain accepted."""

    result = _run_semgrep(
        tmp_path,
        "from sqlalchemy import Numeric\nfrom sqlalchemy.orm import mapped_column\nx = mapped_column(Numeric)\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
