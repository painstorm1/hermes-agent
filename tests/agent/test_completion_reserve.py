from __future__ import annotations

import pytest

from agent.completion_reserve import (
    certified_checkpoint,
    continue_once,
    is_iteration_limit,
    turns_for_cron_job,
    turns_for_platform,
)


def _certificate(task_id="task-1", *, eligible="true"):
    return f"""Iteration budget ended.
[HERMES_COMPLETION_RESERVE_V1]
eligible: {eligible}
task_id: {task_id}
root_cause: Verified root cause.
ticket_scope: Fixed ticket and completion condition.
verified_work: Focused tests and build passed.
remaining_steps: Commit, approved deployment, and fresh readback only.
approval_basis: Existing user approval in the original ticket.
[/HERMES_COMPLETION_RESERVE_V1]
"""


def _limit_result(summary=None):
    summary = summary or _certificate()
    return {
        "final_response": summary,
        "messages": [{"role": "assistant", "content": summary}],
        "api_calls": 60,
        "completed": False,
        "failed": False,
        "interrupted": False,
        "turn_exit_reason": "max_iterations_reached(60/60)",
    }


def _complete_result():
    return {
        "final_response": "completed",
        "messages": [{"role": "assistant", "content": "completed"}],
        "api_calls": 4,
        "completed": True,
        "failed": False,
        "interrupted": False,
        "turn_exit_reason": "text_response(finish_reason=stop)",
    }


def test_platform_config_is_exact_and_fail_closed():
    cfg = {
        "agent": {
            "completion_reserve": {
                "enabled": True,
                "platforms": ["telegram", "cli"],
                "max_turns": 60,
            }
        }
    }
    assert turns_for_platform(cfg, "telegram") == 60
    assert turns_for_platform(cfg, "cron") == 0
    assert turns_for_platform({}, "telegram") == 0

    for malformed in (True, "60", 0, -1, 501):
        cfg["agent"]["completion_reserve"]["max_turns"] = malformed
        assert turns_for_platform(cfg, "telegram") == 0


def test_cron_config_requires_exact_job_id():
    cfg = {
        "cron": {
            "completion_reserve": {
                "enabled": True,
                "job_ids": ["job-1"],
                "max_turns": 60,
            }
        }
    }
    assert turns_for_cron_job(cfg, "job-1") == 60
    assert turns_for_cron_job(cfg, "other") == 0


def test_config_set_json_string_lists_are_strictly_supported():
    cfg = {
        "agent": {
            "completion_reserve": {
                "enabled": True,
                "platforms": '["telegram","slack"]',
                "max_turns": 60,
            }
        },
        "cron": {
            "completion_reserve": {
                "enabled": True,
                "job_ids": '["job-1"]',
                "max_turns": 60,
            }
        },
    }
    assert turns_for_platform(cfg, "telegram") == 60
    assert turns_for_cron_job(cfg, "job-1") == 60

    for malformed in ("telegram,slack", '{"telegram": true}', '["telegram",3]', '[""]'):
        cfg["agent"]["completion_reserve"]["platforms"] = malformed
        assert turns_for_platform(cfg, "telegram") == 0


def test_certificate_requires_exact_task_and_every_evidence_field():
    assert certified_checkpoint(_certificate(), "task-1") is not None
    assert certified_checkpoint(_certificate("other"), "task-1") is None
    assert certified_checkpoint(_certificate(eligible="false"), "task-1") is None
    assert (
        certified_checkpoint(
            _certificate().replace(
                "verified_work: Focused tests and build passed.\n", ""
            ),
            "task-1",
        )
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"completed": True},
        {"failed": True},
        {"interrupted": True},
        {"turn_exit_reason": "text_response(finish_reason=stop)"},
        {"final_response": ""},
    ],
)
def test_only_real_iteration_exhaustion_is_eligible(change):
    result = _limit_result()
    result.update(change)
    assert is_iteration_limit(result) is False


def test_certified_checkpoint_gets_exactly_one_fresh_turn_and_restores_budget():
    class Agent:
        max_iterations = 60

    agent = Agent()
    calls = []

    def run_turn(prompt, history):
        calls.append((prompt, history, agent.max_iterations))
        return _complete_result()

    first = _limit_result()
    result = continue_once(
        agent,
        first,
        task_id="task-1",
        reserve_turns=7,
        run_turn=run_turn,
    )

    assert result["completed"] is True
    assert len(calls) == 1
    assert calls[0][1] == first["messages"]
    assert calls[0][2] == 7
    assert agent.max_iterations == 60
    assert result["_completion_reserve"]["completed"] is True


def test_invalid_checkpoint_never_calls_reserve():
    class Agent:
        max_iterations = 60

    def should_not_run(*_args):
        raise AssertionError("reserve must not run")

    result = continue_once(
        Agent(),
        _limit_result("ordinary fallback summary"),
        task_id="task-1",
        reserve_turns=7,
        run_turn=should_not_run,
    )
    assert result["failed"] is True
    assert result["error"] == (
        "iteration limit reached without a valid completion certificate"
    )
    assert result["_completion_reserve"]["attempted"] is False


def test_second_limit_is_returned_incomplete_without_a_third_turn():
    class Agent:
        max_iterations = 60

    calls = 0

    def run_turn(_prompt, _history):
        nonlocal calls
        calls += 1
        return _limit_result()

    result = continue_once(
        Agent(),
        _limit_result(),
        task_id="task-1",
        reserve_turns=7,
        run_turn=run_turn,
    )
    assert calls == 1
    assert result["failed"] is True
    assert result["error"] == "completion reserve ended without verified completion"
    assert result["_completion_reserve"]["reason"] == "reserve_turn_incomplete"


def test_new_reserve_error_propagates_and_restores_budget():
    class Agent:
        max_iterations = 60

    agent = Agent()

    def run_turn(_prompt, _history):
        assert agent.max_iterations == 7
        raise RuntimeError("new error")

    with pytest.raises(RuntimeError, match="new error"):
        continue_once(
            agent,
            _limit_result(),
            task_id="task-1",
            reserve_turns=7,
            run_turn=run_turn,
        )
    assert agent.max_iterations == 60
