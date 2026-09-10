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
import struct
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.windows_only
def test_windows_handoff_runs_update_without_locking_shim_and_validates_relaunch(
    tmp_path: Path,
) -> None:
    """The update runs through venv Python, then validates the new Desktop app."""
    completed, calls, relaunch, result = run_windows_handoff(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert calls[0]["argv"] == [
        "update",
        "--yes",
        "--gateway",
        "--force",
        "--branch",
        "main",
        "--keep-stash",
    ]
    assert calls[0]["shim_replaceable"] is True
    assert calls[0]["cwd"] == str(tmp_path / "hermes-agent")
    assert calls[1]["argv"] == ["desktop", "--build-only"]
    assert relaunch.exists()
    assert result["ok"] is True
    assert (tmp_path / "relaunched.txt").exists()
    assert not (tmp_path / ".hermes-update-in-progress").exists()


@pytest.mark.windows_only
def test_windows_handoff_reports_failure_after_validation_build_and_single_retry(
    tmp_path: Path,
) -> None:
    """A failed package validation is retried once and never reports success."""
    completed, calls, _relaunch, result = run_windows_handoff(tmp_path, build_fail=True)

    assert completed.returncode != 0
    assert [call["argv"] for call in calls] == [
        ["update", "--yes", "--gateway", "--force", "--branch", "main", "--keep-stash"],
        ["desktop", "--build-only"],
        ["desktop", "--force-build", "--build-only"],
    ]
    assert result["ok"] is False


@pytest.mark.windows_only
def test_windows_handoff_retries_once_when_validation_build_does_not_create_relaunch(
    tmp_path: Path,
) -> None:
    """A missing requested executable makes the otherwise-successful build fail."""
    completed, calls, relaunch, result = run_windows_handoff(
        tmp_path, skip_relaunch=True
    )

    assert completed.returncode == 6
    assert not relaunch.exists()
    assert [call["argv"] for call in calls] == [
        ["update", "--yes", "--gateway", "--force", "--branch", "main", "--keep-stash"],
        ["desktop", "--build-only"],
        ["desktop", "--force-build", "--build-only"],
    ]
    assert len(calls) == 3
    assert result["ok"] is False


def run_windows_handoff(
    tmp_path: Path, *, build_fail: bool = False, skip_relaunch: bool = False
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
    repo = Path(__file__).parents[2]
    # Fake only update/build commands; receipt verification imports real code.
    (package_dir / "__init__.py").write_text(
        f"__path__.append({str(repo / 'hermes_cli')!r})\n", encoding="utf-8")
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
    calls.append({"argv": sys.argv[1:], "shim_replaceable": unlocked, "cwd": os.getcwd()})
    record.write_text(json.dumps(calls))
    if os.environ.get("HERMES_HANDOFF_SKIP_RELAUNCH") == "1":
        raise SystemExit(0)
    if sys.argv[1] == "desktop" and os.environ.get("HERMES_HANDOFF_BUILD_FAIL") != "1":
        import shutil
        shutil.copy2(os.environ["HERMES_HANDOFF_TEMPLATE"], os.environ["HERMES_HANDOFF_RELAUNCH"])
        from hermes_cli.main_desktop import _write_desktop_build_stamp
        _write_desktop_build_stamp(Path.cwd(), source_mode=False)
    if sys.argv[1] == "desktop" and os.environ.get("HERMES_HANDOFF_BUILD_FAIL") == "1":
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )

    record = tmp_path / "calls.json"
    relaunch = install_root / "apps/desktop/release/win-unpacked/Hermes.exe"
    resources = relaunch.parent / "resources"
    dist = resources / "app.asar.unpacked/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script type="module" src="./assets/index.js"></script>', encoding="utf-8")
    (dist / "assets/index.js").write_text("export {};", encoding="utf-8")
    entry = b'import "electron";'
    (dist / "electron-main.mjs").write_bytes(entry)
    package = json.dumps({"main": "dist/electron-main.mjs"}).encode()
    header = json.dumps({"files": {"package.json": {"size": len(package), "offset": "0"}, "dist": {"files": {
        "electron-main.mjs": {"size": len(entry), "unpacked": True}}}}}).encode()
    padded = header + b"\0" * (-len(header) % 4)
    (resources / "app.asar").write_bytes(struct.pack("<4I", 4, 8 + len(padded), 4 + len(padded), len(header)) + padded + package)
    (install_root / ".gitignore").write_text("apps/desktop/release/\n", encoding="utf-8")
    template = tmp_path / "fixture.exe"
    receipt = str(tmp_path / "relaunched.txt").replace('"', '""')
    compile_result = subprocess.run([
        "powershell.exe", "-NoProfile", "-Command",
        "Add-Type -OutputType WindowsApplication -OutputAssembly '" + str(template).replace("'", "''") +
        "' -TypeDefinition 'public class Fixture { public static void Main() { System.IO.File.WriteAllText(@\"" +
        receipt + "\", \"fixture\"); System.Threading.Thread.Sleep(25000); } }'",
    ], capture_output=True, text=True, timeout=30)
    assert compile_result.returncode == 0, compile_result.stderr
    script = Path(__file__).parents[2] / "scripts" / "desktop-update" / "windows.ps1"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join([str(fake_root), str(repo), sysconfig.get_paths()["purelib"], env.get("PYTHONPATH", "")]),
            "PSModuleAnalysisCachePath": str(tmp_path / "ModuleAnalysisCache"),
            "SystemDrive": env.get("SystemDrive", "C:"),
            "HERMES_HANDOFF_RECORD": str(record),
            "HERMES_HANDOFF_SHIM": str(shim),
            "HERMES_HANDOFF_RELAUNCH": str(relaunch),
            "HERMES_HANDOFF_TEMPLATE": str(template),
        }
    )
    if build_fail:
        env["HERMES_HANDOFF_BUILD_FAIL"] = "1"
    if skip_relaunch:
        env["HERMES_HANDOFF_SKIP_RELAUNCH"] = "1"

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
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )
    calls = json.loads(record.read_text(encoding="utf-8"))
    result = json.loads((tmp_path / ".hermes-update-result.json").read_text(encoding="utf-8"))
    return completed, calls, relaunch, result
