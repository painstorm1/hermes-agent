"""Diagnostics must distinguish policy rejection from inspected job properties."""

import json
import plistlib
import shlex

import pytest

from tools.terminal_tool_guards import gateway_lifecycle_block


def test_bootstrap_rejection_does_not_invent_keepalive(tmp_path, monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    plist = tmp_path / "com.example.schedule.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.example.schedule",
        "ProgramArguments": ["/bin/true"],
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
    }))
    blocked = gateway_lifecycle_block(
        command=f"launchctl bootstrap gui/501 {plist}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    assert result["exit_code"] == 1
    assert "regardless of the job label" in result["error"]
    assert "does not inspect" in result["error"]
    assert "KeepAlive settings" in result["error"]
    assert "separate shell outside the gateway" in result["error"]


@pytest.mark.parametrize("leaf, blocked", [("echo ok", False), ("hermes gateway restart", True)])
def test_remote_guard_caller_reads_backend_posix_paths(monkeypatch, leaf, blocked):
    from tools import process_registry, terminal_tool
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda _: None)
    reads = []

    class Remote:
        cwd = "/remote"

        def execute(self, command):
            path = shlex.split(command)[-1]
            reads.append(path)
            return {"returncode": 0, "output": {
                "/remote/a.sh": "bash sub/b.sh", "/remote/sub/b.sh": leaf,
            }[path]}

    result = gateway_lifecycle_block(command="bash a.sh", env=Remote(), env_type="ssh",
                                    cwd="/remote", workdir=None, session_key="fixture-remote")
    assert (result is not None) is blocked
    assert reads == ["/remote/a.sh", "/remote/sub/b.sh"]
