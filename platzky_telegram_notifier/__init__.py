from platzky_telegram_notifier.plugin import (
    MAX_DOCUMENT_SIZE,
    MAX_MESSAGE_LENGTH,
    TelegramApiError,
    TelegramDeliveryError,
    TelegramNotifierConfig,
    TelegramNotifierPlugin,
)

Plugin = TelegramNotifierPlugin

__all__ = [
    "MAX_DOCUMENT_SIZE",
    "MAX_MESSAGE_LENGTH",
    "Plugin",
    "TelegramApiError",
    "TelegramDeliveryError",
    "TelegramNotifierConfig",
    "TelegramNotifierPlugin",
]
