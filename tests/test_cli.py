import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apm import __version__
from apm.cli import app

runner = CliRunner()


@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_version_prints_the_package_version(flag: str) -> None:
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage(flag: str) -> None:
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0
    assert "Usage: apm" in result.stdout


def test_no_arguments_prints_usage_as_a_usage_error() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage: apm" in result.output


def test_unknown_options_exit_with_code_2() -> None:
    result = runner.invoke(app, ["--nope"])
    assert result.exit_code == 2
    assert "--nope" in result.output


def test_runs_as_a_console_script_and_as_a_module() -> None:
    script = Path(sys.executable).parent / "apm"
    for argv in ([str(script), "--version"], [sys.executable, "-m", "apm", "--version"]):
        out = subprocess.run(argv, check=True, capture_output=True, text=True).stdout
        assert out == f"{__version__}\n"
