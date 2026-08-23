"""Platzky notifier plugin delivering notifications to Telegram via the Bot API."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, Literal, cast

import requests
from platzky.attachment import Attachment, AttachmentSizeError
from platzky.notification_topics import NotificationTopic
from platzky.plugin.notifier import Notification, NotifierPluginBase
from pydantic import BaseModel, Field, SecretStr, field_validator

logger = logging.getLogger("platzky.telegram_notifier")

DEFAULT_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT = 30.0

# Bot API hard limits — see https://core.telegram.org/bots/api
MAX_MESSAGE_LENGTH = 4096
MAX_DOCUMENT_SIZE = 50 * 1024 * 1024  # 50MB

ParseMode = Literal["MarkdownV2", "HTML", "Markdown"]


class TelegramApiError(Exception):
    """Raised when the Telegram Bot API rejects a request or is unreachable."""


def chunk_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> Iterator[str]:
    """Split text into chunks no longer than ``limit``, preferring line boundaries.

    Telegram rejects messages longer than ``MAX_MESSAGE_LENGTH`` outright, so long
    notifications are delivered as several consecutive messages instead of failing.

    Args:
        text: The message text. Empty text yields nothing.
        limit: Maximum length of a single chunk.

    Yields:
        Chunks of ``text`` in order, each at most ``limit`` characters long.

    Raises:
        ValueError: If ``limit`` is not positive.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    remaining = text
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        yield remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n")


def _call(
    method: str,
    *,
    bot_token: str,
    api_base_url: str,
    timeout: float,
    data: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> dict[str, Any]:
    """Call a Bot API method and return its ``result`` envelope.

    The bot token is part of the request URL, so the URL is never logged.

    Args:
        method: Bot API method name, e.g. ``sendMessage``.
        bot_token: Bot token issued by BotFather.
        api_base_url: Base URL of the Bot API.
        timeout: Socket timeout in seconds.
        data: Form fields to send.
        files: Multipart files as ``{field: (filename, content, mime_type)}``.

    Returns:
        The decoded JSON response body.

    Raises:
        TelegramApiError: If the request fails, the body is not JSON, or ``ok`` is false.
    """
    url = f"{api_base_url.rstrip('/')}/bot{bot_token}/{method}"
    logger.debug("Calling Telegram method %s", method)
    try:
        response = requests.post(url, data=data, files=files, timeout=timeout)
    except requests.RequestException as e:
        raise TelegramApiError(f"Request to Telegram method '{method}' failed: {e}") from e

    try:
        payload: Any = response.json()
    except ValueError as e:
        raise TelegramApiError(
            f"Telegram method '{method}' returned a non-JSON body " f"(HTTP {response.status_code})"
        ) from e

    if not isinstance(payload, dict):
        raise TelegramApiError(
            f"Telegram method '{method}' returned an unexpected body (HTTP {response.status_code})"
        )

    body = cast("dict[str, Any]", payload)
    if not body.get("ok", False):
        description = body.get("description", "no description")
        raise TelegramApiError(
            f"Telegram method '{method}' failed with HTTP {response.status_code}: {description}"
        )
    return body


def send_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    parse_mode: ParseMode | None = None,
    disable_notification: bool = False,
) -> None:
    """Send a single text message to a chat.

    Args:
        bot_token: Bot token issued by BotFather.
        chat_id: Target chat id or ``@channelusername``.
        text: Message body; must not exceed ``MAX_MESSAGE_LENGTH``.
        api_base_url: Base URL of the Bot API.
        timeout: Socket timeout in seconds.
        parse_mode: Telegram formatting mode, or None for plain text.
        disable_notification: Deliver silently, without a notification sound.

    Raises:
        TelegramApiError: If the Bot API rejects the request.
    """
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": disable_notification,
    }
    if parse_mode is not None:
        data["parse_mode"] = parse_mode
    _call("sendMessage", bot_token=bot_token, api_base_url=api_base_url, timeout=timeout, data=data)
    logger.info("Sent Telegram message to chat %s", chat_id)


def send_document(
    *,
    bot_token: str,
    chat_id: str,
    attachment: Attachment,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    disable_notification: bool = False,
) -> None:
    """Upload an attachment to a chat as a document.

    Args:
        bot_token: Bot token issued by BotFather.
        chat_id: Target chat id or ``@channelusername``.
        attachment: The file to upload.
        api_base_url: Base URL of the Bot API.
        timeout: Socket timeout in seconds.
        disable_notification: Deliver silently, without a notification sound.

    Raises:
        AttachmentSizeError: If the attachment exceeds ``MAX_DOCUMENT_SIZE``.
        TelegramApiError: If the Bot API rejects the request.
    """
    size = len(attachment.content)
    if size > MAX_DOCUMENT_SIZE:
        raise AttachmentSizeError(attachment.filename, size, MAX_DOCUMENT_SIZE)
    _call(
        "sendDocument",
        bot_token=bot_token,
        api_base_url=api_base_url,
        timeout=timeout,
        data={"chat_id": chat_id, "disable_notification": disable_notification},
        files={"document": (attachment.filename, attachment.content, attachment.mime_type)},
    )
    logger.info("Sent Telegram document %s to chat %s", attachment.filename, chat_id)


class TelegramNotifierConfig(BaseModel):
    """Configuration for the Telegram notifier plugin."""

    bot_token: SecretStr = Field(description="Bot token issued by BotFather")
    chat_id: str = Field(
        description="Default chat id or @channelusername to deliver to", min_length=1
    )
    api_base_url: str = Field(
        default=DEFAULT_API_BASE_URL, description="Bot API base URL", min_length=1
    )
    timeout: float = Field(default=DEFAULT_TIMEOUT, description="Socket timeout in seconds", gt=0)
    parse_mode: ParseMode | None = Field(
        default=None, description="Telegram formatting mode, or null for plain text"
    )
    disable_notification: bool = Field(
        default=False, description="Deliver silently, without a notification sound"
    )

    @field_validator("chat_id", mode="before")
    @classmethod
    def _stringify_chat_id(cls, value: Any) -> Any:
        """Accept numeric chat ids from JSON config without requiring them to be quoted."""
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


class TelegramNotifierPlugin(NotifierPluginBase):
    """Telegram notification plugin for Platzky."""

    accepted_topics: frozenset[NotificationTopic] = frozenset({"security", "content", "general"})

    def __init__(self, config: dict[str, Any]) -> None:
        """Validate and store the plugin configuration.

        Args:
            config: Raw plugin configuration mapping (see ``TelegramNotifierConfig``).
        """
        super().__init__(config)
        self.config: TelegramNotifierConfig = TelegramNotifierConfig.model_validate(config)

    def notify(self, notification: Notification) -> None:
        """Deliver a notification, plus any attachments, to Telegram.

        ``notification.receivers`` are treated as chat ids and override the configured
        default chat; an empty set falls back to ``chat_id`` from the config.

        Args:
            notification: The notification payload to deliver.
        """
        config = self.config
        bot_token = config.bot_token.get_secret_value()
        chat_ids = sorted(notification.receivers) or [config.chat_id]
        attachments = sorted(notification.attachments, key=lambda a: a.filename)
        logger.info(
            "TelegramNotifierPlugin handling topic=%s for %d chat(s)",
            notification.topic,
            len(chat_ids),
        )

        for chat_id in chat_ids:
            for chunk in chunk_message(notification.message):
                send_message(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    text=chunk,
                    api_base_url=config.api_base_url,
                    timeout=config.timeout,
                    parse_mode=config.parse_mode,
                    disable_notification=config.disable_notification,
                )
            for attachment in attachments:
                send_document(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    attachment=attachment,
                    api_base_url=config.api_base_url,
                    timeout=config.timeout,
                    disable_notification=config.disable_notification,
                )
