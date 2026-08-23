from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from platzky.attachment import Attachment, AttachmentSizeError
from platzky.plugin.notifier import Notification, NotifierPluginBase
from pydantic import ValidationError

from platzky_telegram_notifier.plugin import (
    MAX_DOCUMENT_SIZE,
    MAX_MESSAGE_LENGTH,
    TelegramApiError,
    TelegramNotifierPlugin,
    chunk_message,
    send_document,
    send_message,
)

TOKEN = "123456:test-token"
CHAT_ID = "-1001234567890"


@pytest.fixture
def valid_config() -> dict[str, Any]:
    return {"bot_token": TOKEN, "chat_id": CHAT_ID}


def _ok_response(**payload: Any) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"ok": True, "result": {}, **payload}
    return response


def _attachment(name: str = "report.pdf", content: bytes = b"pdf-bytes") -> Attachment:
    return Attachment(filename=name, content=content, mime_type="application/pdf")


# --- chunk_message ---------------------------------------------------------


def test_short_message_is_a_single_chunk() -> None:
    assert list(chunk_message("hello")) == ["hello"]


def test_empty_message_yields_nothing() -> None:
    assert list(chunk_message("")) == []


def test_long_message_splits_on_newline_boundary() -> None:
    text = "a" * 10 + "\n" + "b" * 10
    assert list(chunk_message(text, limit=15)) == ["a" * 10, "b" * 10]


def test_long_message_without_newline_splits_at_limit() -> None:
    chunks = list(chunk_message("x" * 25, limit=10))
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]


def test_chunks_never_exceed_the_api_limit() -> None:
    chunks = list(chunk_message("y" * (MAX_MESSAGE_LENGTH * 2 + 7)))
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == "y" * (MAX_MESSAGE_LENGTH * 2 + 7)


def test_non_positive_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        list(chunk_message("hello", limit=0))


# --- send_message ----------------------------------------------------------


def test_send_message_posts_to_the_bot_api() -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    (url,) = mock_post.call_args[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert mock_post.call_args.kwargs["data"] == {
        "chat_id": CHAT_ID,
        "text": "Hello",
        "disable_notification": False,
    }


def test_send_message_includes_parse_mode_only_when_set() -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="*hi*", parse_mode="MarkdownV2")

    assert mock_post.call_args.kwargs["data"]["parse_mode"] == "MarkdownV2"


def test_send_message_honours_custom_base_url_and_timeout() -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        send_message(
            bot_token=TOKEN,
            chat_id=CHAT_ID,
            text="Hello",
            api_base_url="http://localhost:8081/",
            timeout=5.0,
        )

    assert mock_post.call_args[0][0] == f"http://localhost:8081/bot{TOKEN}/sendMessage"
    assert mock_post.call_args.kwargs["timeout"] == 5.0


def test_network_failure_is_wrapped() -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("boom")
        with pytest.raises(TelegramApiError, match="sendMessage"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")


def test_non_json_body_is_wrapped() -> None:
    response = MagicMock()
    response.status_code = 502
    response.json.side_effect = ValueError("not json")
    with patch("platzky_telegram_notifier.plugin.requests.post", return_value=response):
        with pytest.raises(TelegramApiError, match="non-JSON body"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")


def test_non_object_body_is_rejected() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = ["unexpected"]
    with patch("platzky_telegram_notifier.plugin.requests.post", return_value=response):
        with pytest.raises(TelegramApiError, match="unexpected body"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")


def test_api_level_failure_surfaces_the_description() -> None:
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {"ok": False, "description": "chat not found"}
    with patch("platzky_telegram_notifier.plugin.requests.post", return_value=response):
        with pytest.raises(TelegramApiError, match="chat not found"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")


def test_api_level_failure_without_description() -> None:
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {"ok": False}
    with patch("platzky_telegram_notifier.plugin.requests.post", return_value=response):
        with pytest.raises(TelegramApiError, match="no description"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")


def test_the_bot_token_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="platzky.telegram_notifier")
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    assert TOKEN not in caplog.text


# --- send_document ---------------------------------------------------------


def test_send_document_uploads_the_attachment() -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        send_document(bot_token=TOKEN, chat_id=CHAT_ID, attachment=_attachment())

    assert mock_post.call_args[0][0] == f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    assert mock_post.call_args.kwargs["files"] == {
        "document": ("report.pdf", b"pdf-bytes", "application/pdf")
    }
    assert mock_post.call_args.kwargs["data"]["chat_id"] == CHAT_ID


def test_oversized_document_is_rejected_before_the_request() -> None:
    oversized = _attachment(content=b"x" * (MAX_DOCUMENT_SIZE + 1))
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        with pytest.raises(AttachmentSizeError, match=r"report\.pdf"):
            send_document(bot_token=TOKEN, chat_id=CHAT_ID, attachment=oversized)
    mock_post.assert_not_called()


# --- plugin ----------------------------------------------------------------


def test_plugin_is_a_platzky_notifier(valid_config: dict[str, Any]) -> None:
    plugin = TelegramNotifierPlugin(valid_config)
    assert isinstance(plugin, NotifierPluginBase)
    assert plugin.accepted_topics == frozenset({"security", "content", "general"})


def test_plugin_rejects_incomplete_config() -> None:
    with pytest.raises(ValidationError, match="chat_id"):
        TelegramNotifierPlugin({"bot_token": TOKEN})


def test_plugin_accepts_a_numeric_chat_id() -> None:
    plugin = TelegramNotifierPlugin({"bot_token": TOKEN, "chat_id": -1001234567890})
    assert plugin.config.chat_id == "-1001234567890"


def test_plugin_hides_the_token_from_reprs(valid_config: dict[str, Any]) -> None:
    plugin = TelegramNotifierPlugin(valid_config)
    assert TOKEN not in repr(plugin.config)


def test_notify_sends_to_the_configured_chat(valid_config: dict[str, Any]) -> None:
    plugin = TelegramNotifierPlugin(valid_config)
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        plugin.notify(Notification(message="Deploy finished", topic="general"))

    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["data"]["chat_id"] == CHAT_ID
    assert mock_post.call_args.kwargs["data"]["text"] == "Deploy finished"


def test_notify_prefers_explicit_receivers(valid_config: dict[str, Any]) -> None:
    plugin = TelegramNotifierPlugin(valid_config)
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        plugin.notify(
            Notification(message="hi", topic="security", receivers=frozenset({"111", "222"}))
        )

    targets = [call.kwargs["data"]["chat_id"] for call in mock_post.call_args_list]
    assert targets == ["111", "222"]


def test_notify_splits_long_messages(valid_config: dict[str, Any]) -> None:
    plugin = TelegramNotifierPlugin(valid_config)
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        plugin.notify(Notification(message="z" * (MAX_MESSAGE_LENGTH + 10), topic="general"))

    assert mock_post.call_count == 2


def test_notify_sends_attachments_after_the_message(valid_config: dict[str, Any]) -> None:
    plugin = TelegramNotifierPlugin(valid_config)
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        plugin.notify(
            Notification(
                message="see attached",
                topic="content",
                attachments=frozenset({_attachment("b.pdf"), _attachment("a.pdf")}),
            )
        )

    methods = [call[0][0].rsplit("/", 1)[-1] for call in mock_post.call_args_list]
    assert methods == ["sendMessage", "sendDocument", "sendDocument"]
    filenames = [
        call.kwargs["files"]["document"][0]
        for call in mock_post.call_args_list
        if call.kwargs.get("files")
    ]
    assert filenames == ["a.pdf", "b.pdf"]


def test_notify_with_only_attachments_sends_no_message(valid_config: dict[str, Any]) -> None:
    plugin = TelegramNotifierPlugin(valid_config)
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        plugin.notify(
            Notification(message="", topic="general", attachments=frozenset({_attachment()}))
        )

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0].endswith("sendDocument")
