"""One-shot completion-only reserve for exhausted agent turns.

The reserve never changes approvals or tools. It opens one fresh turn only
when the exhausted turn emits a strict evidence certificate, and it cannot
recurse because callers invoke the raw conversation loop for the reserve.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

_BEGIN = "[HERMES_COMPLETION_RESERVE_V1]"
_END = "[/HERMES_COMPLETION_RESERVE_V1]"
_REQUIRED = (
    "root_cause",
    "ticket_scope",
    "verified_work",
    "remaining_steps",
    "approval_basis",
)


def _strict_turns(value: Any) -> int:
    return value if type(value) is int and 1 <= value <= 500 else 0


def _strict_string_list(value: Any) -> list[str]:
    """Accept a YAML list or the exact JSON list emitted by ``config set``."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    if any(not isinstance(item, str) or not item for item in value):
        return []
    return value


def turns_for_platform(cfg: dict, platform: str) -> int:
    """Return reserve turns for an explicitly enabled top-level platform."""
    agent_cfg = cfg.get("agent") if isinstance(cfg, dict) else None
    reserve = (
        agent_cfg.get("completion_reserve") if isinstance(agent_cfg, dict) else None
    )
    if not isinstance(reserve, dict) or reserve.get("enabled") is not True:
        return 0
    platforms = _strict_string_list(reserve.get("platforms"))
    if platform not in platforms:
        return 0
    return _strict_turns(reserve.get("max_turns"))


def turns_for_cron_job(cfg: dict, job_id: str) -> int:
    """Return reserve turns for one exact, allowlisted cron job ID."""
    cron_cfg = cfg.get("cron") if isinstance(cfg, dict) else None
    reserve = cron_cfg.get("completion_reserve") if isinstance(cron_cfg, dict) else None
    if not isinstance(reserve, dict) or reserve.get("enabled") is not True:
        return 0
    job_ids = _strict_string_list(reserve.get("job_ids"))
    if job_id not in job_ids:
        return 0
    return _strict_turns(reserve.get("max_turns"))


def contract(task_id: str) -> str:
    """Prompt contract used by the existing toolless exhaustion summary call."""
    return f"""

[HERMES COMPLETION RESERVE CONTRACT]
This exact task may receive at most ONE fresh completion-only turn after the
normal iteration budget ends. This contract never grants approval for a
mutation, deployment, message, payment, or other protected action.

Only when an iteration-limit summary is requested, append the exact block below
with `eligible: true` if ALL conditions are already evidenced: the root cause
is confirmed; ticket scope and completion conditions are fixed; implementation
and focused verification are complete; only deterministic closure remains; all
required approvals already exist independently; and no new error, repeated
failure, no-progress state, investigation, redesign, or scope expansion is
needed. Otherwise set `eligible: false`.

{_BEGIN}
eligible: true|false
task_id: {task_id}
root_cause: <specific evidence-backed cause>
ticket_scope: <fixed scope and completion condition>
verified_work: <tests/build/readback already completed>
remaining_steps: <deterministic closure steps only>
approval_basis: <existing independent approval evidence or not_required>
{_END}
""".rstrip()


def certified_checkpoint(text: Any, task_id: str) -> Optional[dict[str, str]]:
    """Parse a strict positive checkpoint from the final exhaustion summary."""
    if not isinstance(text, str):
        return None
    begin = text.rfind(_BEGIN)
    end = text.find(_END, begin + len(_BEGIN))
    if begin < 0 or end < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[begin + len(_BEGIN) : end].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()
    if fields.get("eligible", "").lower() != "true":
        return None
    if fields.get("task_id") != task_id:
        return None
    if any(not fields.get(name) for name in _REQUIRED):
        return None
    return fields


def is_iteration_limit(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("failed") is not True
        and result.get("interrupted") is not True
        and result.get("completed") is False
        and str(result.get("turn_exit_reason") or "").startswith(
            "max_iterations_reached("
        )
        and bool(str(result.get("final_response") or "").strip())
    )


def continue_once(
    agent: Any,
    first_result: dict,
    *,
    task_id: str,
    reserve_turns: int,
    run_turn: Callable[[str, list], dict],
) -> dict:
    """Run one certified completion turn; never loop or widen authority."""
    checkpoint = certified_checkpoint(first_result.get("final_response"), task_id)
    if checkpoint is None:
        first_result["_completion_reserve"] = {
            "eligible": False,
            "attempted": False,
            "completed": False,
            "reason": "missing_or_invalid_certificate",
        }
        first_result["failed"] = True
        first_result.setdefault(
            "error", "iteration limit reached without a valid completion certificate"
        )
        return first_result

    prompt = f"""[HERMES COMPLETION RESERVE — ONE TURN ONLY]
Continue the exact task from the preceding transcript. Execute only the
certified remaining steps. Do not investigate, redesign, refactor, add scope,
or infer approval. Existing security and approval boundaries are unchanged.
If any new error, failed check, scope change, or missing approval appears, stop
and report it honestly. No third turn will be granted.

Root cause: {checkpoint["root_cause"]}
Ticket scope: {checkpoint["ticket_scope"]}
Verified work: {checkpoint["verified_work"]}
Remaining steps: {checkpoint["remaining_steps"]}
Approval basis: {checkpoint["approval_basis"]}
"""
    original_max = agent.max_iterations
    try:
        agent.max_iterations = reserve_turns
        result = run_turn(prompt, list(first_result.get("messages") or []))
    finally:
        agent.max_iterations = original_max

    if not isinstance(result, dict):
        raise RuntimeError("completion reserve returned an invalid result")
    completed = bool(
        result.get("completed") is True and result.get("failed") is not True
    )
    result["_completion_reserve"] = {
        "eligible": True,
        "attempted": True,
        "completed": completed,
        "reason": "completed" if completed else "reserve_turn_incomplete",
        "first_turn_api_calls": first_result.get("api_calls", 0),
    }
    if not completed:
        result["failed"] = True
        result.setdefault(
            "error", "completion reserve ended without verified completion"
        )
    return result
