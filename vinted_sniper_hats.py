#!/usr/bin/env python3
"""
Vinted -> Telegram alert bot (single-pass version) -- HATS BOT

Checks all SEARCHES once, sends a Telegram alert for anything new, then
exits. Meant to be triggered on a schedule (e.g. a GitHub Actions cron
job every 5 minutes) rather than run continuously in a loop.

Reads the bot token and chat id from environment variables so they can
be stored as GitHub Secrets instead of sitting in the file in plain text.
"""

import json
import os
import logging
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

# ============================== CONFIG ==============================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# One or more full Vinted search-result URLs (copy straight from your browser)
SEARCHES = [
    "https://www.vinted.co.uk/catalog?search_text=hats&brand_ids[]=180276&brand_ids[]=2975&brand_ids[]=6575977&brand_ids[]=123146",
]

STATE_FILE = Path(__file__).parent / "seen_items_hats.json"
ITEMS_PER_CHECK = 20

# ======================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vinted-sniper-hats")

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }
)


def domain_of(url: str) -> str:
    return urlparse(url).netloc


def refresh_session_cookie(base_domain: str):
    try:
        session.get(f"https://{base_domain}/", timeout=15)
    except requests.RequestException as e:
        log.warning("Could not refresh session cookie for %s: %s", base_domain, e)


def search_url_to_api_params(search_url: str) -> dict:
    parsed = urlparse(search_url)
    qs = parse_qs(parsed.query)
    params = {k: (v if len(v) > 1 else v[0]) for k, v in qs.items()}
    params.pop("page", None)
    params.pop("time", None)
    params["per_page"] = str(ITEMS_PER_CHECK)
    params["order"] = "newest_first"
    return params


def fetch_items(search_url: str) -> list:
    base_domain = domain_of(search_url)
    api_url = f"https://{base_domain}/api/v2/catalog/items"
    params = search_url_to_api_params(search_url)

    resp = session.get(api_url, params=params, timeout=15)
    if resp.status_code in (401, 403):
        refresh_session_cookie(base_domain)
        resp = session.get(api_url, params=params, timeout=15)

    resp.raise_for_status()
    return resp.json().get("items", [])


def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict):
    STATE_FILE.write_text(json.dumps(seen))


def send_telegram_alert(item: dict, base_domain: str):
    title = item.get("title", "New Vinted item")
    price = item.get("price", {})
    price_str = f"{price.get('amount', '?')} {price.get('currency_code', '')}".strip()
    brand = item.get("brand_title", "")
    size = item.get("size_title", "")
    url = item.get("url") or f"https://{base_domain}/items/{item.get('id')}"
    photo_url = (item.get("photo") or {}).get("url")

    caption = (
        f"🆕 <b>{title}</b>\n"
        f"💰 {price_str}\n"
        + (f"🏷️ {brand}\n" if brand else "")
        + (f"📏 {size}\n" if size else "")
        + f"\n{url}"
    )

    try:
        if photo_url:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "photo": photo_url,
                },
                timeout=15,
            )
        else:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": caption,
                    "parse_mode": "HTML",
                },
                timeout=15,
            )
    except requests.RequestException as e:
        log.error("Failed to send Telegram alert: %s", e)


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set. "
            "On GitHub Actions these come from repo Secrets."
        )
        return

    seen = load_seen()

    for search_url in SEARCHES:
        base_domain = domain_of(search_url)
        refresh_session_cookie(base_domain)
        try:
            items = fetch_items(search_url)
        except Exception as e:
            log.warning("Fetch failed for %s: %s", search_url, e)
            continue

        first_time_seeing_this_search = search_url not in seen
        known_ids = set(seen.get(search_url, []))

        if first_time_seeing_this_search:
            known_ids = {str(i["id"]) for i in items}
            log.info("Primed %d existing items for: %s", len(items), search_url)
        else:
            new_items = [i for i in items if str(i["id"]) not in known_ids]
            for item in reversed(new_items):
                log.info("New item: %s", item.get("title"))
                send_telegram_alert(item, base_domain)
                known_ids.add(str(item["id"]))

        seen[search_url] = list(known_ids)[-500:]

    save_seen(seen)


if __name__ == "__main__":
    main()
