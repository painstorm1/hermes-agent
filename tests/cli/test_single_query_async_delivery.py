"""Single-query CLI runtimes must not promise detached completions."""

from types import SimpleNamespace

import cli as cli_mod
from gateway.session_context import async_delivery_supported, reset_session_vars


def test_chat_q_scopes_async_delivery_off_for_the_turn(monkeypatch):
    """``hermes chat -q`` exits after one turn, so background work must run inline."""
    seen = []

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = SimpleNamespace(print=lambda *_a, **_kw: None)
            self.session_id = "single-query-test"
            self.agent = SimpleNamespace(
                session_id=self.session_id,
                platform="cli",
            )

        def _claim_active_session(self, _surface, *, stderr=False):
            return True

        def _show_security_advisories(self):
            pass

        def chat(self, _query, images=None):
            seen.append(async_delivery_supported())
            return "done"

        def _print_exit_summary(self, clear_screen=True):
            pass

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "0")

    reset_session_vars()
    try:
        assert async_delivery_supported() is True
        cli_mod.main(query="run the cron job", quiet=False, toolsets="cronjob")
        assert seen == [False]
        assert async_delivery_supported() is True
    finally:
        reset_session_vars()
