"""Started as ``moleditpy --run-plugin job_server``, not only as a pip command."""

import sys
import types

import pytest

from job_server import __main__ as cli


@pytest.fixture
def from_moleditpy(monkeypatch):
    monkeypatch.setitem(sys.modules, "moleditpy.plugins.plugin_runner", types.ModuleType("runner"))


def test_hints_name_the_pip_command_by_default():
    assert cli._program() == "moleditpy-job-server"


def test_hints_name_moleditpy_when_it_runs_the_plugin(from_moleditpy, tmp_path, monkeypatch, capsys):
    config = tmp_path / "settings.json"
    monkeypatch.setattr(sys, "argv", [
        "__main__.py", "init", "--config", str(config), "--data-dir", str(tmp_path / "data"),
    ])
    assert cli.main() == 0
    assert f'moleditpy -r job_server create-admin --config "{config}"' in capsys.readouterr().out


def test_usage_names_moleditpy_when_it_runs_the_plugin(from_moleditpy, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["__main__.py", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: moleditpy -r job_server ")


def test_help_is_plain_ascii_for_a_piped_windows_console(monkeypatch, capsys):
    # A piped Windows console encodes with the ANSI code page (cp932 here),
    # which has no em dash: --help used to end in a UnicodeEncodeError.
    monkeypatch.setattr(sys, "argv", ["moleditpy-job-server", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    capsys.readouterr().out.encode("ascii")
