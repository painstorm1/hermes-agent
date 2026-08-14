from subprocess import CalledProcessError
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd


def test_windows_git_failure_uses_zip_fallback():
    args = SimpleNamespace(branch="main")
    calls = []
    error = CalledProcessError(128, ["git", "merge", "--ff-only", "origin/main"])

    update_cmd._handle_update_subprocess_failure(
        args,
        error,
        is_windows=True,
        zip_update=lambda value: calls.append(value),
    )

    assert calls == [args]


@pytest.mark.parametrize(
    "command",
    [
        [r"D:\\hermes\\bin\\uv.exe", "pip", "install", "-e", "."],
        ["npm.cmd", "ci"],
        [r"C:\\Python311\\python.exe", "-m", "compileall"],
    ],
)
def test_windows_non_git_failure_never_uses_zip(command):
    calls = []
    error = CalledProcessError(2, command)

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._handle_update_subprocess_failure(
            SimpleNamespace(branch="main"),
            error,
            is_windows=True,
            zip_update=lambda value: calls.append(value),
        )

    assert exc_info.value.code == 1
    assert calls == []


def test_git_executable_path_is_recognized():
    error = CalledProcessError(1, [r"C:\\Program Files\\Git\\cmd\\git.exe", "fetch"])
    assert update_cmd._failed_subprocess_is_git(error) is True
