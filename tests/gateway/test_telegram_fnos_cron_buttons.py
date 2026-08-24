"""Telegram buttons for FNOS cron failure reports."""

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


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("result_line", ["결과: ❌ 실패", "결과: ⚠️ 부분실패"])
async def test_cron_failure_report_gets_fnos_buttons(monkeypatch, result_line):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter._is_no_agent_cron_job", lambda _job_id: True
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows
    )

    result = await adapter.send(
        "12345",
        f"Cronjob Response: 온라인 발주\n\n{result_line}\n실패 이유: 테스트",
        metadata={"job_id": "deb3fe82299a", "notify": True},
    )

    assert result.success is True
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["reply_markup"] == [
        [
            {
                "text": "❗ 실패 이유 확인",
                "callback_data": "fnx:e:deb3fe82299a:",
            }
        ],
        [
            {
                "text": "▶ 실패한 부분 재실행",
                "callback_data": "fnx:r:deb3fe82299a:",
            }
        ],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "metadata"),
    [
        ("결과: ✅ 성공", {"job_id": "deb3fe82299a", "notify": True}),
        ("결과: ❌ 실패", {"notify": True}),
        ("결과: ❌ 실패", {"job_id": "not-a-job-id", "notify": True}),
    ],
)
async def test_non_matching_report_has_no_fnos_buttons(monkeypatch, content, metadata):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=43))
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter._is_no_agent_cron_job", lambda _job_id: True
    )

    await adapter.send("12345", content, metadata=metadata)

    kwargs = adapter._bot.send_message.call_args.kwargs
    assert "reply_markup" not in kwargs


@pytest.mark.asyncio
async def test_agent_cron_failure_report_has_no_fnos_buttons(monkeypatch):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=43))
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter._is_no_agent_cron_job", lambda _job_id: False
    )

    await adapter.send(
        "12345",
        "결과: ❌ 실패",
        metadata={"job_id": "deb3fe82299a", "notify": True},
    )

    assert "reply_markup" not in adapter._bot.send_message.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected_call", "response_text"),
    [
        (
            "fnx:e:deb3fe82299a:",
            ("explain", "deb3fe82299a", None),
            "실패 이유입니다.",
        ),
        (
            "fnx:r:deb3fe82299a:deadbeef",
            ("rerun", "deb3fe82299a", "deadbeef"),
            "이미 해결된 작업입니다.",
        ),
    ],
)
async def test_fnos_callback_calls_api_and_replies_verbatim(
    data, expected_call, response_text
):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=44))

    query = AsyncMock()
    query.data = data
    query.message = SimpleNamespace(
        chat_id=12345,
        chat=SimpleNamespace(type="private"),
        message_id=88,
        message_thread_id=None,
    )
    query.from_user = SimpleNamespace(id="777", first_name="Tester")
    query.answer = AsyncMock()

    update = SimpleNamespace(callback_query=query)

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        with patch(
            "plugins.platforms.telegram.adapter._request_fnos_automation",
            return_value=response_text,
        ) as request:
            await adapter._handle_callback_query(update, MagicMock())

    request.assert_called_once_with(*expected_call)
    query.answer.assert_called_once()
    assert adapter._bot.send_message.call_args.kwargs["text"] == response_text


@pytest.mark.asyncio
async def test_fnos_callback_rejects_unauthorized_user():
    adapter = _make_adapter()
    query = AsyncMock()
    query.data = "fnx:r:deb3fe82299a:"
    query.message = SimpleNamespace(
        chat_id=12345,
        chat=SimpleNamespace(type="private"),
        message_id=89,
        message_thread_id=None,
    )
    query.from_user = SimpleNamespace(id="777", first_name="Tester")
    query.answer = AsyncMock()

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
        with patch(
            "plugins.platforms.telegram.adapter._request_fnos_automation"
        ) as request:
            await adapter._handle_callback_query(
                SimpleNamespace(callback_query=query), MagicMock()
            )

    request.assert_not_called()
    query.answer.assert_called_once()
    assert "권한" in query.answer.call_args.kwargs["text"]
    adapter._bot.send_message.assert_not_called()


def test_fnos_api_request_contract(monkeypatch):
    from plugins.platforms.telegram import adapter as telegram_adapter

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"ok": True, "applied": False, "text": "이미 해결된 작업입니다."}
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(
        telegram_adapter, "_load_fnos_automation_agent_token", lambda: "test-secret"
    )
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    text = telegram_adapter._request_fnos_automation(
        "rerun", "deb3fe82299a", "deadbeef"
    )

    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.full_url == "http://127.0.0.1:3000/api/fnos/automation-ops/telegram"
    assert headers["x-automation-agent-token"] == "test-secret"
    assert json.loads(request.data) == {
        "action": "rerun",
        "job_id": "deb3fe82299a",
        "run_ref": "deadbeef",
    }
    assert captured["timeout"] == 15
    assert text == "이미 해결된 작업입니다."
