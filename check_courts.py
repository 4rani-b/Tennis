#!/usr/bin/env python3
"""
Tennis court availability checker for Better-managed venues.

Calls the Better booking API directly (no browser) to check court availability,
deduplicates across runs using a local state file, and sends Telegram alerts
only for newly available slots.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

LONDON_TZ = ZoneInfo("Europe/London")
API_BASE = "https://better-admin.org.uk/api/activities/venue/islington-tennis-centre/activity"
API_HEADERS = {
    "Origin": "https://bookings.better.org.uk",
    "Referer": "https://bookings.better.org.uk/",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

DEFAULT_STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "seen_slots.json")
STATE_MAX_AGE_HOURS = 24


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
# State (deduplication)
# ---------------------------------------------------------------------------


def load_state(path: str) -> set:
    """Load seen composite_keys from state file. Returns empty set if missing/stale."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as fh:
            data = json.load(fh)
        updated_str = data.get("updated", "")
        if updated_str:
            updated = datetime.fromisoformat(updated_str)
            # Ensure timezone-aware comparison
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=LONDON_TZ)
            age = datetime.now(tz=LONDON_TZ) - updated
            if age > timedelta(hours=STATE_MAX_AGE_HOURS):
                log.info("State file is >%dh old — resetting", STATE_MAX_AGE_HOURS)
                return set()
        return set(data.get("seen", []))
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.warning("Could not read state file %s: %s", path, exc)
        return set()


def save_state(path: str, seen: set) -> None:
    """Save seen composite_keys to state file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "seen": sorted(seen),
        "updated": datetime.now(tz=LONDON_TZ).isoformat(),
    }
    try:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
        log.info("State saved: %d key(s) in %s", len(seen), path)
    except OSError as exc:
        log.error("Could not save state file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# API fetching
# ---------------------------------------------------------------------------


def fetch_slots(slug: str, slot_date: date, session: requests.Session) -> list:
    """Call Better API for a venue slug and date. Returns raw slot dicts."""
    url = f"{API_BASE}/{slug}/v2/times?date={slot_date.isoformat()}"
    log.info("Fetching %s", url)
    try:
        resp = session.get(url, headers=API_HEADERS, timeout=15)
    except requests.RequestException as exc:
        log.warning("Request failed for %s on %s: %s", slug, slot_date, exc)
        return []

    if resp.status_code == 422:
        log.warning("Date %s not bookable yet for %s (422)", slot_date, slug)
        return []

    if resp.status_code != 200:
        log.warning("API returned %d for %s on %s", resp.status_code, slug, slot_date)
        return []

    try:
        body = resp.json()
    except ValueError as exc:
        log.warning("Invalid JSON from API for %s on %s: %s", slug, slot_date, exc)
        return []

    slots = body.get("data", [])
    if not isinstance(slots, list):
        log.warning("Unexpected API shape for %s on %s: %r", slug, slot_date, type(slots))
        return []

    log.info("  Got %d slot(s) from API", len(slots))
    return slots


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _parse_hhmm(s: str):
    try:
        return datetime.strptime(s[:5], "%H:%M").time()
    except (ValueError, TypeError):
        return None


def filter_slots(
    slots: list,
    time_windows: list,
    slot_date: date,
    now_london: datetime,
) -> list:
    """Apply availability, time-window, and past-slot filters. Returns matching dicts."""
    today = now_london.date()
    day_abbr = slot_date.strftime("%a")
    matched = []

    for item in slots:
        if not isinstance(item, dict):
            continue

        # Availability: spaces > 0
        spaces = item.get("spaces")
        if spaces is not None:
            try:
                if int(spaces) <= 0:
                    continue
            except (ValueError, TypeError):
                pass
        else:
            # Fallback: check action_to_show status
            action = item.get("action_to_show") or {}
            status = str(action.get("status", "")).upper()
            if status in ("FULL", "UNAVAILABLE", "CLOSED", "CANCELLED"):
                continue

        # Extract start time
        starts_at = item.get("starts_at", {})
        time_str = starts_at.get("format_24_hour", "") if isinstance(starts_at, dict) else ""
        if not time_str:
            continue
        slot_time = _parse_hhmm(time_str)
        if slot_time is None:
            continue

        # Skip slots in the past (same day, already started)
        if slot_date == today:
            now_time = now_london.time()
            if slot_time < now_time:
                continue

        # Extract end time for display
        ends_at = item.get("ends_at", {})
        end_time_str = ends_at.get("format_24_hour", "") if isinstance(ends_at, dict) else ""

        # Check time windows
        in_window = False
        window_label = ""
        for w in time_windows:
            allowed_days = w.get("days")
            if allowed_days and day_abbr not in allowed_days:
                continue
            start = _parse_hhmm(w["start"])
            end = _parse_hhmm(w["end"])
            if start is None or end is None:
                continue
            if start <= slot_time <= end:
                in_window = True
                window_label = w.get("label", f"{w['start']}–{w['end']}")
                break

        if not in_window:
            continue

        matched.append({
            "composite_key": item.get("composite_key", ""),
            "time": time_str,
            "end_time": end_time_str,
            "date": slot_date,
            "price": (item.get("price") or {}).get("formatted_amount", ""),
            "window": window_label,
        })

    return matched


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------


def check_all(config: dict, state_path: str) -> list:
    """Check all venues for new slots. Returns list of new slot dicts."""
    venues = config.get("venues", [])
    time_windows = config.get("time_windows", [])
    days_ahead = int(os.getenv("DAYS_AHEAD") or config.get("days_ahead", 7))

    now_london = datetime.now(tz=LONDON_TZ)
    today = now_london.date()
    dates_to_check = [today + timedelta(days=d) for d in range(days_ahead)]

    send_all = os.getenv("SEND_ALL", "").lower() == "true"
    last_available = set() if send_all else load_state(state_path)
    log.info("Loaded %d composite_key(s) from last run%s", len(last_available), " (send_all mode)" if send_all else "")

    current_available = set()
    new_slots = []

    with requests.Session() as session:
        for venue in venues:
            slug = venue.get("slug", "")
            if not slug:
                log.warning("Venue %s has no slug, skipping", venue.get("name"))
                continue

            for check_date in dates_to_check:
                raw_slots = fetch_slots(slug, check_date, session)
                matched = filter_slots(raw_slots, time_windows, check_date, now_london)

                for slot in matched:
                    ck = slot["composite_key"]
                    if not ck:
                        continue
                    current_available.add(ck)
                    if ck in last_available:
                        continue
                    new_slots.append({
                        "venue_name": venue["name"],
                        "slug": slug,
                        "book_url": venue.get("book_url", ""),
                        "composite_key": ck,
                        "date": slot["date"],
                        "time": slot["time"],
                        "end_time": slot["end_time"],
                        "price": slot["price"],
                        "window": slot["window"],
                    })

    save_state(state_path, current_available)
    return new_slots


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------


def _format_date_human(d: date) -> str:
    return d.strftime("%A %-d %B")


def send_telegram(slots: list, config: dict) -> None:
    """Send Telegram alert for new slots."""
    token = os.getenv("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id", "")

    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing to stdout")
        _print_slots(slots)
        return

    lines = ["🎾 <b>Tennis courts available!</b>\n"]
    for s in slots:
        date_str = _format_date_human(s["date"])
        time_range = s["time"]
        if s.get("end_time"):
            time_range = f"{s['time']}–{s['end_time']}"
        book_url = f"{s['book_url']}/{s['date'].isoformat()}/by-time"
        price_str = f"\n💷 {s['price']}" if s.get("price") else ""
        lines.append(
            f"📍 <b>{s['venue_name']}</b>\n"
            f"🗓 {date_str} at {time_range}{price_str}\n"
            f"🔗 {book_url}\n"
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
    except requests.RequestException as exc:
        log.error("Telegram notification failed: %s — printing to stdout", exc)
        _print_slots(slots)


def _print_slots(slots: list) -> None:
    print("\n🎾 Tennis courts available!\n")
    for s in slots:
        date_str = _format_date_human(s["date"])
        time_range = s["time"]
        if s.get("end_time"):
            time_range = f"{s['time']}–{s['end_time']}"
        book_url = f"{s['book_url']}/{s['date'].isoformat()}/by-time"
        price_str = f"  💷 {s['price']}" if s.get("price") else ""
        print(
            f"📍 {s['venue_name']}\n"
            f"🗓 {date_str} at {time_range}{price_str}\n"
            f"🔗 {book_url}\n"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    config = load_config()
    state_path = os.getenv("STATE_PATH") or DEFAULT_STATE_PATH

    new_slots = check_all(config, state_path)

    if new_slots:
        log.info("Found %d new slot(s)", len(new_slots))
        send_telegram(new_slots, config)
    else:
        log.info("No new slots found")


if __name__ == "__main__":
    main()
