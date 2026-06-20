#!/usr/bin/env python3
"""
Tennis court availability checker for Better-managed venues.

Uses a headless browser (Playwright) to render the Better timetable pages
(which are Cloudflare-protected and JavaScript-rendered), then parses
available tennis slots and sends a Telegram alert.
"""

import json
import logging
import os
import re
import smtplib
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

BETTER_BASE = "https://www.better.org.uk"

# Venue timetable URLs discovered from the Better sitemap.
# Each venue is checked once per week-start date needed to cover days_ahead.
DEFAULT_VENUES = [
    {
        "name": "Highbury Fields Tennis Courts",
        # Highbury Fields outdoor courts appear under the islington-parks venue.
        # If this shows no tennis results, try slug "highbury" or "islingtontc".
        "timetable_url": f"{BETTER_BASE}/leisure-centre/london/islington/islington-parks/timetable",
        "book_url": f"{BETTER_BASE}/leisure-centre/london/islington/islington-parks",
        "category_filter": "tennis",
    },
    {
        "name": "Islington Tennis Centre",
        "timetable_url": f"{BETTER_BASE}/leisure-centre/london/islington/islingtontc/timetable",
        "book_url": f"{BETTER_BASE}/leisure-centre/london/islington/islingtontc/tennis",
        "category_filter": "tennis",
    },
]

DEFAULT_TIME_WINDOWS = [
    {"start": "07:00", "end": "10:00", "label": "Morning"},
    {"start": "12:00", "end": "14:00", "label": "Lunchtime"},
    {"start": "17:30", "end": "21:00", "label": "Evening"},
]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {}


# ---------------------------------------------------------------------------
# Time-window helpers
# ---------------------------------------------------------------------------


def time_in_window(slot_time: str, windows: list[dict]) -> tuple[bool, str]:
    """Return (matches, window_label) for a HH:MM time string."""
    try:
        t = datetime.strptime(slot_time[:5], "%H:%M").time()
    except ValueError:
        return False, ""
    for w in windows:
        start = datetime.strptime(w["start"], "%H:%M").time()
        end = datetime.strptime(w["end"], "%H:%M").time()
        if start <= t <= end:
            return True, w.get("label", f"{w['start']}–{w['end']}")
    return False, ""


# ---------------------------------------------------------------------------
# Timetable fetching via Playwright
# ---------------------------------------------------------------------------


def _week_start(d: date) -> date:
    """Return the Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def fetch_timetable(page, url: str, start_date: date) -> str:
    """Navigate to a timetable page for a given week start and return HTML."""
    full_url = f"{url}?start_date={start_date.isoformat()}"
    log.info("  → %s", full_url)
    try:
        page.goto(full_url, wait_until="networkidle", timeout=30_000)
        # Wait for timetable rows or the empty-state message
        page.wait_for_selector(
            ".weekly-timetable__row, .weekly-timetable__empty-title",
            timeout=20_000,
        )
    except PWTimeout:
        log.warning("  Timetable load timed out for %s", full_url)
    return page.content()


def parse_timetable(
    html: str,
    venue: dict,
    time_windows: list[dict],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """
    Parse a Better weekly timetable page (server-rendered HTML) into slots.

    The timetable uses a column-per-day layout. Each day column has a header
    with the date and a list of session rows underneath.

    Row data attributes (from the Better JS bundle):
      data-category, data-subcategory, data-session,
      data-time_of_day (or data-time-of-day), data-free_spaces
    """
    soup = BeautifulSoup(html, "lxml")
    slots: list[dict] = []

    # ------------------------------------------------------------------
    # Strategy 1: rows carry data attributes with time + free spaces
    # ------------------------------------------------------------------
    rows = soup.select(
        ".weekly-timetable__row[data-category], "
        "[data-category][data-free-spaces], "
        "[data-category][data-free_spaces]"
    )
    if rows:
        slots.extend(_parse_rows(rows, venue, time_windows, date_from, date_to, soup))
        if slots:
            return slots

    # ------------------------------------------------------------------
    # Strategy 2: look for any element with a time + spaces pattern
    # ------------------------------------------------------------------
    slots.extend(_parse_generic(soup, venue, time_windows, date_from, date_to))
    return slots


def _parse_rows(
    rows,
    venue: dict,
    time_windows: list[dict],
    date_from: date,
    date_to: date,
    soup,
) -> list[dict]:
    slots: list[dict] = []

    # Build a mapping of day-column index → date from column headers
    col_dates: dict[int, date] = {}
    for i, header in enumerate(soup.select(".weekly-timetable__header-cell, [data-date]")):
        date_val = header.get("data-date") or header.get("data-timetables_items_date")
        if date_val:
            try:
                col_dates[i] = date.fromisoformat(date_val[:10])
            except ValueError:
                pass
        else:
            # Try to parse date text like "Mon 20 Jun"
            text = header.get_text(strip=True)
            m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", text)
            if not m:
                m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", text)
            if m:
                try:
                    day_str = m.group(0)
                    for fmt in ("%d %b %Y", "%d %b"):
                        try:
                            parsed = datetime.strptime(day_str, fmt)
                            col_dates[i] = parsed.replace(year=date.today().year).date()
                            break
                        except ValueError:
                            pass
                except Exception:
                    pass

    for row in rows:
        # Category filter
        category = row.get("data-category", "")
        if venue.get("category_filter") and venue["category_filter"].lower() not in category.lower():
            continue

        # Availability
        free = row.get("data-free-spaces") or row.get("data-free_spaces", "0")
        try:
            if int(free) <= 0:
                continue
        except (ValueError, TypeError):
            pass

        # Time
        time_str = (
            row.get("data-time_of_day")
            or row.get("data-time-of-day")
            or row.get("data-start-time")
            or _extract_time_from_text(row.get_text(" ", strip=True))
        )
        if not time_str:
            continue

        in_window, label = time_in_window(str(time_str), time_windows)
        if not in_window:
            continue

        # Date — try data attribute first, then infer from column
        slot_date_str = row.get("data-date") or row.get("data-timetables_items_date")
        slot_date: date | None = None
        if slot_date_str:
            try:
                slot_date = date.fromisoformat(slot_date_str[:10])
            except ValueError:
                pass
        if slot_date is None:
            # Fallback: find which column this row belongs to
            col_idx = _column_index(row, soup)
            slot_date = col_dates.get(col_idx)

        if slot_date is None or not (date_from <= slot_date <= date_to):
            continue

        # Book URL
        link_el = row.select_one("a[href]")
        book_url = venue["book_url"]
        if link_el:
            href = link_el["href"]
            book_url = href if href.startswith("http") else BETTER_BASE + href

        slots.append({
            "venue": venue["name"],
            "date": slot_date.strftime("%A %d %B %Y"),
            "date_iso": slot_date.isoformat(),
            "time": str(time_str),
            "window": label,
            "book_url": book_url,
        })

    return slots


def _parse_generic(
    soup,
    venue: dict,
    time_windows: list[dict],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Last-resort: look for any element with time + availability text."""
    slots: list[dict] = []
    cat_filter = venue.get("category_filter", "")

    for el in soup.find_all(True):
        text = el.get_text(" ", strip=True)
        if cat_filter and cat_filter.lower() not in text.lower():
            continue
        if "full" in text.lower() or "unavailable" in text.lower():
            continue
        time_str = _extract_time_from_text(text)
        if not time_str:
            continue
        in_window, label = time_in_window(time_str, time_windows)
        if not in_window:
            continue

        # Without reliable date info, tag as "this week"
        slots.append({
            "venue": venue["name"],
            "date": "this week",
            "date_iso": date_from.isoformat(),
            "time": time_str,
            "window": label,
            "book_url": venue["book_url"],
        })

    return slots[:20]  # cap to avoid noise


def _extract_time_from_text(text: str) -> str:
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text or "")
    return m.group(0) if m else ""


def _column_index(row, soup) -> int:
    """Best-effort: find which day-column a timetable row is in."""
    parent = row.parent
    while parent and parent.name not in ("td", "th", "div", "section"):
        parent = parent.parent
    if parent is None:
        return 0
    siblings = list(parent.parent.children) if parent.parent else []
    for i, sib in enumerate(siblings):
        if sib is parent:
            return i
    return 0


# ---------------------------------------------------------------------------
# Main checker
# ---------------------------------------------------------------------------


def check_all(venues: list[dict], time_windows: list[dict], days_ahead: int) -> list[dict]:
    today = date.today()
    date_to = today + timedelta(days=days_ahead - 1)

    # Determine which week-start dates we need to fetch per venue
    week_starts: list[date] = sorted(
        {_week_start(today + timedelta(days=d)) for d in range(days_ahead)}
    )

    all_slots: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-GB",
        )
        page = ctx.new_page()

        for venue in venues:
            log.info("Checking %s", venue["name"])
            for ws in week_starts:
                html = fetch_timetable(page, venue["timetable_url"], ws)
                slots = parse_timetable(html, venue, time_windows, today, date_to)
                if slots:
                    log.info("  Found %d matching slot(s) week of %s", len(slots), ws)
                all_slots.extend(slots)

        browser.close()

    return all_slots


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _format_slots_text(slots: list[dict]) -> str:
    lines = [f"🎾 {len(slots)} tennis court slot(s) available — book now!\n"]
    for s in slots:
        lines.append(
            f"• {s['venue']}\n"
            f"  {s['date']} at {s['time']}  ({s['window']})\n"
            f"  {s['book_url']}\n"
        )
    return "\n".join(lines)


def _send_telegram(slots: list[dict], config: dict) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id", "")

    if not token or not chat_id:
        return False

    lines = [f"🎾 <b>{len(slots)} court slot(s) available — book now!</b>\n"]
    for s in slots:
        lines.append(
            f"📍 <b>{s['venue']}</b>\n"
            f"🗓 {s['date']} at <b>{s['time']}</b>  ({s['window']})\n"
            f"🔗 <a href=\"{s['book_url']}\">Book this slot</a>\n"
        )
    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Telegram alert sent (%d slot(s))", len(slots))
        return True
    except requests.RequestException as exc:
        log.error("Telegram notification failed: %s", exc)
        return False


def notify(slots: list[dict], config: dict) -> None:
    if not _send_telegram(slots, config):
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing to stdout")
        print(_format_slots_text(slots))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    config = load_config()
    venues = config.get("venues", DEFAULT_VENUES)
    time_windows = config.get("time_windows", DEFAULT_TIME_WINDOWS)
    days_ahead = int(os.getenv("DAYS_AHEAD") or config.get("days_ahead", 7))

    available = check_all(venues, time_windows, days_ahead)

    if available:
        log.info("Total matching slots: %d", len(available))
        notify(available, config)
    else:
        log.info("No matching slots found")


if __name__ == "__main__":
    main()
