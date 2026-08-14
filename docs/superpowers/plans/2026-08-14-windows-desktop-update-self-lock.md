# Windows Desktop Self-Update Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Windows Desktop manual updates avoid `hermes.exe` self-lock, reserve ZIP fallback for Git failures, preserve an installed Desktop package, rebuild it on every applicable update path, and recover the current installation without changing user content or configuration.

**Architecture:** Keep the existing updater boundaries but harden each transition. PowerShell launches the Python module entry point and validates a Desktop build; Python classifies subprocess failures before fallback, stages an explicit allowlist of Desktop artifacts atomically, and shares one Desktop rebuild policy across Git and ZIP paths. Development happens in the isolated fix worktree; only tested source changes and regenerated build artifacts reach the live managed checkout.

**Tech Stack:** Windows PowerShell 5.1, Python 3.11, pytest through `scripts/run_tests.sh`, Git worktrees, Electron/npm Desktop build.

## Global Constraints

- Do not modify Hermes conversations, memories, profiles, configuration, credentials, skills, cron jobs, or database contents.
- Do not send Slack, Telegram, or other messaging traffic during verification.
- Use `scripts/run_tests.sh`; never call pytest directly.
- Follow red-green-refactor: observe each new test fail for the intended reason before changing production code.
- Tests must execute behavior; they must not read production source text or patch `sys.platform`.
- Windows-only host behavior uses `@pytest.mark.windows_only`.
- Preserve the existing ZIP all-old-or-all-new rollback contract.
- Preserve only `apps/desktop/release` and `apps/desktop/dist`; do not merge arbitrary ignored files or stale tracked source.
- Do not commit a divergent fix on the live managed `main` checkout and do not push upstream.
- Stop and preserve output on dependency or Desktop build failure; do not loop through another full update.
- The fix worktree must not contain a `venv` junction. Before every canonical
  test-runner invocation, set
  `$env:HERMES_PYTHON='C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'`
  so imports come from the worktree while the interpreter comes from the
  verified live venv.

## File Map

- Modify `scripts/desktop-update/windows.ps1`: Python-module invocation, fixed working directory, authoritative Desktop build validation, executable existence check.
- Modify `hermes_cli/update_cmd.py`: subprocess-failure routing, ZIP artifact staging, staging-size accounting, shared Desktop rebuild helper.
- Create `tests/hermes_cli/test_update_failure_routing.py`: Git-only fallback behavior.
- Modify `tests/hermes_cli/test_update_zip_two_phase.py`: preserved-artifact atomicity, rollback, size accounting, and removal of the banned source-inspection test.
- Create `tests/hermes_cli/test_update_desktop_rebuild.py`: installed-before-update rebuild policy.
- Create `tests/hermes_cli/test_desktop_update_windows_handoff.py`: real PowerShell handoff behavior on Windows.
- Keep `docs/superpowers/specs/2026-08-14-windows-desktop-update-self-lock-design.md` as the approved design record.

---

### Task 1: Route only Git failures to ZIP fallback

**Files:**
- Create: `tests/hermes_cli/test_update_failure_routing.py`
- Modify: `hermes_cli/update_cmd.py:5765-5789`

**Interfaces:**
- Produces: `_failed_subprocess_is_git(error: subprocess.CalledProcessError) -> bool`
- Produces: `_handle_update_subprocess_failure(args, error, *, is_windows: bool, zip_update=None) -> None`
- Consumed by: `_cmd_update_impl` outer `except subprocess.CalledProcessError`

- [ ] **Step 1: Write the failing routing tests**

```python
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
```

- [ ] **Step 2: Run the new file and confirm RED**

Run:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' 'tests/hermes_cli/test_update_failure_routing.py' -q
```

Expected: collection or test failure because the two routing helpers do not exist.

- [ ] **Step 3: Implement the minimal classifier and handler**

Add near the update command helpers in `hermes_cli/update_cmd.py`:

```python
def _failed_subprocess_is_git(error: subprocess.CalledProcessError) -> bool:
    cmd = error.cmd
    if isinstance(cmd, (list, tuple)):
        executable = str(cmd[0]) if cmd else ""
    else:
        executable = str(cmd).lstrip().split(maxsplit=1)[0].strip('"')
    command_name = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return command_name in {"git", "git.exe"}


def _handle_update_subprocess_failure(
    args,
    error: subprocess.CalledProcessError,
    *,
    is_windows: bool,
    zip_update=None,
) -> None:
    if is_windows and _failed_subprocess_is_git(error):
        print(f"⚠ Git update failed: {error}")
        print("→ Falling back to ZIP download...")
        print()
        (zip_update or _update_via_zip)(args)
        return
    print(f"✗ Update failed: {error}")
    raise SystemExit(1)
```

Replace the broad Windows fallback body with:

```python
    except subprocess.CalledProcessError as error:
        _handle_update_subprocess_failure(
            args,
            error,
            is_windows=sys.platform == "win32",
        )
```

- [ ] **Step 4: Run targeted routing and existing command-update tests**

Run:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' `
  'tests/hermes_cli/test_update_failure_routing.py' `
  'tests/hermes_cli/test_cmd_update.py' -q
```

Expected: all tests pass and the new dependency-failure cases never call ZIP update.

- [ ] **Step 5: Commit the routing fix**

```powershell
git add hermes_cli/update_cmd.py tests/hermes_cli/test_update_failure_routing.py
git commit -m "fix(update): limit Windows ZIP fallback to git failures"
```

---

### Task 2: Preserve packaged Desktop artifacts inside atomic ZIP staging

**Files:**
- Modify: `hermes_cli/update_cmd.py:625-920`
- Modify: `tests/hermes_cli/test_update_zip_two_phase.py`

**Interfaces:**
- Produces: `_ZIP_PRESERVED_APPS_PATHS: tuple[Path, ...]`
- Produces: `_path_payload_size(path: Path) -> int`
- Produces: `_zip_staging_size(extracted_root: Path, entries: list[str], project_root: Path) -> int`
- Extends: `_stage_replacement(src: str, dst: str, *, preserve_relative: tuple[Path, ...] = ()) -> str`
- Produces: `_stage_zip_entries(extracted_root: Path, project_root: Path, entries: list[str]) -> list[tuple[str, str]]`

- [ ] **Step 1: Replace the banned source-inspection test with failing behavioral coverage**

Delete `test_update_via_zip_wires_discard_into_the_commit_failure_path`, which uses `inspect.getsource`, and add these behavioral tests:

```python
def test_zip_apps_swap_preserves_only_desktop_artifacts(tmp_path):
    live = tmp_path / "live"
    extracted = tmp_path / "extracted"
    (live / "apps/desktop/release/win-unpacked").mkdir(parents=True)
    (live / "apps/desktop/release/win-unpacked/Hermes.exe").write_bytes(b"old-exe")
    (live / "apps/desktop/dist").mkdir(parents=True)
    (live / "apps/desktop/dist/index.html").write_text("old-dist")
    (live / "apps/desktop/stale.ts").write_text("must disappear")
    (live / "apps/desktop/arbitrary.cache").write_text("must disappear")
    (extracted / "apps/desktop").mkdir(parents=True)
    (extracted / "apps/desktop/main.ts").write_text("new source")

    staged = update_cmd._stage_zip_entries(extracted, live, ["apps"])
    update_cmd._commit_staged_replacements(staged)

    assert (live / "apps/desktop/main.ts").read_text() == "new source"
    assert (live / "apps/desktop/release/win-unpacked/Hermes.exe").read_bytes() == b"old-exe"
    assert (live / "apps/desktop/dist/index.html").read_text() == "old-dist"
    assert not (live / "apps/desktop/stale.ts").exists()
    assert not (live / "apps/desktop/arbitrary.cache").exists()
    assert not [p for p in live.rglob("*") if "hermes-update" in p.name]


def test_zip_apps_swap_rolls_back_artifacts_with_source(tmp_path, monkeypatch):
    live = tmp_path / "live"
    extracted = tmp_path / "extracted"
    _live_tree(live, {"agent": "old"})
    _live_tree(extracted, {"agent": "new"})
    (live / "apps/desktop/release/win-unpacked").mkdir(parents=True)
    (live / "apps/desktop/release/win-unpacked/Hermes.exe").write_bytes(b"old")
    (extracted / "apps/desktop").mkdir(parents=True)
    (extracted / "apps/desktop/main.ts").write_text("new")
    staged = update_cmd._stage_zip_entries(extracted, live, ["agent", "apps"])
    real_rename = update_cmd.os.rename
    calls = {"count": 0}

    def fail_during_second_swap(src, dst):
        calls["count"] += 1
        if calls["count"] == 4:
            raise OSError("simulated rename failure")
        return real_rename(src, dst)

    monkeypatch.setattr(update_cmd.os, "rename", fail_during_second_swap)
    with pytest.raises(OSError):
        try:
            update_cmd._commit_staged_replacements(staged)
        except OSError:
            update_cmd._discard_staged(staged)
            raise

    assert (live / "agent/version.txt").read_text() == "old"
    assert (live / "apps/desktop/release/win-unpacked/Hermes.exe").read_bytes() == b"old"


def test_zip_staging_size_includes_preserved_desktop_bytes(tmp_path):
    live = tmp_path / "live"
    extracted = tmp_path / "extracted"
    (extracted / "apps").mkdir(parents=True)
    (extracted / "apps/source.txt").write_bytes(b"1234")
    (live / "apps/desktop/release").mkdir(parents=True)
    (live / "apps/desktop/release/Hermes.exe").write_bytes(b"123456")

    assert update_cmd._zip_staging_size(extracted, ["apps"], live) == 10
```

- [ ] **Step 2: Run the ZIP two-phase file and confirm RED**

Run:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' 'tests/hermes_cli/test_update_zip_two_phase.py' -q
```

Expected: failures because `_stage_zip_entries` and `_zip_staging_size` do not exist.

- [ ] **Step 3: Implement artifact-aware staging and size accounting**

Add the explicit allowlist and helpers:

```python
_ZIP_PRESERVED_APPS_PATHS = (
    Path("desktop") / "release",
    Path("desktop") / "dist",
)


def _path_payload_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return 0


def _zip_staging_size(extracted_root: Path, entries: list[str], project_root: Path) -> int:
    source_bytes = sum(_path_payload_size(extracted_root / entry) for entry in entries)
    preserved_bytes = sum(
        _path_payload_size(project_root / "apps" / relative)
        for relative in _ZIP_PRESERVED_APPS_PATHS
        if "apps" in entries
    )
    return source_bytes + preserved_bytes
```

Extend `_stage_replacement` so it copies each existing allowed relative path from live `dst` into the staging copy after new source is copied. Reject any relative path that is absolute or contains `..` before copying. Add `_stage_zip_entries` and make `_update_via_zip` call it:

```python
def _stage_zip_entries(extracted_root: Path, project_root: Path, entries: list[str]):
    staged = []
    try:
        for item in entries:
            preserve = _ZIP_PRESERVED_APPS_PATHS if item == "apps" else ()
            src = extracted_root / item
            dst = project_root / item
            staged.append(
                (
                    _stage_replacement(
                        str(src),
                        str(dst),
                        preserve_relative=preserve,
                    ),
                    str(dst),
                )
            )
    except Exception:
        _discard_staged(staged)
        raise
    return staged
```

Use `_zip_staging_size(...)` for the existing 20% free-space calculation and `_stage_zip_entries(...)` for phase 1.

- [ ] **Step 4: Run ZIP atomicity, symlink, and command-update tests**

Run:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' `
  'tests/hermes_cli/test_update_zip_two_phase.py' `
  'tests/hermes_cli/test_update_zip_symlink_reject.py' `
  'tests/hermes_cli/test_cmd_update.py' -q
```

Expected: all tests pass, including rollback and zero-litter checks.

- [ ] **Step 5: Commit ZIP artifact preservation**

```powershell
git add hermes_cli/update_cmd.py tests/hermes_cli/test_update_zip_two_phase.py
git commit -m "fix(update): preserve Desktop package during ZIP fallback"
```

---

### Task 3: Share installed-Desktop rebuild policy across Git and ZIP updates

**Files:**
- Create: `tests/hermes_cli/test_update_desktop_rebuild.py`
- Modify: `hermes_cli/update_cmd.py:776-1035,4533-4583`

**Interfaces:**
- Produces: `_desktop_was_installed(desktop_dir: Path) -> bool`
- Produces: `_maybe_rebuild_desktop(was_installed: bool) -> bool`
- Consumed by: `_update_via_zip` and the Git update completion path.

- [ ] **Step 1: Write failing rebuild-policy tests**

```python
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import update_cmd


def test_previously_installed_desktop_rebuilds_when_output_is_missing(tmp_path, monkeypatch):
    desktop = tmp_path / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd._m(), "_resolve_node_runtime_npm", lambda: "npm.cmd")
    monkeypatch.setattr(update_cmd._m(), "_desktop_build_needed", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda cmd, **kwargs: calls.append(cmd) or SimpleNamespace(returncode=0, stdout=""),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert calls == [[
        update_cmd.sys.executable,
        "-m",
        "hermes_cli.main",
        "desktop",
        "--build-only",
    ]]


def test_never_installed_desktop_is_not_built(tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    with patch.object(update_cmd._m(), "_run_logged_subprocess") as run:
        assert update_cmd._maybe_rebuild_desktop(False) is True
    run.assert_not_called()


def test_rebuild_failure_is_reported_after_one_retry(tmp_path, monkeypatch):
    desktop = tmp_path / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd._m(), "_resolve_node_runtime_npm", lambda: "npm.cmd")
    monkeypatch.setattr(update_cmd._m(), "_desktop_build_needed", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda cmd, **kwargs: calls.append(cmd) or SimpleNamespace(returncode=1, stdout="failed"),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert len(calls) == 2
```

- [ ] **Step 2: Run the rebuild-policy file and confirm RED**

Run:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' 'tests/hermes_cli/test_update_desktop_rebuild.py' -q
```

Expected: failures because the shared rebuild helper does not exist.

- [ ] **Step 3: Extract the existing Git rebuild block into the shared helper**

Implement `_desktop_was_installed` using `_desktop_packaged_executable` or `_desktop_dist_exists`. Implement `_maybe_rebuild_desktop` with the current content-hash precheck, managed Node PATH, logged subprocess, and one retry. Return `False` only when an installed Desktop cannot be rebuilt.

In the Git path, capture the boolean before the source update and replace the inline block with:

```python
        _maybe_rebuild_desktop(desktop_was_installed)
```

In `_update_via_zip`, capture `desktop_was_installed` before extraction/staging and call the same helper after Node and web assets update:

```python
    if not _maybe_rebuild_desktop(desktop_was_installed):
        print("  ⚠ Desktop build failed (non-fatal; the previous package was preserved)")
```

- [ ] **Step 4: Run rebuild, ZIP, and existing Desktop integrity tests**

Run:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' `
  'tests/hermes_cli/test_update_desktop_rebuild.py' `
  'tests/hermes_cli/test_update_zip_two_phase.py' `
  'tests/hermes_cli/test_desktop_exe_integrity.py' `
  'tests/hermes_cli/test_gui_command.py' -q
```

Expected: all tests pass and the failed-build case leaves the preserved prior package available.

- [ ] **Step 5: Commit shared rebuild policy**

```powershell
git add hermes_cli/update_cmd.py tests/hermes_cli/test_update_desktop_rebuild.py
git commit -m "fix(update): rebuild installed Desktop on every source path"
```

---

### Task 4: Remove the Windows handoff self-lock and validate the final package

**Files:**
- Create: `tests/hermes_cli/test_desktop_update_windows_handoff.py`
- Modify: `scripts/desktop-update/windows.ps1:525-558,643-715`

**Interfaces:**
- PowerShell child executable: `venv\Scripts\python.exe`
- Python argv prefix: `-m hermes_cli.main`
- Completion contract: successful update, successful validation build, and existing `RelaunchExe` when supplied.

- [ ] **Step 1: Write the failing Windows handoff test harness**

Create a Windows-only test that makes a temporary venv, copies the current venv's `hermes.exe` launcher into it, and places this fake package first on `PYTHONPATH`:

```python
# fake/hermes_cli/main.py
import json
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
```

The test invokes the real script with `-NoUi`, `DesktopPid=0`, and a temporary `RelaunchExe`, then asserts:

```python
assert calls[0]["argv"] == ["update", "--yes", "--gateway", "--force", "--branch", "main"]
assert calls[0]["shim_replaceable"] is True
assert calls[1]["argv"] == ["desktop", "--build-only"]
assert relaunch.exists()
```

Add a second scenario with `HERMES_HANDOFF_BUILD_FAIL=1`; assert the script exits nonzero, performs the validation build plus one `--force-build --build-only` retry, and writes an `ok: false` result.

- [ ] **Step 2: Run the Windows handoff file and confirm RED**

Run from the fix worktree with the external interpreter selected through
`HERMES_PYTHON`:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' 'tests/hermes_cli/test_desktop_update_windows_handoff.py' -q
```

Expected: the self-lock test records `shim_replaceable: false`, and the validation-build assertion fails because the script does not always validate the package.

- [ ] **Step 3: Invoke the Python module from the installation root**

Set `ProcessStartInfo.WorkingDirectory` in `Invoke-HermesStep`:

```powershell
    $psi.WorkingDirectory = $InstallRoot
```

Replace `$hermesExe` execution with:

```powershell
    $pythonExe = Join-Path $InstallRoot "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        $finalCode = 3
        $finalMsg = "Update aborted: $pythonExe is missing. The install needs repair."
        Write-HandoffLog $finalMsg
        exit $finalCode
    }
    $moduleArgs = @("-m", "hermes_cli.main")
    $updateArgs = $moduleArgs + @("update", "--yes", "--gateway", "--force", "--branch", $Branch)
    $res = Invoke-HermesStep $pythonExe $updateArgs "update"
```

Use the same `$pythonExe` and `$moduleArgs` for the update retry and every Desktop build.

- [ ] **Step 4: Make Desktop build validation authoritative**

After a successful update, always run:

```powershell
    $buildArgs = $moduleArgs + @("desktop", "--build-only")
    $rebuild = Invoke-HermesStep $pythonExe $buildArgs "rebuild"
```

If it fails, or a non-empty `$RelaunchExe` does not exist, retry once with:

```powershell
    $forceBuildArgs = $moduleArgs + @("desktop", "--force-build", "--build-only")
```

Set final code 6 unless the final build exits 0 and the supplied relaunch executable exists. Remove the output-string match as a success gate.

- [ ] **Step 5: Run the handoff test and relevant Desktop Electron tests**

Run:

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' 'tests/hermes_cli/test_desktop_update_windows_handoff.py' -q
Push-Location apps\desktop
npm.cmd test -- --run electron/updater-process.test.ts electron/handoff-result.test.ts
Pop-Location
```

Expected: the shim is replaceable while the fake update runs, build validation is always called, failed final builds report failure, and Electron handoff tests pass.

- [ ] **Step 6: Commit the Windows handoff fix**

```powershell
git add scripts/desktop-update/windows.ps1 tests/hermes_cli/test_desktop_update_windows_handoff.py
git commit -m "fix(update): avoid Windows Desktop updater self-lock"
```

---

### Task 5: Integrated regression and review gate

**Files:**
- Verify only; modify a failing task's files only if a regression is found.

**Interfaces:**
- Consumes all helpers and handoff behavior from Tasks 1-4.
- Produces a reviewed implementation commit range on `fix/windows-desktop-self-update-lock-20260814`.

- [ ] **Step 1: Run the focused update suite**

```powershell
$env:HERMES_PYTHON = 'C:\Users\pains\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
& 'C:\Users\pains\AppData\Local\hermes\git\bin\bash.exe' `
  'scripts/run_tests.sh' `
  'tests/hermes_cli/test_update_failure_routing.py' `
  'tests/hermes_cli/test_update_zip_two_phase.py' `
  'tests/hermes_cli/test_update_zip_symlink_reject.py' `
  'tests/hermes_cli/test_update_desktop_rebuild.py' `
  'tests/hermes_cli/test_desktop_update_windows_handoff.py' `
  'tests/hermes_cli/test_update_concurrent_quarantine.py' `
  'tests/hermes_cli/test_desktop_exe_integrity.py' `
  'tests/hermes_cli/test_gui_command.py' `
  'tests/hermes_cli/test_cmd_update.py' -q
```

Expected: all files pass with no flaky retry summary.

- [ ] **Step 2: Run static checks on changed Python**

```powershell
& $env:HERMES_PYTHON -m compileall -q hermes_cli tests\hermes_cli
git diff --check eca85e81d..HEAD
```

Expected: exit code 0 and no whitespace errors.

- [ ] **Step 3: Review the implementation diff against the approved design**

```powershell
git diff --stat eca85e81d..HEAD
git diff --check eca85e81d..HEAD
git log --oneline --decorate eca85e81d..HEAD
```

Confirm that only the mapped source, test, spec, and plan files changed; no config, secret, database, generated build output, or unrelated source is present.

- [ ] **Step 4: Export recoverable commits outside the live checkout**

```powershell
New-Item -ItemType Directory -Path 'D:\hermes\backups\desktop-update-self-lock-20260814' -Force | Out-Null
git format-patch -o 'D:\hermes\backups\desktop-update-self-lock-20260814' eca85e81d..HEAD
```

Expected: one patch file per committed design, plan, and implementation commit; source branch remains clean.

---

### Task 6: Apply the verified fix and recover the live installation

**Files and state:**
- Modify live source only: the tested production files from Tasks 1-4, left as an uncommitted managed-install patch on `main`.
- Regenerate: `apps/desktop/release/**`, Desktop build stamp, and editable venv metadata.
- Temporarily change process state: Desktop, gateway task, and watchdog task.
- Do not modify: `D:\hermes\config.yaml`, profile configs, state database contents, memories, skills, or jobs.

**Interfaces:**
- Consumes the reviewed worktree commits and external format-patch backup.
- Produces a launchable Desktop, healthy existing `fn_cool` gateway, and a documented live diff.

- [ ] **Step 1: Capture live safety evidence before any mutation**

```powershell
$liveRoot = 'C:\Users\pains\AppData\Local\hermes\hermes-agent'
$hermesHome = 'D:\hermes'
git -C $liveRoot status --short --branch
git -C $liveRoot rev-parse HEAD
git -C $liveRoot rev-parse origin/main
Get-FileHash -Algorithm SHA256 `
  "$hermesHome\config.yaml", `
  "$hermesHome\profiles\fn_cool\config.yaml" | Select-Object Path,Hash
Get-Item -LiteralPath "$hermesHome\state.db" | Select-Object FullName,Length,LastWriteTimeUtc
```

Use the venv Python to open `state.db` in SQLite read-only URI mode and run `PRAGMA quick_check`; expected result is `ok`.

- [ ] **Step 2: Freeze only Hermes runtime processes**

Record scheduled-task enabled/state values. Temporarily disable `Hermes_Gateway_fn_cool_Watchdog`, stop `Hermes_Gateway_fn_cool`, and stop only process trees whose executable or command line resolves under the live Hermes checkout/venv. Requery to prove zero Hermes Desktop/backend/gateway processes before touching the venv.

- [ ] **Step 3: Apply only the tested production patch to live source**

Use `apply_patch` to mirror the reviewed changes in:

```text
scripts/desktop-update/windows.ps1
hermes_cli/update_cmd.py
```

Do not copy the worktree, reset live `main`, or include test/doc files in the managed installation patch. Run `git diff --check` and compare the live production diff with the same two files from the worktree commit range.

- [ ] **Step 4: Repair the editable Python installation without the shim**

```powershell
$env:HERMES_HOME = 'D:\hermes'
& "$liveRoot\venv\Scripts\python.exe" -m pip install -e "${liveRoot}[all]"
```

Expected: exit code 0 and a regenerated, unlocked `venv\Scripts\hermes.exe`. On failure, preserve all output and stop.

- [ ] **Step 5: Build Desktop once from the repaired Python module path**

```powershell
Push-Location $liveRoot
& '.\venv\Scripts\python.exe' -m hermes_cli.main desktop --force-build --build-only
Pop-Location
```

Expected: exit code 0. Verify nontrivial sizes and SHA-256 hashes for:

```text
apps\desktop\release\win-unpacked\Hermes.exe
apps\desktop\release\win-unpacked\resources\app.asar
apps\desktop\release\win-unpacked\resources\install-stamp.json
```

- [ ] **Step 6: Verify repair/build did not alter user state**

Repeat config hashes, `state.db` metadata, and read-only `PRAGMA quick_check` before launching anything. Expected: config hashes identical, database check `ok`, and no unexpected user-state changes.

- [ ] **Step 7: Launch Desktop and restore the existing gateway task**

Launch the rebuilt `Hermes.exe` visibly. Verify its process path, a nonempty main-window title, backend startup, and Desktop logs. Re-enable the watchdog to its recorded original state, start `Hermes_Gateway_fn_cool`, and verify the gateway process, port 8642, `gateway_state.json`, and configured channel connection states without sending messages.

- [ ] **Step 8: Record final source and runtime evidence**

```powershell
git -C $liveRoot status --short --branch
git -C $liveRoot diff --check
git -C $liveRoot diff --stat
```

Report the live production files changed, worktree commit IDs, external patch location, test counts, executable hash, Desktop status, gateway status, and any normal post-launch metadata writes separately from user-content/config changes.
