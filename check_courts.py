#!/usr/bin/env python3
"""
Tennis court availability checker for Better-managed venues.

Polls Highbury Fields and Islington Tennis Centre via the Better
booking system and sends a Telegram alert when courts are available
within the configured time windows.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

BETTER_BASE = "https://better.org.uk"

# Default venues — override in config.json
DEFAULT_VENUES = [
    {
        "name": "Highbury Fields Tennis Courts",
        "slug": "highbury-fields-tennis-courts",
        "location": "islington",
        "activity": "tennis",
    },
    {
        "name": "Islington Tennis Centre",
        "slug": "islington-tennis-centre",
        "location": "islington",
        "activity": "tennis",
    },
]

# Default time windows — override in config.json
DEFAULT_TIME_WINDOWS = [
    {"start": "07:00", "end": "10:00", "label": "Morning"},
    {"start": "12:00", "end": "14:00", "label": "Lunchtime"},
    {"start": "17:30", "end": "21:00", "label": "Evening"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": BETTER_BASE + "/",
}


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
    """Return (matches, window_label) for a HH:MM slot time."""
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
# Checker
# ---------------------------------------------------------------------------


class BetterChecker:
    def __init__(self, time_windows: list[dict]) -> None:
        self.time_windows = time_windows
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def check_all(self, venues: list[dict], days_ahead: int) -> list[dict]:
        today = date.today()
        found: list[dict] = []
        for offset in range(days_ahead):
            check_date = today + timedelta(days=offset)
            for venue in venues:
                log.info("Checking %s on %s", venue["name"], check_date)
                slots = self._get_slots(venue, check_date)
                if slots:
                    log.info("  %d matching slot(s)", len(slots))
                found.extend(slots)
        return found

    # ------------------------------------------------------------------
    # API strategy (tried first — JSON response)
    # ------------------------------------------------------------------

    def _get_slots(self, venue: dict, check_date: date) -> list[dict]:
        slots = self._try_api(venue, check_date)
        if slots is not None:
            return slots
        return self._try_scrape(venue, check_date)

    def _try_api(self, venue: dict, check_date: date) -> list[dict] | None:
        """
        Better exposes a JSON API used by their mobile apps and website SPA.
        The endpoint below was identified from browser network traffic; it may
        need updating if Better changes their backend.

        To find the correct URL yourself:
          1. Open Chrome DevTools → Network tab → filter XHR/Fetch
          2. Visit the venue booking page on better.org.uk
          3. Select a date — watch for requests returning slot JSON
          4. Copy the URL and update SLOT_API_URL in config.json
        """
        api_url = (
            os.getenv("BETTER_SLOT_API_URL")
            or load_config().get("slot_api_url")
            or f"{BETTER_BASE}/api/activities/{venue['slug']}/slots"
        )

        params = {
            "date": check_date.isoformat(),
            "location": venue["location"],
            "activity": venue.get("activity", "tennis"),
        }

        try:
            resp = self.session.get(api_url, params=params, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return self._parse_json(data, venue, check_date)
        except (requests.RequestException, ValueError) as exc:
            log.debug("API attempt failed (%s), falling back to HTML scrape", exc)
            return None

    def _parse_json(
        self, data: Any, venue: dict, check_date: date
    ) -> list[dict]:
        # Normalise to a flat list regardless of response shape
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (
                data.get("slots")
                or data.get("data")
                or data.get("results")
                or data.get("sessions")
                or []
            )
        else:
            return []

        slots: list[dict] = []
        for item in items:
            if not item.get("available", True):
                continue
            time_str = (
                item.get("time")
                or item.get("start_time")
                or item.get("startTime")
                or item.get("begins_at", "")
            )
            in_window, label = time_in_window(str(time_str), self.time_windows)
            if not in_window:
                continue
            book_url = (
                item.get("book_url")
                or item.get("bookUrl")
                or item.get("booking_url")
                or self._venue_book_url(venue)
            )
            if book_url and not book_url.startswith("http"):
                book_url = BETTER_BASE + book_url
            slots.append(
                self._make_slot(venue["name"], check_date, str(time_str), label, book_url)
            )
        return slots

    # ------------------------------------------------------------------
    # HTML scrape strategy (fallback)
    # ------------------------------------------------------------------

    def _try_scrape(self, venue: dict, check_date: date) -> list[dict]:
        url = self._venue_book_url(venue)
        params = {"date": check_date.isoformat()}
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Could not fetch %s: %s", venue["name"], exc)
            return []

        # Better's SPA sometimes embeds slot data in a __NEXT_DATA__ script
        slots = self._parse_next_data(resp.text, venue, check_date)
        if slots is not None:
            return slots

        # Last resort: find slot elements in rendered HTML
        return self._parse_html_slots(resp.text, venue, check_date, url)

    def _parse_next_data(
        self, html: str, venue: dict, check_date: date
    ) -> list[dict] | None:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script or not script.string:
            return None
        try:
            page_data = json.loads(script.string)
        except ValueError:
            return None

        # Walk the page props to find anything resembling slot data
        def _walk(obj: Any) -> list[dict]:
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                if any(k in obj[0] for k in ("time", "startTime", "start_time", "begins_at")):
                    return self._parse_json(obj, venue, check_date)
            if isinstance(obj, dict):
                for v in obj.values():
                    result = _walk(v)
                    if result:
                        return result
            return []

        return _walk(page_data)

    def _parse_html_slots(
        self, html: str, venue: dict, check_date: date, page_url: str
    ) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        slots: list[dict] = []

        # Common selectors used by Better's booking pages (inspect the live
        # page and update these if the structure changes)
        candidates = soup.select(
            "[data-time], [data-start-time], "
            ".time-slot, .slot-item, .booking-slot, "
            ".activity-session, .session-card"
        )

        for el in candidates:
            time_str = (
                el.get("data-time")
                or el.get("data-start-time")
                or _inner(el, ".time, .slot-time, .start-time, time")
            )
            if not time_str:
                continue

            classes = " ".join(el.get("class", []))
            if any(x in classes for x in ("unavailable", "full", "disabled", "sold-out")):
                continue
            if el.get("data-available") == "false" or el.get("disabled"):
                continue

            in_window, label = time_in_window(str(time_str), self.time_windows)
            if not in_window:
                continue

            link = el.get("href") or ""
            anchor = el.select_one("a[href]")
            if anchor:
                link = anchor["href"]
            if link and not link.startswith("http"):
                link = BETTER_BASE + link
            book_url = link or page_url

            slots.append(
                self._make_slot(venue["name"], check_date, str(time_str), label, book_url)
            )

        return slots

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _venue_book_url(self, venue: dict) -> str:
        return (
            f"{BETTER_BASE}/leisure-centre/{venue['location']}"
            f"/{venue['slug']}/book-now/{venue.get('activity', 'tennis')}"
        )

    @staticmethod
    def _make_slot(
        venue_name: str, check_date: date, time_str: str, label: str, book_url: str
    ) -> dict:
        return {
            "venue": venue_name,
            "date": check_date.strftime("%A %d %B %Y"),
            "date_iso": check_date.isoformat(),
            "time": time_str,
            "window": label,
            "book_url": book_url,
        }


def _inner(el: Any, selector: str) -> str:
    found = el.select_one(selector)
    return found.get_text(strip=True) if found else ""


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
            f"🗓 {s['date']} at <b>{s['time']}</b> ({s['window']})\n"
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
        log.info("Telegram alert sent to chat %s (%d slot(s))", chat_id, len(slots))
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

    checker = BetterChecker(time_windows)
    available = checker.check_all(venues, days_ahead)

    if available:
        log.info("Total matching slots found: %d", len(available))
        notify(available, config)
    else:
        log.info("No matching slots found")


if __name__ == "__main__":
    main()
