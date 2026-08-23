# platzky-telegram-notifier

Platzky notifier plugin that delivers notifications — and their attachments — to a Telegram
chat, channel, or group via the [Bot API](https://core.telegram.org/bots/api).

## Installation

```sh
pip install platzky_telegram_notifier
```

## Setting up the Telegram bot

You need two values: a bot token and the id of the chat to post into.

### 1. Create the bot

Open [@BotFather](https://t.me/botfather) in Telegram and send `/newbot`. It asks for a display
name and then a username ending in `bot` (e.g. `platzky_notifier_bot`). It replies with the
token, which looks like `123456789:AAH...`. That is the `bot_token` config value; treat it as a
secret — anyone holding it controls the bot.

`/token` reissues it and `/revoke` invalidates the old one, both via BotFather.

### 2. Give the bot access to the target chat

Which step applies depends on where you want notifications to land:

- **Direct message to a person** — that person must message the bot first (`/start`). Bots
  cannot open a conversation.
- **Group** — add the bot as a member. No admin rights needed to post.
- **Channel** — add the bot as an **administrator** with the *Post Messages* permission.
  Ordinary members cannot post to a channel.

### 3. Find the chat id

For a **public channel** you can skip this and use `@channelusername` directly as `chat_id`.

Otherwise, send any message in the target chat, then ask the Bot API what it saw:

```sh
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

Read `result[].message.chat.id` (or `result[].channel_post.chat.id` for channels) from the
response. Ids are positive for direct messages, negative for groups, and start with `-100` for
supergroups and channels.

Two things that commonly make `getUpdates` come back empty:

- **Group privacy mode.** By default a bot in a group only receives messages that start with a
  command, so send `/start@your_bot_name` in the group rather than a plain "hi". (You can also
  turn privacy off via BotFather → `/setprivacy`, but you don't need to just to send.)
- **A webhook is set.** `getUpdates` and webhooks are mutually exclusive; call
  `https://api.telegram.org/bot<TOKEN>/deleteWebhook` first if this bot is used elsewhere.

### 4. Check it works

```sh
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=-1001234567890 \
  -d text="hello from platzky"
```

`{"ok":true,...}` means the token, membership, and chat id are all good. `chat not found`
usually means the id is wrong or the bot was never added; `bot is not a member of the channel
chat` means step 2 was skipped.

## Activation

Add the plugin to the `plugins` list in your Platzky database configuration. The `name` must
match the entry-point key declared in `pyproject.toml`:

```json
{
    "plugins": [
        {
            "name": "telegram_notifier",
            "is_active": true,
            "allowed_topics": ["security", "general"],
            "config": {
                "bot_token": "123456789:AAH...",
                "chat_id": "-1001234567890"
            }
        }
    ]
}
```

`allowed_topics` is enforced by the Platzky engine and intersected with the topics the plugin
declares (`security`, `content`, `general`).

### Config keys

| Key | Required | Default | Meaning |
|---|---|---|---|
| `bot_token` | yes | — | Bot token from BotFather (step 1) |
| `chat_id` | yes | — | Default target: numeric chat id or `@channelusername`. Numbers may be unquoted |
| `api_base_url` | no | `https://api.telegram.org` | Override for a local Bot API server |
| `timeout` | no | `30` | Socket timeout in seconds |
| `parse_mode` | no | `null` | `MarkdownV2`, `HTML`, or `Markdown`. Plain text when unset |
| `disable_notification` | no | `false` | Deliver silently |

Leave `parse_mode` unset unless you control the message text: Telegram rejects a message whose
markup is malformed, and `MarkdownV2` requires escaping a long list of punctuation characters.

## Behaviour

- `notification.receivers` are treated as chat ids and **override** the configured `chat_id`.
  An empty set — the default — falls back to `chat_id`.
- Messages longer than Telegram's 4096-character limit are split into consecutive messages on
  line boundaries rather than being rejected.
- Attachments are sent with `sendDocument` after the message text. Anything above the Bot API's
  50MB document limit raises `AttachmentSizeError` before the request is made.
- API and transport failures raise `TelegramApiError`. The bot token is part of the request URL
  and is never logged.

## Development

See the workspace `CLAUDE.md`. In short:

```sh
poetry install
make lint-check   # black, ruff, pyright (strict), interrogate
make coverage     # pytest + branch coverage (fails under 90%)
```
