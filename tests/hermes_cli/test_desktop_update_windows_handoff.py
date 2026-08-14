"""Windows Desktop update handoff behavior tests.

The break these tests catch is starting ``hermes.exe`` from the handoff
script: Windows keeps that shim mapped, so the update cannot replace it.  They
also catch treating a successful update command as a successful Desktop update
without validating the package that will be relaunched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest


@pytest.mark.windows_only
def test_windows_handoff_runs_update_without_locking_shim_and_validates_relaunch(
    tmp_path: Path,
) -> None:
    """The update runs through venv Python, then validates the new Desktop app."""
    completed, calls, relaunch, _result = run_windows_handoff(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert calls[0]["argv"] == [
        "update",
        "--yes",
        "--gateway",
        "--force",
        "--branch",
        "main",
    ]
    assert calls[0]["shim_replaceable"] is True
    assert calls[1]["argv"] == ["desktop", "--build-only"]
    assert relaunch.exists()


@pytest.mark.windows_only
def test_windows_handoff_reports_failure_after_validation_build_and_single_retry(
    tmp_path: Path,
) -> None:
    """A failed package validation is retried once and never reports success."""
    completed, calls, _relaunch, result = run_windows_handoff(tmp_path, build_fail=True)

    assert completed.returncode != 0
    assert [call["argv"] for call in calls] == [
        ["update", "--yes", "--gateway", "--force", "--branch", "main"],
        ["desktop", "--build-only"],
        ["desktop", "--force-build", "--build-only"],
    ]
    assert result["ok"] is False


def run_windows_handoff(
    tmp_path: Path, *, build_fail: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]], Path, dict[str, object]]:
    """Run the real handoff script against a disposable venv and fake CLI module."""
    install_root = tmp_path / "hermes-agent"
    install_root.mkdir()
    venv.EnvBuilder(with_pip=False).create(install_root / "venv")

    scripts_dir = install_root / "venv" / "Scripts"
    source_shim = Path(sys.executable).with_name("hermes.exe")
    assert source_shim.exists(), f"expected the test venv launcher at {source_shim}"
    shim = scripts_dir / "hermes.exe"
    shutil.copy2(source_shim, shim)

    fake_root = tmp_path / "fake"
    package_dir = fake_root / "hermes_cli"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "main.py").write_text(
        '''import json
import os
import sys
from pathlib import Path


def main():
    record = Path(os.environ["HERMES_HANDOFF_RECORD"])
    shim = Path(os.environ["HERMES_HANDOFF_SHIM"])
    calls = json.loads(record.read_text()) if record.exists() else []
    unlocked = None
    if sys.argv[1] == "update":
        moved = shim.with_suffix(".probe")
        try:
            shim.rename(moved)
            moved.rename(shim)
            unlocked = True
        except PermissionError:
            unlocked = False
    calls.append({"argv": sys.argv[1:], "shim_replaceable": unlocked})
    record.write_text(json.dumps(calls))
    if sys.argv[1] == "desktop" and os.environ.get("HERMES_HANDOFF_BUILD_FAIL") != "1":
        Path(os.environ["HERMES_HANDOFF_RELAUNCH"]).write_bytes(b"fake-pe")
    if sys.argv[1] == "desktop" and os.environ.get("HERMES_HANDOFF_BUILD_FAIL") == "1":
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )

    record = tmp_path / "calls.json"
    relaunch = tmp_path / "Hermes.exe"
    script = Path(__file__).parents[2] / "scripts" / "desktop-update" / "windows.ps1"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(fake_root),
            "HERMES_HANDOFF_RECORD": str(record),
            "HERMES_HANDOFF_SHIM": str(shim),
            "HERMES_HANDOFF_RELAUNCH": str(relaunch),
        }
    )
    if build_fail:
        env["HERMES_HANDOFF_BUILD_FAIL"] = "1"

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InstallRoot",
            str(install_root),
            "-DesktopPid",
            "0",
            "-RelaunchExe",
            str(relaunch),
            "-NoUi",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    calls = json.loads(record.read_text(encoding="utf-8"))
    result = json.loads((tmp_path / ".hermes-update-result.json").read_text(encoding="utf-8"))
    return completed, calls, relaunch, result
