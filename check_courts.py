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


def time_in_window(slot_time: str, windows: list[dict], slot_date: date | None = None) -> tuple[bool, str]:
    """Return (matches, window_label) for a HH:MM time string.

    Windows can include an optional 'days' list of 3-letter day names
    (e.g. ["Mon","Tue","Wed","Thu","Fri"]) to restrict which days the
    window applies. If 'days' is absent the window applies every day.
    """
    try:
        t = datetime.strptime(slot_time[:5], "%H:%M").time()
    except ValueError:
        return False, ""
    day_abbr = slot_date.strftime("%a") if slot_date else None
    for w in windows:
        allowed_days = w.get("days")
        if allowed_days and day_abbr and day_abbr not in allowed_days:
            continue
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

    # Intercept JSON responses to surface any timetable API endpoints
    def _on_response(response):
        if response.status == 200 and "json" in response.headers.get("content-type", ""):
            ru = response.url
            if any(k in ru.lower() for k in ("timetable", "session", "schedule", "activity", "slot")):
                log.info("  [API] %s", ru[:200])

    page.on("response", _on_response)
    try:
        page.goto(full_url, wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        log.warning("  domcontentloaded timed out for %s", full_url)

    # After initial load, wait for the JS app to render timetable data.
    # networkidle fires once all pending requests settle; give it 30s.
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        pass  # Proceed with whatever is in the DOM

    html = page.content()
    page.remove_listener("response", _on_response)
    return html


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

        in_window, label = time_in_window(str(time_str), time_windows, slot_date)
        if not in_window:
            continue

        # Book URL
        link_el = row.select_one("a[href]")
        book_url = venue["book_url"]
        if link_el:
            href = link_el["href"]
            book_url = href if href.startswith("http") else BETTER_BASE + href

        # Session detail (indoor / outdoor / court type)
        row_text_lower = row.get_text(" ", strip=True).lower()
        subcategory = (row.get("data-subcategory") or row.get("data-session") or "").strip()
        if "indoor" in row_text_lower or "indoor" in subcategory.lower():
            session_type = "Indoor"
        elif "outdoor" in row_text_lower or "outdoor" in subcategory.lower():
            session_type = "Outdoor"
        else:
            session_type = subcategory or ""

        slots.append({
            "venue": venue["name"],
            "session_type": session_type,
            "date": slot_date.strftime("%A %d %B %Y"),
            "date_iso": slot_date.isoformat(),
            "time": str(time_str),
            "window": label,
            "book_url": book_url,
        })

    return slots


_MONTH_RE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_PAT = re.compile(
    rf"\b(\d{{1,2}})\s+({_MONTH_RE})\b|\b({_MONTH_RE})\s+(\d{{1,2}})\b",
    re.IGNORECASE,
)


def _extract_date_from_text(text: str, year: int) -> "date | None":
    m = _DATE_PAT.search(text)
    if not m:
        return None
    raw = m.group(0)
    for fmt in ("%d %b", "%d %B", "%b %d", "%B %d"):
        try:
            return datetime.strptime(raw, fmt).replace(year=year).date()
        except ValueError:
            pass
    return None


def _parse_generic(
    soup,
    venue: dict,
    time_windows: list[dict],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Last-resort: find time slots and try to infer their date from DOM context.

    Better's timetable is a column-per-day layout: date headers are siblings of
    day columns, not ancestors of individual slot elements.  Walking ancestors
    (up to 6 levels) misses them.  Instead we:
    1. Pre-scan every element for date markers and record their document-order
       position.
    2. For each time slot, find the nearest preceding date marker.
    """
    slots: list[dict] = []
    seen: set[tuple] = set()
    cat_filter = venue.get("category_filter", "")

    all_els = list(soup.find_all(True))
    el_to_idx: dict[int, int] = {id(el): i for i, el in enumerate(all_els)}

    # Collect (document_index, date) pairs, deduped by position
    dated_positions: list[tuple[int, date]] = []
    seen_positions: set[int] = set()
    for i, el in enumerate(all_els):
        d: "date | None" = None
        d_val = el.get("data-date") or el.get("data-timetables_items_date")
        if d_val:
            try:
                d = date.fromisoformat(d_val[:10])
            except ValueError:
                pass
        if d is None:
            text = el.get_text(" ", strip=True) if hasattr(el, "get_text") else ""
            if 3 < len(text) < 50:
                d = _extract_date_from_text(text, date_from.year)
        if d and date_from <= d <= date_to and i not in seen_positions:
            dated_positions.append((i, d))
            seen_positions.add(i)

    for el in all_els:
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 3000:
            continue
        if cat_filter and cat_filter.lower() not in text.lower():
            continue
        if "full" in text.lower() or "unavailable" in text.lower():
            continue

        time_str = _extract_time_from_text(text)
        if not time_str:
            continue

        el_idx = el_to_idx.get(id(el), -1)
        slot_date: "date | None" = None

        # Try ancestor data-date first (fast, precise)
        for candidate in [el] + list(el.parents)[:6]:
            if not hasattr(candidate, "get"):
                continue
            d_val = candidate.get("data-date") or candidate.get("data-timetables_items_date")
            if d_val:
                try:
                    d = date.fromisoformat(d_val[:10])
                    if date_from <= d <= date_to:
                        slot_date = d
                        break
                except ValueError:
                    pass

        # Fall back to nearest preceding date element in document order
        if slot_date is None:
            for pos, d in reversed(dated_positions):
                if pos < el_idx:
                    slot_date = d
                    break

        in_window, label = time_in_window(time_str, time_windows, slot_date)
        if not in_window:
            continue

        key = (slot_date.isoformat() if slot_date else "week", time_str)
        if key in seen:
            continue
        seen.add(key)

        # Try to detect indoor/outdoor from element text
        text_lower = text.lower()
        if "indoor" in text_lower:
            session_type = "Indoor"
        elif "outdoor" in text_lower:
            session_type = "Outdoor"
        else:
            session_type = ""

        slots.append({
            "venue": venue["name"],
            "session_type": session_type,
            "date": slot_date.strftime("%A %d %B %Y") if slot_date else "this week",
            "date_iso": slot_date.isoformat() if slot_date else date_from.isoformat(),
            "time": time_str,
            "window": label,
            "book_url": venue["book_url"],
        })

    return slots[:20]


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
# Booking-page parser (bookings.better.org.uk per-date pages)
# ---------------------------------------------------------------------------


def fetch_booking_page(page, url: str, slot_date: date) -> str:
    """Fetch a bookings.better.org.uk date-specific page and return HTML."""
    log.info("  → %s", url)

    def _on_response(response):
        if response.status == 200 and "json" in response.headers.get("content-type", ""):
            ru = response.url
            if any(k in ru.lower() for k in ("slot", "session", "avail", "book", "timetable")):
                log.info("  [API] %s", ru[:200])

    page.on("response", _on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        log.warning("  domcontentloaded timed out for %s", url)

    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PWTimeout:
        pass

    html = page.content()
    page.remove_listener("response", _on_response)
    return html


def parse_booking_page(html: str, venue: dict, time_windows: list[dict], slot_date: date) -> list[dict]:
    """Parse a bookings.better.org.uk by-time page.

    The date is already known (it's in the URL), so we only need to find
    available time slots and filter by the time windows.
    """
    soup = BeautifulSoup(html, "lxml")
    slots: list[dict] = []
    seen: set[str] = set()

    # Collect all text containing a time pattern; skip anything that looks unavailable
    for el in soup.find_all(True):
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 500:
            continue

        # Skip booked / unavailable slots
        tl = text.lower()
        if any(w in tl for w in ("unavailable", "booked", "full", "sold out", "closed")):
            continue

        time_str = _extract_time_from_text(text)
        if not time_str or time_str in seen:
            continue

        in_window, label = time_in_window(time_str, time_windows, slot_date)
        if not in_window:
            continue

        seen.add(time_str)

        # Look for a booking link nearby
        link_el = el.select_one("a[href]") or el.find_parent("a")
        book_url = venue["book_url"]
        if link_el and link_el.get("href"):
            href = link_el["href"]
            book_url = href if href.startswith("http") else "https://bookings.better.org.uk" + href

        slots.append({
            "venue": venue["name"],
            "session_type": "",
            "date": slot_date.strftime("%A %d %B %Y"),
            "date_iso": slot_date.isoformat(),
            "time": time_str,
            "window": label,
            "book_url": book_url,
        })

    return slots


# ---------------------------------------------------------------------------
# Main checker
# ---------------------------------------------------------------------------


def check_all(venues: list[dict], time_windows: list[dict], days_ahead: int) -> list[dict]:
    today = date.today()
    date_to = today + timedelta(days=days_ahead - 1)
    dates_ahead: list[date] = [today + timedelta(days=d) for d in range(days_ahead)]

    # Week-start dates for venues using the old weekly-timetable URLs
    week_starts: list[date] = sorted(
        {_week_start(d) for d in dates_ahead}
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

            if "booking_url_template" in venue:
                # bookings.better.org.uk: one request per day, date known from URL
                tmpl = venue["booking_url_template"]
                for check_date in dates_ahead:
                    url = tmpl.format(date=check_date.isoformat())
                    html = fetch_booking_page(page, url, check_date)
                    slots = parse_booking_page(html, venue, time_windows, check_date)
                    if slots:
                        log.info("  Found %d slot(s) on %s", len(slots), check_date)
                    all_slots.extend(slots)
            else:
                # www.better.org.uk weekly timetable fallback
                for ws in week_starts:
                    html = fetch_timetable(page, venue["timetable_url"], ws)
                    slots = parse_timetable(html, venue, time_windows, today, date_to)
                    if slots:
                        log.info("  Found %d matching slot(s) week of %s", len(slots), ws)
                    all_slots.extend(slots)

        browser.close()

    # Deduplicate across week fetches (same slot can appear in two weekly pages)
    seen_keys: set[tuple] = set()
    unique: list[dict] = []
    for s in all_slots:
        key = (s["venue"], s["date_iso"], s["time"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(s)
    return unique


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _format_slots_text(slots: list[dict]) -> str:
    lines = [f"🎾 {len(slots)} tennis court slot(s) available — book now!\n"]
    for s in slots:
        venue_str = s["venue"]
        if s.get("session_type"):
            venue_str += f" ({s['session_type']})"
        lines.append(
            f"• {venue_str}\n"
            f"  {s['date']} at {s['time']}  ({s['window']})\n"
            f"  {s['book_url']}\n"
        )
    return "\n".join(lines)


def _send_telegram(slots: list[dict], config: dict) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id", "")

    if not token or not chat_id:
        return False

    lines = ["🎾 <b>Tennis court available!</b>\n"]
    for s in slots:
        venue_line = f"📍 <b>{s['venue']}</b>"
        if s.get("session_type"):
            venue_line += f" ({s['session_type']})"
        lines.append(
            f"{venue_line}\n"
            f"🗓 {s['date']} at <b>{s['time']}</b>\n"
            f"🔗 <a href=\"{s['book_url']}\">Book now</a>\n"
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
