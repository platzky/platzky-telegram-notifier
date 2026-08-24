"""Platzky notifier plugin delivering notifications to Telegram via the Bot API."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal, TypeVar, cast

import requests
from platzky.attachment import Attachment, AttachmentSizeError
from platzky.notification_topics import NotificationTopic
from platzky.plugin.notifier import Notification, NotifierPluginBase
from pydantic import BaseModel, Field, SecretStr, field_validator

logger = logging.getLogger("platzky.telegram_notifier")

DEFAULT_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RETRY_AFTER = 60.0

# Bot API hard limits — see https://core.telegram.org/bots/api
MAX_MESSAGE_LENGTH = 4096
MAX_DOCUMENT_SIZE = 50 * 1024 * 1024  # 50MB

ParseMode = Literal["MarkdownV2", "HTML", "Markdown"]

T = TypeVar("T")


class TelegramApiError(Exception):
    """Raised when the Telegram Bot API rejects a request or is unreachable."""


class TelegramDeliveryError(TelegramApiError):
    """Raised when a notification could not be delivered to every target chat.

    Delivery is attempted for all chats before this is raised, so a single unreachable
    chat never stops the others from receiving the notification.

    Attributes:
        failures: Errors collected per chat id, in delivery order.
    """

    def __init__(self, failures: dict[str, list[Exception]]) -> None:
        """Build the aggregate error from the per-chat failures collected so far.

        Args:
            failures: Errors collected per chat id.
        """
        self.failures = failures
        details = "; ".join(
            f"{chat_id}: {', '.join(str(error) for error in errors)}"
            for chat_id, errors in failures.items()
        )
        super().__init__(f"Telegram delivery failed for {len(failures)} chat(s): {details}")


class _RetryableError(Exception):
    """Internal marker for a failed attempt that is worth repeating.

    Attributes:
        error: The error to surface once retries are exhausted.
        retry_after: Delay the API asked for, in seconds, or None to back off instead.
    """

    def __init__(self, error: TelegramApiError, retry_after: float | None = None) -> None:
        """Wrap the underlying error together with the API's own retry hint.

        Args:
            error: The error to surface once retries are exhausted.
            retry_after: Delay the API asked for, in seconds, if it supplied one.
        """
        super().__init__(str(error))
        self.error = error
        self.retry_after = retry_after


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


def _redact(text: str, bot_token: str) -> str:
    """Blank out the bot token wherever it appears in a message.

    ``requests`` embeds the full request URL — bot token included — in its exception
    messages, and those messages reach the logs and ``TelegramDeliveryError``.

    Args:
        text: The message to scrub.
        bot_token: The token to remove.

    Returns:
        ``text`` with every occurrence of ``bot_token`` replaced by a placeholder.
    """
    return text.replace(bot_token, "<bot_token>")


def _retry_after(body: dict[str, Any]) -> float | None:
    """Read Telegram's ``parameters.retry_after`` hint out of a failed response body.

    Args:
        body: The decoded response body of a call that returned ``ok: false``.

    Returns:
        The requested delay in seconds, or None if the body carries no usable hint.
    """
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    value = cast("dict[str, Any]", parameters).get("retry_after")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # A negative hint would reach time.sleep() and raise ValueError out of notify().
    return max(0.0, float(value))


def _with_retries(
    description: str,
    operation: Callable[[], T],
    *,
    max_retries: int,
    max_retry_after: float,
) -> T:
    """Run ``operation``, repeating attempts that fail transiently.

    Rate limits (HTTP 429) are retried after the delay Telegram asks for; transport
    errors and 5xx responses back off exponentially: 1s, 2s, 4s, … A requested delay
    above ``max_retry_after`` is not waited out — the call fails instead of blocking
    the caller for minutes.

    Args:
        description: Human-readable name of the operation, used in log messages.
        operation: The callable to run; raises ``_RetryableError`` to ask for a retry.
        max_retries: How many additional attempts to make after the first one.
        max_retry_after: Longest delay, in seconds, worth waiting before giving up.

    Returns:
        Whatever ``operation`` returns on its first successful attempt.

    Raises:
        TelegramApiError: If the operation fails and no further retry is made.
    """
    attempt = 0
    while True:
        try:
            return operation()
        except _RetryableError as retryable:
            if attempt >= max_retries:
                logger.warning("%s failed after %d attempt(s), giving up", description, attempt + 1)
                raise retryable.error from retryable.__cause__

            requested = retryable.retry_after
            if requested is None:
                # Backoff is our own heuristic, so trimming it to the cap is harmless.
                delay = min(float(2**attempt), max_retry_after)  # 1s, 2s, 4s, …
            elif requested > max_retry_after:
                # Retrying sooner than Telegram asked for only earns another rejection.
                logger.warning(
                    "%s asked to wait %.1fs, above the %.1fs cap; giving up",
                    description,
                    requested,
                    max_retry_after,
                )
                raise retryable.error from retryable.__cause__
            else:
                delay = requested

            logger.warning("%s failed (%s); retrying in %.1fs", description, retryable, delay)
            time.sleep(delay)
            attempt += 1


def _request_once(
    method: str,
    url: str,
    *,
    bot_token: str,
    timeout: float,
    data: dict[str, Any] | None,
    files: dict[str, tuple[str, bytes, str]] | None,
) -> dict[str, Any]:
    """Make a single Bot API request and return its decoded body.

    Args:
        method: Bot API method name, e.g. ``sendMessage``.
        url: Fully built method URL, including the bot token.
        bot_token: The token to scrub out of any error message.
        timeout: Socket timeout in seconds.
        data: Form fields to send.
        files: Multipart files as ``{field: (filename, content, mime_type)}``.

    Returns:
        The decoded JSON response body of a successful call.

    Raises:
        _RetryableError: If the failure looks transient — a transport error, a 5xx
            response, or a 429 rate limit.
        TelegramApiError: If the call failed in a way that retrying cannot fix.
    """
    try:
        response = requests.post(url, data=data, files=files, timeout=timeout)
    except requests.RequestException as e:
        raise _RetryableError(
            TelegramApiError(
                f"Request to Telegram method '{method}' failed: {_redact(str(e), bot_token)}"
            )
        ) from e

    status_code = response.status_code
    try:
        payload: Any = response.json()
    except ValueError as e:
        non_json = TelegramApiError(
            f"Telegram method '{method}' returned a non-JSON body (HTTP {status_code})"
        )
        if status_code >= 500:
            raise _RetryableError(non_json) from e
        raise non_json from e

    if not isinstance(payload, dict):
        unexpected = TelegramApiError(
            f"Telegram method '{method}' returned an unexpected body (HTTP {status_code})"
        )
        if status_code >= 500:
            raise _RetryableError(unexpected)
        raise unexpected

    body = cast("dict[str, Any]", payload)
    if body.get("ok", False):
        return body

    description = body.get("description", "no description")
    error = TelegramApiError(
        f"Telegram method '{method}' failed with HTTP {status_code}: {description}"
    )
    if status_code == 429 or body.get("error_code") == 429:
        raise _RetryableError(error, retry_after=_retry_after(body))
    if status_code >= 500:
        raise _RetryableError(error)
    raise error


def _call(
    method: str,
    *,
    bot_token: str,
    api_base_url: str,
    timeout: float,
    data: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
) -> dict[str, Any]:
    """Call a Bot API method, with retries, and return its ``result`` envelope.

    The bot token is part of the request URL, so the URL is never logged.

    Args:
        method: Bot API method name, e.g. ``sendMessage``.
        bot_token: Bot token issued by BotFather.
        api_base_url: Base URL of the Bot API.
        timeout: Socket timeout in seconds.
        data: Form fields to send.
        files: Multipart files as ``{field: (filename, content, mime_type)}``.
        max_retries: How many additional attempts to make after a transient failure.
        max_retry_after: Longest rate-limit delay, in seconds, worth waiting out.

    Returns:
        The decoded JSON response body.

    Raises:
        TelegramApiError: If the request fails, the body is not JSON, or ``ok`` is false.
    """
    url = f"{api_base_url.rstrip('/')}/bot{bot_token}/{method}"
    logger.debug("Calling Telegram method %s", method)
    return _with_retries(
        f"Telegram method '{method}'",
        lambda: _request_once(
            method, url, bot_token=bot_token, timeout=timeout, data=data, files=files
        ),
        max_retries=max_retries,
        max_retry_after=max_retry_after,
    )


def send_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    parse_mode: ParseMode | None = None,
    disable_notification: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
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
        max_retries: How many additional attempts to make after a transient failure.
        max_retry_after: Longest rate-limit delay, in seconds, worth waiting out.

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
    _call(
        "sendMessage",
        bot_token=bot_token,
        api_base_url=api_base_url,
        timeout=timeout,
        data=data,
        max_retries=max_retries,
        max_retry_after=max_retry_after,
    )
    logger.info("Sent Telegram message to chat %s", chat_id)


def send_document(
    *,
    bot_token: str,
    chat_id: str,
    attachment: Attachment,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    disable_notification: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
) -> None:
    """Upload an attachment to a chat as a document.

    Args:
        bot_token: Bot token issued by BotFather.
        chat_id: Target chat id or ``@channelusername``.
        attachment: The file to upload.
        api_base_url: Base URL of the Bot API.
        timeout: Socket timeout in seconds.
        disable_notification: Deliver silently, without a notification sound.
        max_retries: How many additional attempts to make after a transient failure.
        max_retry_after: Longest rate-limit delay, in seconds, worth waiting out.

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
        max_retries=max_retries,
        max_retry_after=max_retry_after,
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
    max_retries: int = Field(
        default=DEFAULT_MAX_RETRIES,
        description="Extra attempts after a rate limit, 5xx, or transport failure",
        ge=0,
    )
    max_retry_after: float = Field(
        default=DEFAULT_MAX_RETRY_AFTER,
        description="Longest rate-limit delay in seconds worth waiting out",
        gt=0,
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

        Every target chat is attempted even when an earlier one fails, so one blocked
        or unknown chat cannot swallow the notification for the rest. A failing message
        skips the rest of that chat — remaining chunks and all attachments — since the
        chat is generally unreachable, while a single failing attachment does not stop
        the remaining ones. A message long enough to be split can therefore be left
        partially delivered if a later chunk fails.

        Args:
            notification: The notification payload to deliver.

        Raises:
            TelegramDeliveryError: If delivery to any chat failed, once all chats have
                been attempted.
        """
        config = self.config
        bot_token = config.bot_token.get_secret_value()
        chat_ids = sorted(notification.receivers) or [config.chat_id]
        attachments = sorted(notification.attachments, key=lambda a: a.filename)
        failures: dict[str, list[Exception]] = {}
        logger.info(
            "TelegramNotifierPlugin handling topic=%s for %d chat(s)",
            notification.topic,
            len(chat_ids),
        )

        for chat_id in chat_ids:
            try:
                for chunk in chunk_message(notification.message):
                    send_message(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        text=chunk,
                        api_base_url=config.api_base_url,
                        timeout=config.timeout,
                        parse_mode=config.parse_mode,
                        disable_notification=config.disable_notification,
                        max_retries=config.max_retries,
                        max_retry_after=config.max_retry_after,
                    )
            except TelegramApiError as e:
                logger.error("Telegram message to chat %s failed: %s", chat_id, e)
                failures.setdefault(chat_id, []).append(e)
                continue

            for attachment in attachments:
                try:
                    send_document(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        attachment=attachment,
                        api_base_url=config.api_base_url,
                        timeout=config.timeout,
                        disable_notification=config.disable_notification,
                        max_retries=config.max_retries,
                        max_retry_after=config.max_retry_after,
                    )
                except (TelegramApiError, AttachmentSizeError) as e:
                    logger.error(
                        "Telegram document %s to chat %s failed: %s",
                        attachment.filename,
                        chat_id,
                        e,
                    )
                    failures.setdefault(chat_id, []).append(e)

        if failures:
            raise TelegramDeliveryError(failures)
