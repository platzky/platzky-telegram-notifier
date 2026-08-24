from collections.abc import Iterator
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
    TelegramDeliveryError,
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


@pytest.fixture(autouse=True)
def no_sleep() -> Iterator[MagicMock]:
    """Keep retry backoff from actually sleeping, and expose the delays it asked for."""
    with patch("platzky_telegram_notifier.plugin.time.sleep") as mock_sleep:
        yield mock_sleep


def _ok_response(**payload: Any) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"ok": True, "result": {}, **payload}
    return response


def _error_response(status_code: int, **payload: Any) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"ok": False, **payload}
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
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello", max_retries=0)

    mock_post.assert_called_once()


def test_non_json_body_is_wrapped() -> None:
    response = MagicMock()
    response.status_code = 502
    response.json.side_effect = ValueError("not json")
    with patch("platzky_telegram_notifier.plugin.requests.post", return_value=response):
        with pytest.raises(TelegramApiError, match="non-JSON body"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello", max_retries=0)


def test_non_json_body_below_500_is_not_retried() -> None:
    response = MagicMock()
    response.status_code = 400
    response.json.side_effect = ValueError("not json")
    with patch(
        "platzky_telegram_notifier.plugin.requests.post", return_value=response
    ) as mock_post:
        with pytest.raises(TelegramApiError, match="non-JSON body"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    mock_post.assert_called_once()


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


# --- retries ---------------------------------------------------------------


def test_transport_errors_are_retried(no_sleep: MagicMock) -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.side_effect = [requests.ConnectionError("boom"), _ok_response()]
        send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    assert mock_post.call_count == 2
    no_sleep.assert_called_once_with(1.0)


def test_server_errors_are_retried_with_exponential_backoff(no_sleep: MagicMock) -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _error_response(503, description="Bad Gateway")
        with pytest.raises(TelegramApiError, match="Bad Gateway"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    assert mock_post.call_count == 3
    assert [call.args[0] for call in no_sleep.call_args_list] == [1.0, 2.0]


def test_rate_limits_wait_for_the_requested_delay(no_sleep: MagicMock) -> None:
    throttled = _error_response(
        429, error_code=429, description="Too Many Requests", parameters={"retry_after": 7}
    )
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.side_effect = [throttled, _ok_response()]
        send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    assert mock_post.call_count == 2
    no_sleep.assert_called_once_with(7.0)


def test_rate_limits_without_a_usable_hint_fall_back_to_backoff(no_sleep: MagicMock) -> None:
    throttled = _error_response(
        429, error_code=429, description="Too Many Requests", parameters={"retry_after": "soon"}
    )
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.side_effect = [throttled, _ok_response()]
        send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    no_sleep.assert_called_once_with(1.0)


def test_rate_limits_without_parameters_fall_back_to_backoff(no_sleep: MagicMock) -> None:
    throttled = _error_response(429, error_code=429, description="Too Many Requests")
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.side_effect = [throttled, _ok_response()]
        send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    no_sleep.assert_called_once_with(1.0)


def test_a_delay_above_the_cap_is_not_waited_out(no_sleep: MagicMock) -> None:
    throttled = _error_response(
        429, error_code=429, description="Too Many Requests", parameters={"retry_after": 900}
    )
    with patch(
        "platzky_telegram_notifier.plugin.requests.post", return_value=throttled
    ) as mock_post:
        with pytest.raises(TelegramApiError, match="Too Many Requests"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello", max_retry_after=60.0)

    mock_post.assert_called_once()
    no_sleep.assert_not_called()


def test_client_errors_are_not_retried(no_sleep: MagicMock) -> None:
    forbidden = _error_response(
        403, error_code=403, description="Forbidden: bot can't initiate conversation with a user"
    )
    with patch(
        "platzky_telegram_notifier.plugin.requests.post", return_value=forbidden
    ) as mock_post:
        with pytest.raises(TelegramApiError, match="initiate conversation"):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello")

    mock_post.assert_called_once()
    no_sleep.assert_not_called()


def test_retries_can_be_disabled(no_sleep: MagicMock) -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _error_response(500, description="Internal Server Error")
        with pytest.raises(TelegramApiError):
            send_message(bot_token=TOKEN, chat_id=CHAT_ID, text="Hello", max_retries=0)

    mock_post.assert_called_once()
    no_sleep.assert_not_called()


def test_document_uploads_are_retried_too(no_sleep: MagicMock) -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.side_effect = [_error_response(500, description="oops"), _ok_response()]
        send_document(bot_token=TOKEN, chat_id=CHAT_ID, attachment=_attachment())

    assert mock_post.call_count == 2


def test_retry_settings_come_from_the_plugin_config(no_sleep: MagicMock) -> None:
    plugin = TelegramNotifierPlugin(
        {"bot_token": TOKEN, "chat_id": CHAT_ID, "max_retries": 4, "max_retry_after": 60.0}
    )
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _error_response(500, description="oops")
        with pytest.raises(TelegramDeliveryError):
            plugin.notify(Notification(message="hi", topic="general"))

    assert mock_post.call_count == 5
    assert [call.args[0] for call in no_sleep.call_args_list] == [1.0, 2.0, 4.0, 8.0]


def test_backoff_stops_once_it_would_exceed_the_cap(no_sleep: MagicMock) -> None:
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _error_response(500, description="oops")
        with pytest.raises(TelegramApiError):
            send_message(
                bot_token=TOKEN,
                chat_id=CHAT_ID,
                text="Hello",
                max_retries=9,
                max_retry_after=3.0,
            )

    assert [call.args[0] for call in no_sleep.call_args_list] == [1.0, 2.0]
    assert mock_post.call_count == 3


# --- partial delivery failures ---------------------------------------------


def test_notify_keeps_delivering_after_a_chat_fails() -> None:
    plugin = TelegramNotifierPlugin({"bot_token": TOKEN, "chat_id": CHAT_ID})

    def respond(url: str, **kwargs: Any) -> MagicMock:
        if kwargs["data"]["chat_id"] == "111":
            return _error_response(403, error_code=403, description="bot was blocked by the user")
        return _ok_response()

    with patch("platzky_telegram_notifier.plugin.requests.post", side_effect=respond) as mock_post:
        with pytest.raises(TelegramDeliveryError) as excinfo:
            plugin.notify(
                Notification(message="hi", topic="general", receivers=frozenset({"111", "222"}))
            )

    assert [call.kwargs["data"]["chat_id"] for call in mock_post.call_args_list] == ["111", "222"]
    assert list(excinfo.value.failures) == ["111"]
    assert "bot was blocked by the user" in str(excinfo.value)


def test_delivery_error_collects_every_failed_chat() -> None:
    plugin = TelegramNotifierPlugin({"bot_token": TOKEN, "chat_id": CHAT_ID})
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _error_response(400, description="chat not found")
        with pytest.raises(TelegramDeliveryError) as excinfo:
            plugin.notify(
                Notification(message="hi", topic="general", receivers=frozenset({"111", "222"}))
            )

    assert sorted(excinfo.value.failures) == ["111", "222"]
    assert isinstance(excinfo.value, TelegramApiError)


def test_notify_skips_attachments_for_an_unreachable_chat() -> None:
    plugin = TelegramNotifierPlugin({"bot_token": TOKEN, "chat_id": CHAT_ID})
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _error_response(400, description="chat not found")
        with pytest.raises(TelegramDeliveryError):
            plugin.notify(
                Notification(
                    message="see attached",
                    topic="content",
                    attachments=frozenset({_attachment()}),
                )
            )

    methods = [call[0][0].rsplit("/", 1)[-1] for call in mock_post.call_args_list]
    assert methods == ["sendMessage"]


def test_notify_keeps_sending_attachments_after_one_fails() -> None:
    plugin = TelegramNotifierPlugin({"bot_token": TOKEN, "chat_id": CHAT_ID})
    oversized = _attachment("a-huge.pdf", b"x" * (MAX_DOCUMENT_SIZE + 1))
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        with pytest.raises(TelegramDeliveryError) as excinfo:
            plugin.notify(
                Notification(
                    message="see attached",
                    topic="content",
                    attachments=frozenset({oversized, _attachment("b-small.pdf")}),
                )
            )

    filenames = [
        call.kwargs["files"]["document"][0]
        for call in mock_post.call_args_list
        if call.kwargs.get("files")
    ]
    assert filenames == ["b-small.pdf"]
    assert isinstance(excinfo.value.failures[CHAT_ID][0], AttachmentSizeError)


def test_notify_raises_nothing_when_every_chat_succeeds() -> None:
    plugin = TelegramNotifierPlugin({"bot_token": TOKEN, "chat_id": CHAT_ID})
    with patch("platzky_telegram_notifier.plugin.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        plugin.notify(
            Notification(message="hi", topic="general", receivers=frozenset({"111", "222"}))
        )

    assert mock_post.call_count == 2
