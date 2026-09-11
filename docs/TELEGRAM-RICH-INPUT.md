# Incoming Telegram rich messages

Telegram may put user-authored paragraphs and lists in `Message.rich_message`
while leaving `Message.text` empty. PTB 22.8 retains this Bot API field in
`api_kwargs`; `filters.TEXT` alone silently ignores it. The ordinary text handler
now also admits that field after the same owner/chat access check. No library
upgrade, additional Telegram client, or provider-specific path is required.

The bounded parser projects text formatting, paragraphs/headings/code, lists,
checkboxes, tables, quotations and collapsed details to plain text. Link targets
are retained as inert text and never fetched. Code indentation is preserved.
Depth 24, 4,096 tree nodes, and 32,768 output characters bound conversion. Unknown
blocks (including rich embedded media/buttons) or malformed/oversized content
reject the entire input with a user-visible explanation, before approval parsing
or task admission. Unsupported content is not silently omitted from instructions.

Original updates remain unmodified. The existing `text` follow-up envelope stores
the complete PTB update, including `rich_message`, and re-parses it after reopen.
Normal text, access policy, approvals and per-conversation queues keep their
existing behavior. Historical ignored updates are not automatically replayed.

Contract checked 2026-09-11:
- https://core.telegram.org/bots/api#richmessage
- https://core.telegram.org/bots/api#richtext
- https://github.com/python-telegram-bot/python-telegram-bot/issues/5261

Tests use actual PTB `Update.de_json`, message filters, serialized queue reopen,
the real delivery mixin and generated rich-only inputs. They do not send live
Telegram messages or execute previously ignored user instructions.
