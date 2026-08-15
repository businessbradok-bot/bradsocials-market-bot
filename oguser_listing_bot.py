"""
OGUsers Listing Formatter Bot + Multi-Source Pastebin Watcher
----------------------------------------------------------------
1) Send: @handle price   (e.g. "@cooltag 250")
   Bot replies with a ready-to-copy single-listing OGUsers post.

2) Send: /template  (or /template <source label>)
   Bot replies with your standard catalog post template, ready for
   you to paste usernames into by hand.

3) Bot polls one or more Pastebin raw URLs on an interval, and when a
   paste changes it just notifies you with the added/removed lines -
   it does NOT auto-generate a post. Use /template when you're ready
   to make a new listing.

Env vars needed (set in Railway):
  TELEGRAM_BOT_TOKEN     - from @BotFather
  ALLOWED_USER_ID        - your Telegram user ID (restricts who can use the bot)
  PASTEBIN_RAW_URLS      - comma-separated raw paste URLs, optionally
                           "label=url" pairs, e.g.:
                           "buddy1=https://pastebin.com/raw/AbCdEfGh,https://pastebin.com/raw/ZzYyXx"
                           (unlabelled URLs are just shown as-is)
  PASTEBIN_POLL_SECONDS  - optional, defaults to 60

Deploy the same way as your other Railway bots:
  requirements.txt -> python-telegram-bot[job-queue]==21.*, requests
  Procfile / start command -> python oguser_listing_bot.py
"""

import os
import re
import logging
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
PASTEBIN_POLL_SECONDS = int(os.environ.get("PASTEBIN_POLL_SECONDS", "60"))

CONTACT_LINK = "http://t.me/bradsocials"
MIDDLEMAN_LINK = "https://oguser.com/Laugh"
FULL_LIST_LINK = "https://t.me/bradsocialsmarket"

# Matches "@handle price" or "handle price", price can have $ / commas.
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

# ---------- Formatting helpers ----------

def format_price(price: str) -> str:
    """Formats a raw price string with thousands separators, e.g. '1200' -> '1,200'."""
    clean = price.replace(",", "")
    try:
        if "." in clean:
            whole, frac = clean.split(".", 1)
            return f"{int(whole):,}.{frac}"
        return f"{int(clean):,}"
    except ValueError:
        return price  # fall back to whatever was given


def try_parse_line(line: str):
    """Attempts to parse a line as 'handle price'. Returns
    (handle, price) or None if it doesn't match."""
    match = INPUT_PATTERN.match(line.strip())
    if not match:
        return None
    return match.group("handle"), match.group("price")


def build_single_listing(handle: str, price: str) -> tuple[str, str]:
    """Formats a single standalone OGUsers-style sale thread (title + body),
    used for one-off manual @handle price messages."""
    clean_price = format_price(price)
    title = f"[Selling] @{handle} - ${clean_price}"

    body = f"""{title}

Handle: @{handle}
Price: ${clean_price} (OBO)
Payment: PayPal F&F / Crypto
Proof of ownership: available on request
Delivery: instant after payment confirmed

DM or comment to purchase. First come first served."""

    return title, body


def build_template() -> str:
    """Formats the blank catalog post template, ready for you to fill in
    usernames by hand. The FULL LIST HERE link always points at your
    Telegram market channel."""
    return f"""A trusted middleman is always used during the sale of usernames to ensure proper business.

[👉 Click here to contact me 👈]({CONTACT_LINK})

Other usernames I have include:


@ 
@ 
@ 

[FULL LIST HERE]({FULL_LIST_LINK})

@[Laugh]({MIDDLEMAN_LINK}) middleman is preferred.

All prices are negotiable.

I am open to enquires / questions."""


# ---------- Command handlers ----------

async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        return

    template = build_template()
    await update.message.reply_text(
        template, parse_mode="Markdown", disable_web_page_preview=True
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        return  # silently ignore anyone else

    text = update.message.text.strip()
    parsed = try_parse_line(text)

    if not parsed:
        await update.message.reply_text(
            "Didn't catch that. Send it like:\n@handle 250\n\n"
            "Or use /template to get a blank listing template."
        )
        return

    handle, price = parsed
    title, body = build_single_listing(handle, price)

    reply = f"Title:\n{title}\n\nBody:\n{body}"
    await update.message.reply_text(reply)


# ---------- Pastebin watcher (notify only, no auto-draft) ----------

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

    msg_parts = [f"[{label}] list updated. Use /template when ready to post."]

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

    logger.info(
        f"[{label}] Change notified: {len(added)} added, {len(removed)} removed."
    )


async def check_all_sources(context: ContextTypes.DEFAULT_TYPE):
    for label, url in SOURCES.items():
        await check_one_source(context, label, url)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("template", template_command))
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
