import json
import os
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import main as cli_main
from hermes_cli import update_cmd


PE_AMD64 = 0x8664


def _valid_pe(marker=b""):
    buf = bytearray(0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", buf, 0x84, PE_AMD64, 1, 0, 0, 0, 0, 0x0002)
    struct.pack_into("<II", buf, 0x98 + 16, 0x200, 0x200)
    return bytes(buf) + marker


def _prepare_rebuild(tmp_path, monkeypatch, previous_bytes=None):
    desktop = tmp_path / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    live = desktop / "release" / "win-unpacked"
    if previous_bytes is not None:
        live.mkdir(parents=True)
        (live / "Hermes.exe").write_bytes(previous_bytes)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd._m(), "_resolve_node_runtime_npm", lambda: "npm.cmd")
    monkeypatch.setattr(update_cmd._m(), "_desktop_build_needed", lambda *a, **k: True)
    return live


def _simulate_pack(live, output_bytes, returncode=1, error=None):
    backup = live.with_name("win-unpacked.bak")
    if live.exists():
        if backup.exists():
            shutil.rmtree(backup)
        live.rename(backup)
    live.mkdir(parents=True)
    (live / "Hermes.exe").write_bytes(output_bytes)
    if error is not None:
        raise error
    return SimpleNamespace(returncode=returncode, stdout="failed")


def _release_entries(live):
    return sorted(path.name for path in live.parent.iterdir())


def _recovery_root(project_root):
    return project_root / "venv" / ".desktop-update-recovery"


def _write_recovery_snapshot(project_root, package_bytes):
    snapshot = _recovery_root(project_root) / "snapshot" / "win-unpacked"
    snapshot.mkdir(parents=True)
    (snapshot / "Hermes.exe").write_bytes(package_bytes)
    return snapshot


def _write_rebuild_lock(project_root, pid, start_time):
    lock = _recovery_root(project_root) / "lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(
        json.dumps({"pid": pid, "start_time": start_time, "token": "other-owner"}),
        encoding="utf-8",
    )
    return lock


def test_previously_installed_desktop_rebuilds_when_output_is_missing(tmp_path, monkeypatch):
    desktop = tmp_path / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd._m(), "_resolve_node_runtime_npm", lambda: "npm.cmd")
    monkeypatch.setattr(update_cmd._m(), "_desktop_build_needed", lambda *a, **k: True)
    calls = []

    def run_build(cmd, **kwargs):
        calls.append(cmd)
        live = desktop / "release" / "win-unpacked"
        live.mkdir(parents=True)
        (live / "Hermes.exe").write_bytes(_valid_pe(b"new-package"))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        run_build,
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


def test_installed_desktop_with_missing_package_cannot_be_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)

    assert update_cmd._maybe_rebuild_desktop(True) is False


def test_installed_desktop_with_missing_managed_npm_cannot_be_rebuilt(
    tmp_path, monkeypatch
):
    desktop = tmp_path / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd._m(), "_resolve_node_runtime_npm", lambda: None)

    assert update_cmd._maybe_rebuild_desktop(True) is False


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


@pytest.mark.windows_only
def test_retry_failure_restores_original_package_not_first_partial_build(
    tmp_path, monkeypatch
):
    original = _valid_pe(b"original-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    attempts = iter((b"first-partial", b"second-partial"))
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(live, next(attempts)),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert (live / "Hermes.exe").read_bytes() == original
    assert _release_entries(live) == ["win-unpacked"]


@pytest.mark.windows_only
def test_snapshot_survives_builder_cleanup_of_release_output(tmp_path, monkeypatch):
    original = _valid_pe(b"original-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    attempts = iter((b"first-partial", b"second-partial"))

    def run_pack(*args, **kwargs):
        for entry in live.parent.iterdir():
            if entry != live:
                shutil.rmtree(entry)
        return _simulate_pack(live, next(attempts))

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert (live / "Hermes.exe").read_bytes() == original


@pytest.mark.windows_only
def test_successful_retry_keeps_new_package_and_removes_snapshot(tmp_path, monkeypatch):
    original = _valid_pe(b"original-package")
    new_package = _valid_pe(b"new-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    outcomes = iter(((b"first-partial", 1), (new_package, 0)))

    def run_pack(*args, **kwargs):
        output_bytes, returncode = next(outcomes)
        return _simulate_pack(live, output_bytes, returncode)

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert (live / "Hermes.exe").read_bytes() == new_package
    assert not (_recovery_root(tmp_path) / "snapshot").exists()
    assert _release_entries(live) == ["win-unpacked"]


@pytest.mark.windows_only
def test_rebuild_exception_restores_original_package(tmp_path, monkeypatch):
    original = _valid_pe(b"original-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(
            live, b"partial-package", error=OSError("build crashed")
        ),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert (live / "Hermes.exe").read_bytes() == original
    assert _release_entries(live) == ["win-unpacked"]


@pytest.mark.windows_only
def test_zero_exit_with_invalid_package_retries_then_restores_original(
    tmp_path, monkeypatch
):
    original = _valid_pe(b"original-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    attempts = iter((b"first-invalid", b"second-invalid"))
    calls = []

    def run_pack(*args, **kwargs):
        calls.append(args)
        return _simulate_pack(live, next(attempts), returncode=0)

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert len(calls) == 2
    assert (live / "Hermes.exe").read_bytes() == original
    assert _release_entries(live) == ["win-unpacked"]


def test_rebuild_without_prior_package_does_not_restore_partial_as_backup(
    tmp_path, monkeypatch
):
    live = _prepare_rebuild(tmp_path, monkeypatch)
    attempts = iter((b"first-partial", b"second-partial"))
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(live, next(attempts)),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert (live / "Hermes.exe").read_bytes() == b"second-partial"
    assert _release_entries(live) == ["win-unpacked"]


@pytest.mark.windows_only
def test_recovery_snapshot_does_not_stale_desktop_content_stamp(
    tmp_path, monkeypatch
):
    desktop = tmp_path / "apps" / "desktop"
    (desktop / "src").mkdir(parents=True)
    (desktop / "src" / "main.ts").write_text("export const ready = true\n")
    (desktop / "package.json").write_text("{}")
    (desktop / "dist").mkdir()
    (desktop / "dist" / "index.html").write_text("ready")
    live = desktop / "release" / "win-unpacked"
    live.mkdir(parents=True)
    (live / "Hermes.exe").write_bytes(_valid_pe(b"original-package"))
    (tmp_path / ".gitignore").write_text(
        "/venv/\napps/desktop/dist/\napps/desktop/release/\n"
    )
    stamp = tmp_path / "desktop-build-stamp.json"
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli_main, "_desktop_stamp_path", lambda: stamp)
    cli_main._write_desktop_build_stamp(tmp_path, source_mode=True)
    initial_hash = cli_main._compute_desktop_content_hash(tmp_path)

    snapshot = update_cmd._snapshot_windows_desktop_package(desktop)

    assert live.is_dir()
    assert snapshot[1] == _recovery_root(tmp_path) / "snapshot" / "win-unpacked"
    assert cli_main._compute_desktop_content_hash(tmp_path) == initial_hash
    assert cli_main._desktop_build_needed(
        desktop, tmp_path, source_mode=True
    ) is False

    update_cmd._discard_windows_desktop_snapshot(snapshot)

    assert cli_main._compute_desktop_content_hash(tmp_path) == initial_hash
    assert cli_main._desktop_build_needed(
        desktop, tmp_path, source_mode=True
    ) is False


@pytest.mark.parametrize("interrupted_live", [None, b"partial-package"])
@pytest.mark.windows_only
def test_orphan_snapshot_recovers_before_rebuild(
    tmp_path, monkeypatch, interrupted_live
):
    original = _valid_pe(b"original-package")
    new_package = _valid_pe(b"new-package")
    live = _prepare_rebuild(tmp_path, monkeypatch)
    if interrupted_live is not None:
        live.mkdir(parents=True)
        (live / "Hermes.exe").write_bytes(interrupted_live)
    snapshot = _write_recovery_snapshot(tmp_path, original)
    observed_before_build = []

    def run_pack(*args, **kwargs):
        observed_before_build.append((live / "Hermes.exe").read_bytes())
        return _simulate_pack(live, new_package, returncode=0)

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert observed_before_build == [original]
    assert (live / "Hermes.exe").read_bytes() == new_package
    assert not snapshot.exists()
    assert _release_entries(live) == ["win-unpacked"]


@pytest.mark.windows_only
def test_orphan_snapshot_is_cleaned_when_live_package_is_valid(
    tmp_path, monkeypatch
):
    live = _prepare_rebuild(tmp_path, monkeypatch, _valid_pe(b"new-package"))
    snapshot = _write_recovery_snapshot(
        tmp_path, _valid_pe(b"old-package")
    )
    monkeypatch.setattr(
        update_cmd._m(), "_desktop_build_needed", lambda *a, **k: False
    )

    with patch.object(update_cmd._m(), "_run_logged_subprocess") as run:
        assert update_cmd._maybe_rebuild_desktop(True) is True

    run.assert_not_called()
    assert (live / "Hermes.exe").read_bytes() == _valid_pe(b"new-package")
    assert not snapshot.exists()


def test_active_rebuild_lock_rejects_concurrent_invocation(tmp_path, monkeypatch):
    from gateway.status import _get_process_start_time

    original = _valid_pe(b"original-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    start_time = _get_process_start_time(os.getpid())
    assert start_time is not None
    lock = _write_rebuild_lock(tmp_path, os.getpid(), start_time)

    with patch.object(update_cmd._m(), "_run_logged_subprocess") as run:
        assert update_cmd._maybe_rebuild_desktop(True) is False

    run.assert_not_called()
    assert (live / "Hermes.exe").read_bytes() == original
    assert json.loads((lock / "owner.json").read_text(encoding="utf-8"))[
        "token"
    ] == "other-owner"


def test_stale_rebuild_lock_is_reclaimed(tmp_path, monkeypatch):
    live = _prepare_rebuild(
        tmp_path, monkeypatch, _valid_pe(b"original-package")
    )
    lock = _write_rebuild_lock(tmp_path, 2_147_483_646, 0)
    new_package = _valid_pe(b"new-package")
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(live, new_package, returncode=0),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert (live / "Hermes.exe").read_bytes() == new_package
    assert not lock.exists()


@pytest.mark.windows_only
def test_snapshot_cleanup_failure_does_not_fail_successful_build(
    tmp_path, monkeypatch, capsys
):
    live = _prepare_rebuild(
        tmp_path, monkeypatch, _valid_pe(b"original-package")
    )
    new_package = _valid_pe(b"new-package")
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(live, new_package, returncode=0),
    )
    monkeypatch.setattr(
        update_cmd,
        "_discard_windows_desktop_snapshot",
        lambda snapshot: (_ for _ in ()).throw(OSError("snapshot busy")),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert (live / "Hermes.exe").read_bytes() == new_package
    assert "snapshot busy" in capsys.readouterr().out


def test_lock_cleanup_failure_does_not_fail_successful_build(
    tmp_path, monkeypatch, capsys
):
    live = _prepare_rebuild(
        tmp_path, monkeypatch, _valid_pe(b"original-package")
    )
    new_package = _valid_pe(b"new-package")
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(live, new_package, returncode=0),
    )
    monkeypatch.setattr(
        update_cmd,
        "_release_desktop_rebuild_lock",
        lambda lock: (_ for _ in ()).throw(OSError("lock busy")),
        raising=False,
    )

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert (live / "Hermes.exe").read_bytes() == new_package
    assert (_recovery_root(tmp_path) / "lock").exists()
    assert "lock busy" in capsys.readouterr().out
