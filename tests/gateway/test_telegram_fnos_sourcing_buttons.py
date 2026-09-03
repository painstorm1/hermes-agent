"""Telegram 있음/없음 buttons for the weekly FNOS sourcing meeting (fnx:s:<date>:<y|n>)."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from tests.gateway.test_telegram_fnos_cron_buttons import (  # noqa: E402
    _ensure_telegram_mock,
    _make_adapter,
    _make_query,
)

_ensure_telegram_mock()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,answer", [("y", "yes"), ("n", "no")])
async def test_sourcing_button_posts_answer_and_relays_text(suffix, answer):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=45))
    query = _make_query(f"fnx:s:2026-09-02:{suffix}", report="이번 주 조사할 수입상품이 있습니까?")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_sourcing_answer",
        return_value="자동추천 모드로 저장했습니다.",
    ) as request:
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    request.assert_called_once_with("2026-09-02", answer, "Tester")
    assert adapter._bot.send_message.call_args.kwargs["text"] == "자동추천 모드로 저장했습니다."
    assert "저장" in query.answer.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_sourcing_button_rejects_malformed_and_unauthorized():
    adapter = _make_adapter()
    bad = _make_query("fnx:s:2026-9-2:y")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_sourcing_answer"
    ) as request:
        await adapter._handle_callback_query(SimpleNamespace(callback_query=bad), MagicMock())
    request.assert_not_called()
    assert "잘못된" in bad.answer.call_args.kwargs["text"]

    denied = _make_query("fnx:s:2026-09-02:y")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_sourcing_answer"
    ) as request:
        await adapter._handle_callback_query(SimpleNamespace(callback_query=denied), MagicMock())
    request.assert_not_called()
    assert "권한" in denied.answer.call_args.kwargs["text"]
    adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_sourcing_button_api_failure_sends_safe_error():
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=46))
    query = _make_query("fnx:s:2026-09-02:n")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_sourcing_answer",
        side_effect=RuntimeError("AUTOMATION_AGENT_TOKEN is not configured"),
    ):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    text = adapter._bot.send_message.call_args.kwargs["text"]
    assert text.startswith("요청 처리 실패")


def test_sourcing_api_request_contract(monkeypatch):
    from plugins.platforms.telegram import adapter as telegram_adapter

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"ok": True, "text": "다음 메시지로 제품명을 보내주세요."}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(telegram_adapter, "_load_fnos_automation_agent_token", lambda: "test-secret")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    text = telegram_adapter._request_fnos_sourcing_answer("2026-09-02", "yes", "재민")

    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.full_url == "http://127.0.0.1:3000/api/fnos/import/sourcing-meetings/current"
    assert headers["x-automation-agent-token"] == "test-secret"
    assert json.loads(request.data) == {
        "action": "answer",
        "meeting_date": "2026-09-02",
        "answer": "yes",
        "actor": "telegram:재민",
    }
    assert captured["timeout"] == 15
    assert text == "다음 메시지로 제품명을 보내주세요."
