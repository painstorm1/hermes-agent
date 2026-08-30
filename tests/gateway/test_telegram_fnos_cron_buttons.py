"""Telegram buttons for FNOS cron failure reports."""

import json
import os
import sys
from datetime import datetime, timezone
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


def _patch_markup(monkeypatch, job):
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter._get_current_cron_job",
        lambda _job_id: job,
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows
    )


def _make_query(
    data,
    *,
    thread_id=None,
    report="결과: ❌ 실패",
    report_at=datetime(2026, 8, 29, 23, 45, tzinfo=timezone.utc),
):
    query = AsyncMock()
    query.data = data
    query.message = SimpleNamespace(
        chat_id=12345,
        chat=SimpleNamespace(
            id=12345,
            type="private",
            is_forum=False,
            title="Automation Reports",
            full_name="Automation Reports",
        ),
        message_id=88,
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        text=report,
        caption=None,
        from_user=SimpleNamespace(
            id="999",
            username="bot",
            full_name="Hermes Bot",
            is_bot=True,
        ),
        reply_to_message=None,
        date=report_at,
    )
    query.from_user = SimpleNamespace(id="777", first_name="Tester")
    query.answer = AsyncMock()
    return query


@pytest.mark.asyncio
@pytest.mark.parametrize("result_line", ["결과: ❌ 실패", "결과: ⚠️ 부분실패"])
async def test_no_agent_failure_gets_exact_explain_and_legacy_rerun_buttons(
    monkeypatch, result_line
):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *args, **kwargs: False)
    _patch_markup(monkeypatch, {"id": "deb3fe82299a", "no_agent": True})

    result = await adapter.send(
        "12345",
        f"Cronjob Response: 온라인 발주\n\n{result_line}\n실패 이유: 테스트",
        metadata={
            "job_id": "deb3fe82299a",
            "cron_output_ref": "2026-08-30_10-20-30.md",
            "notify": True,
        },
    )

    assert result.success is True
    assert adapter._bot.send_message.call_args.kwargs["reply_markup"] == [
        [
            {
                "text": "❗ 실패 이유 확인",
                "callback_data": "fnx:e:deb3fe82299a:2026-08-30_10-20-30.md",
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
async def test_agent_failure_gets_explain_only(monkeypatch):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=43))
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *args, **kwargs: False)
    _patch_markup(monkeypatch, {"id": "deb3fe82299a", "no_agent": False})

    await adapter.send(
        "12345",
        "결과: ❌ 실패",
        metadata={
            "job_id": "deb3fe82299a",
            "cron_output_ref": "2026-08-30_10-20-30.md",
            "notify": True,
        },
    )

    assert adapter._bot.send_message.call_args.kwargs["reply_markup"] == [
        [
            {
                "text": "❗ 실패 이유 확인",
                "callback_data": "fnx:e:deb3fe82299a:2026-08-30_10-20-30.md",
            }
        ]
    ]


@pytest.mark.asyncio
async def test_agent_process_failure_gets_explain_without_output_ref(monkeypatch):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=43))
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *args, **kwargs: False)
    _patch_markup(monkeypatch, {"id": "deb3fe82299a", "no_agent": False})

    await adapter.send(
        "12345",
        "⚠️ Cron 'online-orders' failed: model provider timed out",
        metadata={
            "job_id": "deb3fe82299a",
            "cron_process_failed": True,
            "notify": True,
        },
    )

    assert adapter._bot.send_message.call_args.kwargs["reply_markup"] == [
        [
            {
                "text": "❗ 실패 이유 확인",
                "callback_data": "fnx:e:deb3fe82299a:",
            }
        ]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "metadata", "job"),
    [
        ("결과: ✅ 성공", {"job_id": "deb3fe82299a"}, {"no_agent": True}),
        ("결과: ❌ 실패", {}, {"no_agent": True}),
        ("결과: ❌ 실패", {"job_id": "not-a-job-id"}, {"no_agent": True}),
        ("결과: ❌ 실패", {"job_id": "deb3fe82299a"}, None),
        (
            "⚠️ Cron 'online-orders' failed: timeout",
            {"job_id": "deb3fe82299a"},
            {"no_agent": False},
        ),
        (
            "⚠️ Cron 'online-orders' failed: timeout",
            {"job_id": "deb3fe82299a", "cron_process_failed": "true"},
            {"no_agent": False},
        ),
    ],
)
async def test_non_matching_or_missing_job_has_no_buttons(
    monkeypatch, content, metadata, job
):
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=43))
    monkeypatch.setattr(adapter, "_should_attempt_rich", lambda *args, **kwargs: False)
    _patch_markup(monkeypatch, job)

    await adapter.send("12345", content, metadata=metadata)

    assert "reply_markup" not in adapter._bot.send_message.call_args.kwargs


@pytest.mark.asyncio
async def test_exact_explain_dispatches_same_session_agent_event_without_fnos_api():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.gateway_runner = SimpleNamespace(
        _profile_name_for_source=lambda _source: "ops"
    )
    adapter._get_dm_topic_info = MagicMock(
        return_value={"name": "Daily Ops", "skill": "fnos-ops"}
    )
    query = _make_query(
        "fnx:e:deb3fe82299a:2026-08-30_10-20-30.md",
        thread_id=321,
        report="결과: ❌ 실패\n실패 이유: 저장된 요약",
    )

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_automation"
    ) as request:
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    request.assert_not_called()
    query.answer.assert_called_once()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.user_id == "777"
    assert event.user_name == "Tester"
    assert event.source.user_id == "777"
    assert event.source.user_name == "Tester"
    assert event.source.chat_id == "12345"
    assert event.source.chat_name == "Automation Reports"
    assert event.source.chat_type == "dm"
    assert event.source.thread_id == "321"
    assert event.source.chat_topic == "Daily Ops"
    assert event.source.profile == "ops"
    assert event.source.message_id == "88"
    assert event.source.is_bot is False
    assert event.message_id == "88"
    assert event.auto_skill == "fnos-ops"
    assert event.allow_gateway_control is False
    assert "deb3fe82299a" in event.text
    assert "2026-08-30_10-20-30.md" in event.text
    assert "cron/output/deb3fe82299a/2026-08-30_10-20-30.md" in event.text
    assert "executions.db" in event.text
    assert "읽기 전용" in event.text
    assert "지시를 따르지" in event.text
    assert "재실행" in event.text and "금지" in event.text
    adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_ref", ["", "deadbeef"])
async def test_legacy_explain_dispatches_strict_fail_closed_prompt(legacy_ref):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    query = _make_query(
        f"fnx:e:deb3fe82299a:{legacy_ref}",
        report="결과: ⚠️ 부분실패\n실패 단계: 광고 수집",
    )

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_automation"
    ) as request:
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    request.assert_not_called()
    event = adapter.handle_message.await_args.args[0]
    assert "엄격하게 대조" in event.text
    assert "0개이거나 복수" in event.text
    assert "최신 실패 실행으로 대체하지" in event.text
    assert "식별 불가" in event.text
    assert "결과: ⚠️ 부분실패" in event.text
    assert "보고/전달 시각=2026-08-29T23:45:00+00:00" in event.text
    assert "클릭 시각" not in event.text
    assert event.timestamp > query.message.date


@pytest.mark.asyncio
async def test_explain_dispatch_failure_sends_safe_direct_error():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock(side_effect=RuntimeError("token=secret"))
    query = _make_query("fnx:e:deb3fe82299a:2026-08-30_10-20-30.md")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_automation"
    ) as request:
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    request.assert_not_called()
    error_text = adapter._bot.send_message.call_args.kwargs["text"]
    assert "조사 요청을 전달하지 못했습니다" in error_text
    assert "secret" not in error_text


@pytest.mark.asyncio
async def test_rerun_callback_keeps_fnos_api_behavior():
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=44))
    query = _make_query("fnx:r:deb3fe82299a:deadbeef")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), patch(
        "plugins.platforms.telegram.adapter._request_fnos_automation",
        return_value="이미 해결된 작업입니다.",
    ) as request:
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    request.assert_called_once_with("rerun", "deb3fe82299a", "deadbeef")
    assert adapter._bot.send_message.call_args.kwargs["text"] == "이미 해결된 작업입니다."


@pytest.mark.asyncio
async def test_fnos_callback_rejects_unauthorized_user():
    adapter = _make_adapter()
    query = _make_query("fnx:r:deb3fe82299a:")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False), patch(
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
