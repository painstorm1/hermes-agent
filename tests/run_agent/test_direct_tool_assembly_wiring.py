"""Focused constructor wiring for route-local direct tool assembly."""

from run_agent import AIAgent


def test_ai_agent_forwards_skip_tool_search_assembly(monkeypatch):
    captured = {}

    def _init_agent(_agent, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("agent.agent_init.init_agent", _init_agent)

    AIAgent(skip_tool_search_assembly=True)

    assert captured["skip_tool_search_assembly"] is True