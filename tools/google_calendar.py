import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib import parse, request
from zoneinfo import ZoneInfo

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")

BUSINESS_TIMEZONE = os.getenv("BUSINESS_TIMEZONE", "Europe/Budapest")
BUSINESS_HOURS = os.getenv("BUSINESS_HOURS", "09:00-17:00")
BUSINESS_DAYS = os.getenv("BUSINESS_DAYS", "1,2,3,4,5")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    v = (value or "").strip()
    if not v:
        raise ValueError("empty datetime")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(BUSINESS_TIMEZONE))
    return dt


def _auth_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json"}


def _require_credentials() -> None:
    required = [
        GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET,
        GOOGLE_REFRESH_TOKEN,
        GOOGLE_CALENDAR_ID,
    ]
    if not all(required):
        raise RuntimeError("Google Calendar credentials are missing in environment")


def _get_access_token() -> str:
    _require_credentials()
    token_url = "https://oauth2.googleapis.com/token"
    body = parse.urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Failed to refresh Google access token: {payload}")
    return token


def _get_busy_blocks(time_min: datetime, time_max: datetime) -> List[Tuple[datetime, datetime]]:
    token = _get_access_token()
    url = "https://www.googleapis.com/calendar/v3/freeBusy"
    payload = {
        "timeMin": _iso(time_min),
        "timeMax": _iso(time_max),
        "timeZone": BUSINESS_TIMEZONE,
        "items": [{"id": GOOGLE_CALENDAR_ID}],
    }
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            **_auth_headers(),
        },
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    busy = data.get("calendars", {}).get(GOOGLE_CALENDAR_ID, {}).get("busy", [])
    blocks: List[Tuple[datetime, datetime]] = []
    for item in busy:
        start = _parse_iso(item["start"])
        end = _parse_iso(item["end"])
        blocks.append((start, end))
    return sorted(blocks, key=lambda x: x[0])


def _business_window_for_day(day: datetime) -> Tuple[datetime, datetime]:
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    start_s, end_s = BUSINESS_HOURS.split("-")
    sh, sm = [int(x) for x in start_s.split(":")]
    eh, em = [int(x) for x in end_s.split(":")]
    start = day.astimezone(tz).replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = day.astimezone(tz).replace(hour=eh, minute=em, second=0, microsecond=0)
    return start, end


def get_free_slots_next_two_weeks(
    duration_minutes: int = 30,
    max_slots: int = 10,
) -> Dict[str, Any]:
    if duration_minutes <= 0:
        duration_minutes = 30
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=14)
    busy = _get_busy_blocks(now, end)
    allowed_days = {int(x.strip()) for x in BUSINESS_DAYS.split(",") if x.strip()}
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    slots: List[Dict[str, str]] = []

    cursor_day = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = end.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    while cursor_day <= last_day and len(slots) < max_slots:
        iso_weekday = cursor_day.isoweekday()
        if iso_weekday in allowed_days:
            b_start, b_end = _business_window_for_day(cursor_day)
            if b_end > b_start:
                work_start = max(b_start, now.astimezone(tz))
                work_end = b_end
                free_windows = [(work_start, work_end)] if work_end > work_start else []

                for busy_start, busy_end in busy:
                    busy_start_tz = busy_start.astimezone(tz)
                    busy_end_tz = busy_end.astimezone(tz)
                    updated: List[Tuple[datetime, datetime]] = []
                    for free_start, free_end in free_windows:
                        if busy_end_tz <= free_start or busy_start_tz >= free_end:
                            updated.append((free_start, free_end))
                            continue
                        if busy_start_tz > free_start:
                            updated.append((free_start, busy_start_tz))
                        if busy_end_tz < free_end:
                            updated.append((busy_end_tz, free_end))
                    free_windows = updated

                for free_start, free_end in free_windows:
                    slot_start = free_start
                    while slot_start + timedelta(minutes=duration_minutes) <= free_end:
                        slot_end = slot_start + timedelta(minutes=duration_minutes)
                        slots.append(
                            {
                                "start": slot_start.isoformat(),
                                "end": slot_end.isoformat(),
                            }
                        )
                        if len(slots) >= max_slots:
                            break
                        slot_start = slot_end
                    if len(slots) >= max_slots:
                        break
        cursor_day = cursor_day + timedelta(days=1)

    return {
        "timezone": BUSINESS_TIMEZONE,
        "duration_minutes": duration_minutes,
        "horizon_days": 14,
        "slots": slots,
    }


def create_meeting_event(
    title: str,
    description: str,
    start_iso: str,
    end_iso: str,
) -> Dict[str, Any]:
    start_dt = _parse_iso(start_iso)
    end_dt = _parse_iso(end_iso)
    now = datetime.now(timezone.utc)
    if end_dt <= start_dt:
        raise ValueError("meeting end must be after meeting start")
    if start_dt > now + timedelta(days=14):
        return {"created": False, "reason": "outside_two_weeks"}

    # Deterministic guard: re-check the slot before creating.
    busy = _get_busy_blocks(start_dt, end_dt)
    for busy_start, busy_end in busy:
        if not (busy_end <= start_dt or busy_start >= end_dt):
            return {"created": False, "reason": "slot_not_free"}

    token = _get_access_token()
    url = f"https://www.googleapis.com/calendar/v3/calendars/{parse.quote(GOOGLE_CALENDAR_ID, safe='')}/events"
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_dt.astimezone(ZoneInfo(BUSINESS_TIMEZONE)).isoformat()},
        "end": {"dateTime": end_dt.astimezone(ZoneInfo(BUSINESS_TIMEZONE)).isoformat()},
    }
    req = request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            **_auth_headers(),
        },
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {"created": True, "event_id": data.get("id"), "html_link": data.get("htmlLink")}
