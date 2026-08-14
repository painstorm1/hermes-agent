import errno
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import main as cli_main
from hermes_cli import update_cmd


PE_AMD64 = 0x8664
VALID_INSTALL_STAMP = {
    "schemaVersion": 1,
    "commit": "1234567890abcdef1234567890abcdef12345678",
    "branch": "main",
}


def _valid_pe(marker=b""):
    buf = bytearray(0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", buf, 0x84, PE_AMD64, 1, 0, 0, 0, 0, 0x0002)
    struct.pack_into("<II", buf, 0x98 + 16, 0x200, 0x200)
    return bytes(buf) + marker


def _write_complete_package(package, executable_bytes, *, asar_bytes=b"app-asar"):
    resources = package / "resources"
    resources.mkdir(parents=True)
    (package / "Hermes.exe").write_bytes(executable_bytes)
    (resources / "app.asar").write_bytes(asar_bytes)
    (resources / "install-stamp.json").write_text(
        json.dumps(VALID_INSTALL_STAMP), encoding="utf-8"
    )
    return package


def _prepare_rebuild(tmp_path, monkeypatch, previous_bytes=None):
    desktop = tmp_path / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    live = desktop / "release" / "win-unpacked"
    if previous_bytes is not None:
        _write_complete_package(live, previous_bytes)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd._m(), "_resolve_node_runtime_npm", lambda: "npm.cmd")
    monkeypatch.setattr(update_cmd._m(), "_desktop_build_needed", lambda *a, **k: True)
    return live


def _simulate_pack(
    live, output_bytes, returncode=1, error=None, *, complete_package=False
):
    backup = live.with_name("win-unpacked.bak")
    if live.exists():
        if backup.exists():
            shutil.rmtree(backup)
        live.rename(backup)
    if complete_package:
        _write_complete_package(live, output_bytes)
    else:
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
    _write_complete_package(snapshot, package_bytes)
    return snapshot


def _damage_complete_package(package: Path, damage: str) -> None:
    app_asar = package / "resources" / "app.asar"
    install_stamp = package / "resources" / "install-stamp.json"
    if damage == "missing-app-asar":
        app_asar.unlink()
    elif damage == "empty-app-asar":
        app_asar.write_bytes(b"")
    elif damage == "missing-install-stamp":
        install_stamp.unlink()
    elif damage == "empty-install-stamp":
        install_stamp.write_bytes(b"")
    elif damage == "invalid-install-stamp":
        install_stamp.write_text('{"schemaVersion": 999}', encoding="utf-8")
    elif damage == "invalid-utf8-install-stamp":
        install_stamp.write_bytes(b"\xff")
    elif damage == "oversized-install-stamp":
        encoded = json.dumps(VALID_INSTALL_STAMP).encode("utf-8")
        install_stamp.write_bytes(encoded + b" " * (65537 - len(encoded)))
    elif damage == "deeply-nested-install-stamp":
        install_stamp.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
    else:
        raise AssertionError(f"unknown package damage: {damage}")


def _make_windows_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        pytest.skip(
            "native Windows directory junction creation unavailable via "
            f"unprivileged mklink /J (exit {result.returncode}): {detail}"
        )


def _unsafe_ancestor_fixture(tmp_path, monkeypatch, ancestor, create_link):
    project_root = tmp_path / "agent"
    apps = project_root / "apps"
    apps.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    if ancestor == "desktop":
        external_desktop = outside / "desktop"
        external_desktop.mkdir()
        (external_desktop / "package.json").write_text("{}")
        external_release = external_desktop / "release"
        link = apps / "desktop"
        target = external_desktop
    else:
        desktop = apps / "desktop"
        desktop.mkdir()
        (desktop / "package.json").write_text("{}")
        external_release = outside / "release"
        external_release.mkdir()
        link = desktop / "release"
        target = external_release

    external_live = _write_complete_package(
        external_release / "win-unpacked", _valid_pe(b"external-package")
    )
    marker = outside / "external-marker.bin"
    marker.write_bytes(b"outside-unchanged")
    create_link(link, target)
    snapshot = _write_recovery_snapshot(
        project_root, _valid_pe(b"original-package")
    )
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        update_cmd._m(), "_resolve_node_runtime_npm", lambda: "npm.cmd"
    )
    monkeypatch.setattr(
        update_cmd._m(), "_desktop_build_needed", lambda *a, **k: True
    )
    return project_root, external_live, marker, snapshot


def _assert_builder_leaf_link_fails_closed_and_restores(
    tmp_path, monkeypatch, unsafe_leaf, create_link
):
    original = _valid_pe(b"original-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    backup = live.with_name("win-unpacked.bak")
    outside = tmp_path / "outside-builder-target"
    outside.mkdir()
    marker = outside / "marker.bin"
    marker.write_bytes(b"external-unchanged")
    calls = []

    def run_pack(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            live.rename(backup)
            if unsafe_leaf == "live":
                create_link(live, outside)
            else:
                shutil.rmtree(backup)
                create_link(backup, outside)
                live.mkdir()
                (live / "Hermes.exe").write_bytes(b"partial-package")
        return SimpleNamespace(returncode=1, stdout="failed")

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert len(calls) == 1
    assert marker.read_bytes() == b"external-unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["marker.bin"]
    assert (live / "Hermes.exe").read_bytes() == original
    assert (live / "resources" / "app.asar").read_bytes() == b"app-asar"
    assert not os.path.lexists(backup)
    assert not (_recovery_root(tmp_path) / "snapshot").exists()
    assert _release_entries(live) == ["win-unpacked"]


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
        _write_complete_package(live, _valid_pe(b"new-package"))
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
        return _simulate_pack(
            live,
            output_bytes,
            returncode,
            complete_package=returncode == 0,
        )

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
    _write_complete_package(live, _valid_pe(b"original-package"))
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
        return _simulate_pack(
            live, new_package, returncode=0, complete_package=True
        )

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


@pytest.mark.windows_only
@pytest.mark.parametrize(
    "damaged_resource",
    (
        "missing-app-asar",
        "empty-app-asar",
        "missing-install-stamp",
        "empty-install-stamp",
        "invalid-install-stamp",
    ),
)
def test_orphan_snapshot_restores_package_with_incomplete_resources_before_stamp_skip(
    tmp_path, monkeypatch, damaged_resource
):
    original = _valid_pe(b"original-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, _valid_pe(b"incomplete-package"))
    _damage_complete_package(live, damaged_resource)
    snapshot = _write_recovery_snapshot(tmp_path, original)
    monkeypatch.setattr(
        update_cmd._m(), "_desktop_build_needed", lambda *a, **k: False
    )

    with patch.object(update_cmd._m(), "_run_logged_subprocess") as run:
        assert update_cmd._maybe_rebuild_desktop(True) is True

    run.assert_not_called()
    assert (live / "Hermes.exe").read_bytes() == original
    assert (live / "resources" / "app.asar").read_bytes() == b"app-asar"
    assert json.loads(
        (live / "resources" / "install-stamp.json").read_text(encoding="utf-8")
    ) == VALID_INSTALL_STAMP
    assert not snapshot.exists()


@pytest.mark.windows_only
@pytest.mark.parametrize(
    "damage",
    (
        "missing-app-asar",
        "empty-app-asar",
        "missing-install-stamp",
        "empty-install-stamp",
        "invalid-install-stamp",
        "invalid-utf8-install-stamp",
        "oversized-install-stamp",
        "deeply-nested-install-stamp",
    ),
)
def test_current_hash_rebuilds_incomplete_package_without_snapshot(
    tmp_path, monkeypatch, damage
):
    new_package = _valid_pe(b"rebuilt-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, _valid_pe(b"incomplete-package"))
    _damage_complete_package(live, damage)
    monkeypatch.setattr(
        update_cmd._m(), "_desktop_build_needed", lambda *a, **k: False
    )
    calls = []

    def run_pack(*args, **kwargs):
        calls.append(args)
        return _simulate_pack(
            live, new_package, returncode=0, complete_package=True
        )

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert len(calls) == 1
    assert (live / "Hermes.exe").read_bytes() == new_package
    assert (live / "resources" / "app.asar").read_bytes() == b"app-asar"
    assert json.loads(
        (live / "resources" / "install-stamp.json").read_text(encoding="utf-8")
    ) == VALID_INSTALL_STAMP


@pytest.mark.windows_only
def test_current_hash_skips_complete_package_without_snapshot(tmp_path, monkeypatch):
    original = _valid_pe(b"complete-package")
    live = _prepare_rebuild(tmp_path, monkeypatch, original)
    hash_checks = []
    monkeypatch.setattr(
        update_cmd._m(),
        "_desktop_build_needed",
        lambda *a, **k: hash_checks.append((a, k)) or False,
    )

    with patch.object(update_cmd._m(), "_run_logged_subprocess") as run:
        assert update_cmd._maybe_rebuild_desktop(True) is True

    run.assert_not_called()
    assert len(hash_checks) == 1
    assert (live / "Hermes.exe").read_bytes() == original


@pytest.mark.require_symlinks
@pytest.mark.parametrize("ancestor", ("desktop", "release"))
def test_rebuild_rejects_symlinked_package_ancestor_without_touching_external_tree(
    tmp_path, monkeypatch, ancestor
):
    project_root, external_live, marker, snapshot = _unsafe_ancestor_fixture(
        tmp_path,
        monkeypatch,
        ancestor,
        lambda link, target: link.symlink_to(target, target_is_directory=True),
    )
    calls = []
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: calls.append(a)
        or SimpleNamespace(returncode=1, stdout="unsafe"),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert calls == []
    assert marker.read_bytes() == b"outside-unchanged"
    assert (external_live / "Hermes.exe").read_bytes() == _valid_pe(
        b"external-package"
    )
    assert sorted(path.name for path in external_live.parent.iterdir()) == [
        "win-unpacked"
    ]
    assert snapshot.is_dir()
    recovery_root = _recovery_root(project_root)
    assert not (recovery_root / "snapshot.new").exists()
    assert not (recovery_root / "failed-live").exists()


@pytest.mark.windows_only
@pytest.mark.parametrize("ancestor", ("desktop", "release"))
def test_rebuild_rejects_junction_package_ancestor_without_touching_external_tree(
    tmp_path, monkeypatch, ancestor
):
    project_root, external_live, marker, snapshot = _unsafe_ancestor_fixture(
        tmp_path, monkeypatch, ancestor, _make_windows_junction
    )
    calls = []
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: calls.append(a)
        or SimpleNamespace(returncode=1, stdout="unsafe"),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert calls == []
    assert marker.read_bytes() == b"outside-unchanged"
    assert (external_live / "Hermes.exe").read_bytes() == _valid_pe(
        b"external-package"
    )
    assert sorted(path.name for path in external_live.parent.iterdir()) == [
        "win-unpacked"
    ]
    assert snapshot.is_dir()
    recovery_root = _recovery_root(project_root)
    assert not (recovery_root / "snapshot.new").exists()
    assert not (recovery_root / "failed-live").exists()


@pytest.mark.require_symlinks
@pytest.mark.parametrize("unsafe_leaf", ("live", "backup"))
def test_retry_rejects_builder_created_symlink_leaf_and_restores_original(
    tmp_path, monkeypatch, unsafe_leaf
):
    _assert_builder_leaf_link_fails_closed_and_restores(
        tmp_path,
        monkeypatch,
        unsafe_leaf,
        lambda link, target: link.symlink_to(target, target_is_directory=True),
    )


@pytest.mark.windows_only
@pytest.mark.parametrize("unsafe_leaf", ("live", "backup"))
def test_retry_rejects_builder_created_junction_leaf_and_restores_original(
    tmp_path, monkeypatch, unsafe_leaf
):
    _assert_builder_leaf_link_fails_closed_and_restores(
        tmp_path, monkeypatch, unsafe_leaf, _make_windows_junction
    )


def test_os_rebuild_lock_is_exclusive_and_leftover_file_is_reacquirable(tmp_path):
    first = update_cmd._acquire_desktop_rebuild_lock(tmp_path)
    assert first is not None
    try:
        assert update_cmd._acquire_desktop_rebuild_lock(tmp_path) is None
    finally:
        update_cmd._release_desktop_rebuild_lock(first)

    lock_path = _recovery_root(tmp_path) / "rebuild.lock"
    assert lock_path.is_file()
    reacquired = update_cmd._acquire_desktop_rebuild_lock(tmp_path)
    assert reacquired is not None
    update_cmd._release_desktop_rebuild_lock(reacquired)


@pytest.mark.windows_only
def test_os_rebuild_lock_retries_transient_windows_release_delay(
    tmp_path, monkeypatch
):
    import msvcrt

    real_locking = msvcrt.locking
    attempts = 0

    def delayed_locking(descriptor, mode, byte_count):
        nonlocal attempts
        if mode == msvcrt.LK_NBLCK:
            attempts += 1
            if attempts < 3:
                raise OSError(errno.EACCES, "lock release still propagating")
        return real_locking(descriptor, mode, byte_count)

    monkeypatch.setattr(msvcrt, "locking", delayed_locking)

    lock = update_cmd._acquire_desktop_rebuild_lock(tmp_path)

    assert lock is not None
    assert attempts == 3
    update_cmd._release_desktop_rebuild_lock(lock)


def test_active_os_rebuild_lock_rejects_build_within_bounded_wait(
    tmp_path, monkeypatch
):
    _prepare_rebuild(tmp_path, monkeypatch, _valid_pe(b"original-package"))
    first = update_cmd._acquire_desktop_rebuild_lock(tmp_path)
    assert first is not None
    started = time.monotonic()
    try:
        with patch.object(update_cmd._m(), "_run_logged_subprocess") as run:
            assert update_cmd._maybe_rebuild_desktop(True) is False
        run.assert_not_called()
    finally:
        update_cmd._release_desktop_rebuild_lock(first)
    assert time.monotonic() - started < 2


def test_abrupt_process_exit_releases_os_rebuild_lock(tmp_path):
    code = "\n".join(
        (
            "import time",
            "from pathlib import Path",
            "from hermes_cli import update_cmd",
            f"lock = update_cmd._acquire_desktop_rebuild_lock(Path({str(tmp_path)!r}))",
            "assert lock is not None",
            "print('LOCKED', flush=True)",
            "time.sleep(60)",
        )
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        assert update_cmd._acquire_desktop_rebuild_lock(tmp_path) is None
        child.kill()
        child.wait(timeout=10)

        recovered = update_cmd._acquire_desktop_rebuild_lock(tmp_path)
        assert recovered is not None
        update_cmd._release_desktop_rebuild_lock(recovered)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


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
        lambda *a, **k: _simulate_pack(
            live, new_package, returncode=0, complete_package=True
        ),
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
        lambda *a, **k: _simulate_pack(
            live, new_package, returncode=0, complete_package=True
        ),
    )
    monkeypatch.setattr(
        update_cmd,
        "_release_desktop_rebuild_lock",
        lambda lock: (_ for _ in ()).throw(OSError("lock busy")),
        raising=False,
    )

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert (live / "Hermes.exe").read_bytes() == new_package
    assert (_recovery_root(tmp_path) / "rebuild.lock").is_file()
    assert "lock busy" in capsys.readouterr().out
