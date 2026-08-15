"""
OGUsers Listing Formatter Bot + Multi-Source Pastebin Watcher
----------------------------------------------------------------
1) Send: @handle price   (e.g. "@cooltag 250")
   Bot replies with a ready-to-copy OGUsers listing.

2) Bot polls one or more Pastebin raw URLs on an interval, and when a
   paste changes it:
     - shows you exactly which lines were added/removed
     - auto-drafts a ready-to-copy OGUsers listing for every new line
       that parses as "handle price"
     - flags any new line it couldn't parse, so nothing gets missed

Env vars needed (set in Railway):
  TELEGRAM_BOT_TOKEN     - from @BotFather
  ALLOWED_USER_ID        - your Telegram user ID (restricts who can use the bot)
  PASTEBIN_RAW_URLS      - comma-separated raw paste URLs, optionally
                           "label=url" pairs, e.g.:
                           "buddy1=https://pastebin.com/raw/AbCdEfGh,https://pastebin.com/raw/ZzYyXx"
                           (unlabelled URLs are just shown as-is)
  PASTEBIN_POLL_SECONDS  - optional, defaults to 60

Deploy the same way as your other Railway bots:
  requirements.txt -> python-telegram-bot==21.*, requests
  Procfile / start command -> python oguser_listing_bot.py
"""

import os
import re
import hashlib
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
PASTEBIN_POLL_SECONDS = int(os.environ.get("PASTEBIN_POLL_SECONDS", "60"))

# Matches "@handle price" or "handle price", price can have $ / commas.
# Reused both for manual Telegram input and for parsing paste lines.
INPUT_PATTERN = re.compile(
    r"^@?(?P<handle>[A-Za-z0-9._]{1,30})\s+\$?(?P<price>[\d,]+(?:\.\d+)?)\s*$"
)


def parse_sources(raw: str) -> dict:
    """Parses PASTEBIN_RAW_URLS into {label: url}.
    Accepts "label=url" or bare "url" entries, comma-separated."""
    sources = {}
    if not raw:
        return sources
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            label, url = chunk.split("=", 1)
            label, url = label.strip(), url.strip()
        else:
            url = chunk
            label = url.rsplit("/", 1)[-1]  # short fallback label
        sources[label] = url
    return sources


SOURCES = parse_sources(os.environ.get("PASTEBIN_RAW_URLS", ""))

# ---------- Listing formatting ----------

def build_listing(handle: str, price: str) -> tuple[str, str]:
    """Formats a standard OGUsers-style sale thread body. Tweak this template
    freely to match whatever category/format you actually post under."""
    clean_price = price.replace(",", "")
    title = f"[Selling] @{handle} - ${clean_price}"

    body = f"""{title}

Handle: @{handle}
Price: ${clean_price} (OBO)
Payment: PayPal F&F / Crypto
Proof of ownership: available on request
Delivery: instant after payment confirmed

DM or comment to purchase. First come first served."""

    return title, body


def try_parse_line(line: str):
    """Attempts to parse a paste line as 'handle price'. Returns
    (handle, price) or None if it doesn't match."""
    match = INPUT_PATTERN.match(line.strip())
    if not match:
        return None
    return match.group("handle"), match.group("price")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        return  # silently ignore anyone else

    text = update.message.text.strip()
    parsed = try_parse_line(text)

    if not parsed:
        await update.message.reply_text(
            "Didn't catch that. Send it like:\n@handle 250"
        )
        return

    handle, price = parsed
    title, body = build_listing(handle, price)

    reply = f"Title:\n{title}\n\nBody:\n{body}"
    await update.message.reply_text(reply)


# ---------- Pastebin watcher ----------

def fetch_paste_lines(url: str):
    """Returns the paste's non-empty lines, or None on fetch failure."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
        return lines
    except requests.RequestException as e:
        logger.warning(f"Pastebin fetch failed for {url}: {e}")
        return None


def diff_lines(old_lines: list, new_lines: list):
    """Returns (added, removed) preserving order, based on set difference."""
    old_set, new_set = set(old_lines), set(new_lines)
    added = [ln for ln in new_lines if ln not in old_set]
    removed = [ln for ln in old_lines if ln not in new_set]
    return added, removed


async def check_one_source(context: ContextTypes.DEFAULT_TYPE, label: str, url: str):
    new_lines = fetch_paste_lines(url)
    if new_lines is None:
        return  # fetch failed, try again next interval

    store_key = f"paste_lines::{label}"
    old_lines = context.bot_data.get(store_key)

    if old_lines is None:
        # first run - baseline only, no notification
        context.bot_data[store_key] = new_lines
        logger.info(f"[{label}] Pastebin baseline set ({len(new_lines)} lines).")
        return

    if new_lines == old_lines:
        return  # no change

    added, removed = diff_lines(old_lines, new_lines)
    context.bot_data[store_key] = new_lines

    if not added and not removed:
        return  # order changed only, nothing meaningful to report

    msg_parts = [f"Update on [{label}]:"]

    if added:
        msg_parts.append(f"\n+ {len(added)} added:")
        for line in added:
            msg_parts.append(f"  + {line}")

    if removed:
        msg_parts.append(f"\n- {len(removed)} removed:")
        for line in removed:
            msg_parts.append(f"  - {line}")

    await context.bot.send_message(
        chat_id=ALLOWED_USER_ID, text="\n".join(msg_parts)
    )

    # Auto-draft listings for every new line that parses cleanly
    unparsed = []
    for line in added:
        parsed = try_parse_line(line)
        if parsed:
            handle, price = parsed
            title, body = build_listing(handle, price)
            await context.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text=f"Draft for {line}:\n\nTitle:\n{title}\n\nBody:\n{body}",
            )
        else:
            unparsed.append(line)

    if unparsed:
        unparsed_text = "\n".join(f"  - {ln}" for ln in unparsed)
        await context.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=(
                f"Couldn't auto-draft {len(unparsed)} new line(s) from "
                f"[{label}] (format didn't match 'handle price'):\n{unparsed_text}"
            ),
        )

    logger.info(
        f"[{label}] Change processed: {len(added)} added, {len(removed)} removed."
    )


async def check_all_sources(context: ContextTypes.DEFAULT_TYPE):
    for label, url in SOURCES.items():
        await check_one_source(context, label, url)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if SOURCES:
        app.job_queue.run_repeating(
            check_all_sources, interval=PASTEBIN_POLL_SECONDS, first=10
        )
        logger.info(
            f"Watching {len(SOURCES)} source(s) every {PASTEBIN_POLL_SECONDS}s: "
            f"{list(SOURCES.keys())}"
        )
    else:
        logger.info("PASTEBIN_RAW_URLS not set - watcher disabled.")

    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
