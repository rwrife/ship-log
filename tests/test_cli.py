"""Tests for the M1 scaffold: version + hello banner."""

from __future__ import annotations

import subprocess
import sys

from typer.testing import CliRunner

from shiplog import __version__
from shiplog.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_hello_default() -> None:
    result = runner.invoke(app, ["hello"])
    assert result.exit_code == 0
    assert "ship-log" in result.stdout
    assert "sailor" in result.stdout


def test_hello_named() -> None:
    result = runner.invoke(app, ["hello", "--name", "Ada"])
    assert result.exit_code == 0
    assert "Ada" in result.stdout


def test_python_dash_m_entrypoint() -> None:
    """``python -m shiplog`` runs the same app as the console script."""
    result = subprocess.run(
        [sys.executable, "-m", "shiplog", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout
