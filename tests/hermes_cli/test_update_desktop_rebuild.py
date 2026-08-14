import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import update_cmd


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


def test_retry_failure_restores_original_package_not_first_partial_build(
    tmp_path, monkeypatch
):
    live = _prepare_rebuild(tmp_path, monkeypatch, b"original-package")
    attempts = iter((b"first-partial", b"second-partial"))
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(live, next(attempts)),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert (live / "Hermes.exe").read_bytes() == b"original-package"
    assert _release_entries(live) == ["win-unpacked"]


def test_snapshot_survives_builder_cleanup_of_release_output(tmp_path, monkeypatch):
    live = _prepare_rebuild(tmp_path, monkeypatch, b"original-package")
    attempts = iter((b"first-partial", b"second-partial"))

    def run_pack(*args, **kwargs):
        for entry in live.parent.iterdir():
            if entry != live:
                shutil.rmtree(entry)
        return _simulate_pack(live, next(attempts))

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert (live / "Hermes.exe").read_bytes() == b"original-package"


def test_successful_retry_keeps_new_package_and_removes_snapshot(tmp_path, monkeypatch):
    live = _prepare_rebuild(tmp_path, monkeypatch, b"original-package")
    outcomes = iter(((b"first-partial", 1), (b"new-package", 0)))

    def run_pack(*args, **kwargs):
        output_bytes, returncode = next(outcomes)
        return _simulate_pack(live, output_bytes, returncode)

    monkeypatch.setattr(update_cmd._m(), "_run_logged_subprocess", run_pack)

    assert update_cmd._maybe_rebuild_desktop(True) is True
    assert (live / "Hermes.exe").read_bytes() == b"new-package"
    assert _release_entries(live) == ["win-unpacked"]


def test_rebuild_exception_restores_original_package(tmp_path, monkeypatch):
    live = _prepare_rebuild(tmp_path, monkeypatch, b"original-package")
    monkeypatch.setattr(
        update_cmd._m(),
        "_run_logged_subprocess",
        lambda *a, **k: _simulate_pack(
            live, b"partial-package", error=OSError("build crashed")
        ),
    )

    assert update_cmd._maybe_rebuild_desktop(True) is False
    assert (live / "Hermes.exe").read_bytes() == b"original-package"
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
