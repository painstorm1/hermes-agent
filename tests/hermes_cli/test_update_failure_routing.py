from subprocess import CalledProcessError
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd


def test_windows_git_failure_uses_zip_fallback(monkeypatch):
    args = SimpleNamespace(branch="main")
    calls = []
    error = CalledProcessError(128, ["git", "merge", "--ff-only", "origin/main"])
    monkeypatch.setattr(update_cmd._m(), "_is_windows", lambda: True)
    monkeypatch.setattr(update_cmd, "_update_via_zip", lambda value, **kwargs: calls.append(value))
    update_cmd._handle_update_called_process_error(error, args, False, False)
    assert calls == [args]


@pytest.mark.parametrize("command", [
    [r"D:\hermes\bin\uv.exe", "pip", "install", "-e", "."],
    ["npm.cmd", "ci"],
    [r"C:\Python311\python.exe", "-m", "compileall"],
])
def test_windows_non_git_failure_never_uses_zip(command, monkeypatch):
    calls = []
    error = CalledProcessError(2, command)
    monkeypatch.setattr(update_cmd._m(), "_is_windows", lambda: True)
    monkeypatch.setattr(update_cmd, "_update_via_zip", lambda value, **kwargs: calls.append(value))
    with pytest.raises(SystemExit) as exc_info:
        update_cmd._handle_update_called_process_error(error, SimpleNamespace(branch="main"), False, False)
    assert exc_info.value.code == 1
    assert calls == []


def test_git_executable_path_is_recognized():
    error = CalledProcessError(1, [r"C:\Program Files\Git\cmd\git.exe", "fetch"])
    assert update_cmd._called_process_error_is_git(error) is True
