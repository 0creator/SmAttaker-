"""
SmAttaker — Safe Telegram Message Editing
`query.edit_message_text(...)` raises `telegram.error.BadRequest` with
"Message is not modified" whenever the new text+markup are byte-for-
byte identical to what's already displayed (e.g. a user double-taps
the same button, or revisits a section with no new data). Every
handler in this bot called `edit_message_text` directly and
unprotected — so that specific, extremely common case silently failed
the whole callback with an unhandled exception, making the button
LOOK completely broken (nothing visibly happens, no error shown to the
user) even though the rest of the bot kept running fine.

This is the single shared fix for that entire class of "this button
doesn't work" reports across every menu in the bot.
"""
import logging
from telegram.error import BadRequest

logger = logging.getLogger("smattaker.bot.safe_edit")


async def safe_edit_message(query, text: str, parse_mode: str = None, reply_markup=None) -> bool:
    """
    Drop-in replacement for `await query.edit_message_text(...)`.

    Returns True if the message was actually changed, False if Telegram
    rejected it as a no-op (content identical) — callers generally don't
    need to check the return value, it's there for the rare case a
    handler wants to react differently when nothing changed.

    ⚠️ FIX: previously any BadRequest that wasn't "message is not
    modified" (most commonly a Markdown parse error caused by an
    unescaped `_`, `*`, `[` or `` ` `` in user-supplied text like a
    Telegram username) was re-raised. Because the callback router had
    no try/except around the handler, that exception killed the whole
    callback silently — the button looked completely dead even though
    the data was fine. Now Markdown parse errors are retried once as
    plain text (no parse_mode) so the user at least sees *something*,
    and only truly unexpected errors are re-raised.
    """
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            # Not an error — the button worked, there's just nothing new
            # to show. Silently succeed instead of crashing the callback.
            return False
        # Markdown / parse errors are extremely common when user-generated
        # text (usernames, bios) contains characters that Telegram's
        # strict Markdown parser chokes on. Retry once as plain text so
        # the button still shows *something* instead of looking broken.
        if parse_mode and ("can't parse" in msg or "parse" in msg
                           or "entity" in msg or "markdown" in msg):
            logger.warning(
                f"Markdown parse failed ({e}); retrying as plain text. "
                f"First 120 chars: {text[:120]!r}"
            )
            try:
                await query.edit_message_text(text, parse_mode=None, reply_markup=reply_markup)
                return True
            except BadRequest as e2:
                if "message is not modified" in str(e2).lower():
                    return False
                logger.error(f"plain-text fallback also failed: {e2}")
                raise
        # Any OTHER BadRequest (message too old to edit, chat not found,
        # etc.) is a real problem — don't swallow it, just log with
        # enough context to debug, then re-raise so it's still visible
        # in error tracking rather than silently disappearing.
        logger.error(f"edit_message_text failed (not a no-op): {e}")
        raise
