#!/usr/bin/env python3
"""
GemRadar - DexScreener new-token monitor with Telegram alerts.

Polls DexScreener for newly listed token profiles, filters them by chain and
liquidity, and pushes a formatted alert to a Telegram chat. Designed to run
forever as a single long-lived process (e.g. on Railway.app).
"""

import os
import sys
import time
import html
import logging

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
#
# TELEGRAM_BOT_TOKEN
#   The API token for your Telegram bot. You get this from @BotFather when you
#   create a bot (it looks like "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx").
#   Set it as an environment variable named TELEGRAM_BOT_TOKEN. On Railway you
#   add it under the service's "Variables" tab. Do NOT hard-code it here if you
#   plan to share the code.
#
# TELEGRAM_CHAT_ID
#   The ID of the chat (you, a group, or a channel) that should receive alerts.
#   For a personal chat this is your numeric user ID. For a channel it usually
#   starts with "-100...". See the README / setup notes for how to find it with
#   @userinfobot or the getUpdates method. Set it as the TELEGRAM_CHAT_ID
#   environment variable.
#
# Both values are read from environment variables so secrets stay out of the
# source. For quick local testing you can replace the os.getenv fallbacks below
# with your real values, but prefer environment variables.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# How often to poll DexScreener, in seconds.
POLL_INTERVAL = 30

# Only alert for tokens on these chains (DexScreener "chainId" values).
ALLOWED_CHAINS = {"ethereum", "base", "solana"}

# Minimum liquidity (in USD) required before we send an alert.
MIN_LIQUIDITY_USD = 10_000

# If True, ONLY alert on tokens that have a Telegram link (so you can join and
# pitch). Tokens without a Telegram are skipped entirely. Set to False if you'd
# rather receive every qualifying token and just see the Telegram line blank.
REQUIRE_TELEGRAM = True

# DexScreener endpoints.
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# Network timeout (seconds) for every outbound HTTP request.
REQUEST_TIMEOUT = 15

# Pretty chain labels for the alert message.
CHAIN_LABELS = {
    "ethereum": "Ethereum",
    "base": "Base",
    "solana": "Solana",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gemradar")

# Tokens we've already alerted on, so we never send a duplicate.
seen_tokens = set()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(message: str) -> bool:
    """Send an HTML-formatted message to the configured Telegram chat.

    Returns True on success, False on failure. Never raises so a Telegram
    hiccup can't crash the monitor loop.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return True
        log.error("Telegram API returned %s: %s", resp.status_code, resp.text[:300])
        return False
    except requests.RequestException as exc:
        log.error("Failed to send Telegram message: %s", exc)
        return False


def extract_socials(items) -> dict:
    """Pull telegram / twitter / website URLs out of a DexScreener links or
    socials array. Handles both the profile shape ({"type"/"label", "url"})
    and the pair "info.socials" / "info.websites" shapes.
    """
    out: dict = {}
    for item in items or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        kind = (item.get("type") or item.get("label") or "").lower()
        if "telegram" in kind or "t.me/" in url:
            out.setdefault("telegram", url)
        elif "twitter" in kind or kind == "x" or "x.com/" in url or "twitter.com/" in url:
            out.setdefault("twitter", url)
        elif "website" in kind or kind == "":
            out.setdefault("website", url)
    return out


def shorten_address(address: str) -> str:
    """Return a shortened form of a token address, e.g. 0x1234...abcd."""
    if not address or len(address) <= 12:
        return address or "unknown"
    return f"{address[:6]}...{address[-4:]}"


def format_price(price) -> str:
    """Format a USD price string for display, handling tiny values."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return "N/A"
    if value == 0:
        return "$0"
    if value >= 1:
        return f"${value:,.4f}"
    # Show more precision for sub-dollar prices.
    return f"${value:.8f}".rstrip("0").rstrip(".")


def build_alert(token: dict) -> str:
    """Build the HTML alert message for a token dict (see enrich_token)."""
    name = html.escape(token["name"])
    symbol = html.escape(token["symbol"])
    chain = CHAIN_LABELS.get(token["chain"], token["chain"].title())
    liquidity = f"${token['liquidity']:,.0f}"
    price = format_price(token["price"])
    short_addr = html.escape(shorten_address(token["address"]))
    link = html.escape(token["url"])
    socials = token.get("socials") or {}

    lines = [
        "🚨 <b>New Token Detected</b> 🚨\n",
        f"<b>Name:</b> {name}",
        f"<b>Symbol:</b> ${symbol}",
        f"<b>Chain:</b> {chain}",
        f"<b>Liquidity:</b> {liquidity}",
        f"<b>Price:</b> {price}",
        f"<b>Address:</b> <code>{short_addr}</code>",
    ]

    # Telegram front and center — this is the chat to join and pitch.
    tg = socials.get("telegram")
    if tg:
        tg_esc = html.escape(tg)
        lines.append(f'\n💬 <b>Telegram:</b> <a href="{tg_esc}">{tg_esc}</a>')
    else:
        lines.append("\n💬 <b>Telegram:</b> none listed")

    # Other socials as bonus context.
    extras = []
    if socials.get("twitter"):
        extras.append(f'<a href="{html.escape(socials["twitter"])}">Twitter/X</a>')
    if socials.get("website"):
        extras.append(f'<a href="{html.escape(socials["website"])}">Website</a>')
    if extras:
        lines.append("🌐 " + " | ".join(extras))

    lines.append(f'\n🔗 <a href="{link}">View on DexScreener</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DexScreener
# ---------------------------------------------------------------------------
def fetch_latest_profiles() -> list:
    """Fetch the latest token profiles. Returns a list (empty on error)."""
    try:
        resp = requests.get(PROFILES_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # The endpoint returns a JSON array of profile objects.
        if isinstance(data, list):
            return data
        log.warning("Unexpected profiles payload type: %s", type(data).__name__)
        return []
    except requests.RequestException as exc:
        log.error("Error fetching token profiles: %s", exc)
        return []
    except ValueError as exc:  # JSON decode error
        log.error("Error decoding profiles JSON: %s", exc)
        return []


def enrich_token(chain: str, address: str, profile_links=None) -> dict | None:
    """Look up market data for a token and return a normalized dict.

    Returns None if the token has no qualifying pair or on any error. The
    DexScreener tokens endpoint returns every pair the token trades in; we pick
    the pair on the matching chain with the highest USD liquidity.

    ``profile_links`` is the ``links`` array from the token-profiles feed; its
    socials are curated and take precedence over the ones found on the pair.
    """
    try:
        resp = requests.get(TOKEN_URL.format(address=address), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.error("Error fetching token data for %s: %s", address, exc)
        return None
    except ValueError as exc:
        log.error("Error decoding token JSON for %s: %s", address, exc)
        return None

    pairs = data.get("pairs") or []
    # Keep only pairs on the chain we care about.
    pairs = [p for p in pairs if p.get("chainId") == chain]
    if not pairs:
        return None

    # Best (most liquid) pair wins.
    def liquidity_of(pair: dict) -> float:
        try:
            return float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    best = max(pairs, key=liquidity_of)
    base = best.get("baseToken") or {}
    info = best.get("info") or {}

    # Build socials from the pair, then let the curated profile links override.
    socials = extract_socials(info.get("socials"))
    socials.update(extract_socials(info.get("websites")))
    socials.update(extract_socials(profile_links))

    return {
        "chain": chain,
        "address": address,
        "name": base.get("name") or "Unknown",
        "symbol": base.get("symbol") or "???",
        "liquidity": liquidity_of(best),
        "price": best.get("priceUsd"),
        "url": best.get("url") or f"https://dexscreener.com/{chain}/{address}",
        "socials": socials,
    }


def check_for_new_tokens() -> None:
    """One polling pass: fetch profiles, filter, and alert on new qualifiers."""
    profiles = fetch_latest_profiles()
    if not profiles:
        return

    for profile in profiles:
        chain = profile.get("chainId")
        address = profile.get("tokenAddress")

        # Skip anything missing identifiers or on an unwanted chain.
        if not chain or not address:
            continue
        if chain not in ALLOWED_CHAINS:
            continue

        # Dedupe on chain+address so we alert at most once per token.
        token_key = f"{chain}:{address}"
        if token_key in seen_tokens:
            continue

        token = enrich_token(chain, address, profile.get("links"))
        if token is None:
            # Couldn't price it yet (e.g. pair not indexed). Don't mark as seen
            # so a later poll can retry once liquidity data appears.
            continue

        # Mark as seen now that we have real data, regardless of threshold,
        # so we don't re-evaluate the same token forever.
        seen_tokens.add(token_key)

        if token["liquidity"] < MIN_LIQUIDITY_USD:
            log.info(
                "Skipping %s ($%s) on %s: liquidity below $%s",
                token["symbol"], f"{token['liquidity']:,.0f}", chain, f"{MIN_LIQUIDITY_USD:,}",
            )
            continue

        if REQUIRE_TELEGRAM and not token["socials"].get("telegram"):
            log.info(
                "Skipping %s on %s: no Telegram link",
                token["symbol"], chain,
            )
            continue

        log.info("Alerting on %s (%s) on %s", token["symbol"], address, chain)
        send_telegram(build_alert(token))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
            "(as environment variables)."
        )
        sys.exit(1)

    # Startup confirmation so you know the bot is alive.
    startup_ok = send_telegram(
        "✅ <b>GemRadar is online</b>\n\n"
        f"Monitoring: {', '.join(CHAIN_LABELS[c] for c in CHAIN_LABELS)}\n"
        f"Min liquidity: ${MIN_LIQUIDITY_USD:,}\n"
        f"Telegram required: {'yes' if REQUIRE_TELEGRAM else 'no'}\n"
        f"Poll interval: {POLL_INTERVAL}s"
    )
    if startup_ok:
        log.info("Startup message sent. GemRadar is running.")
    else:
        # A failed startup message usually means a bad token or chat ID.
        log.warning(
            "Could not send startup message. Check TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID. Continuing anyway."
        )

    # Main loop: poll forever, never let one bad pass kill the process.
    while True:
        try:
            check_for_new_tokens()
        except Exception as exc:  # noqa: BLE001 - last-resort safety net
            log.exception("Unexpected error during poll: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("GemRadar stopped by user.")
