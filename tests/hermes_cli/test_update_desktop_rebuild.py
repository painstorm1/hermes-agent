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
