import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib import parse, request
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from app_config import DEFAULT_BUSINESS_DAYS, DEFAULT_BUSINESS_HOURS, DEFAULT_BUSINESS_TIMEZONE

logger = logging.getLogger("google_calendar")
BUSINESS_TIMEZONE = DEFAULT_BUSINESS_TIMEZONE


class CalendarContext:
    def __init__(self, credentials: dict[str, Any], settings: dict[str, Any] | None = None):
        creds = dict(credentials or {})
        options = dict(settings or {})
        self.client_id = str(creds.get("client_id") or "").strip()
        self.client_secret = str(creds.get("client_secret") or "").strip()
        self.refresh_token = str(creds.get("refresh_token") or "").strip()
        self.calendar_id = str(creds.get("calendar_id") or "primary").strip() or "primary"
        self.business_timezone = str(options.get("business_timezone") or DEFAULT_BUSINESS_TIMEZONE).strip() or DEFAULT_BUSINESS_TIMEZONE
        self.business_hours = str(options.get("business_hours") or DEFAULT_BUSINESS_HOURS).strip() or DEFAULT_BUSINESS_HOURS
        self.business_days = str(options.get("business_days") or DEFAULT_BUSINESS_DAYS).strip() or DEFAULT_BUSINESS_DAYS
        self.enforce_busy_recheck = bool(options.get("enforce_busy_recheck", False))


DEFAULT_CONTEXT = CalendarContext({}, {})


def _http_error_details(error: HTTPError) -> str:
    body = ""
    try:
        body = error.read().decode("utf-8")
    except Exception:
        body = ""
    body = body.strip()
    if body:
        return f"HTTP {error.code} {body}"
    return f"HTTP {error.code} {error.reason}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str, ctx: CalendarContext) -> datetime:
    v = (value or "").strip()
    if not v:
        raise ValueError("empty datetime")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(ctx.business_timezone))
    return dt


def _auth_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json"}


def _require_credentials(ctx: CalendarContext) -> None:
    required = [ctx.client_id, ctx.client_secret, ctx.refresh_token, ctx.calendar_id]
    if not all(required):
        raise RuntimeError("Google Calendar credentials are missing")


def get_access_token(ctx: CalendarContext) -> str:
    _require_credentials(ctx)
    token_url = "https://oauth2.googleapis.com/token"
    body = parse.urlencode(
        {
            "client_id": ctx.client_id,
            "client_secret": ctx.client_secret,
            "refresh_token": ctx.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        details = _http_error_details(exc)
        logger.error(
            "[CALENDAR auth] token refresh failed calendar_id=%s details=%s",
            ctx.calendar_id,
            details,
        )
        raise RuntimeError(f"Google token refresh failed: {details}") from exc
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Failed to refresh Google access token: {payload}")
    return token


def _business_window_for_day(day: datetime, ctx: CalendarContext) -> Tuple[datetime, datetime]:
    tz = ZoneInfo(ctx.business_timezone)
    start_s, end_s = ctx.business_hours.split("-")
    sh, sm = [int(x) for x in start_s.split(":")]
    eh, em = [int(x) for x in end_s.split(":")]
    start = day.astimezone(tz).replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = day.astimezone(tz).replace(hour=eh, minute=em, second=0, microsecond=0)
    return start, end


def _allowed_business_days(ctx: CalendarContext) -> set[int]:
    return {int(x.strip()) for x in ctx.business_days.split(",") if x.strip()}


def _is_within_business_hours(start_dt: datetime, end_dt: datetime, ctx: CalendarContext) -> bool:
    tz = ZoneInfo(ctx.business_timezone)
    s = start_dt.astimezone(tz)
    e = end_dt.astimezone(tz)
    if s.date() != e.date():
        return False
    if s.isoweekday() not in _allowed_business_days(ctx):
        return False
    b_start, b_end = _business_window_for_day(s, ctx)
    return s >= b_start and e <= b_end


def _overlaps_busy(start_dt: datetime, end_dt: datetime, busy: List[Tuple[datetime, datetime]]) -> bool:
    for busy_start, busy_end in busy:
        if not (busy_end <= start_dt or busy_start >= end_dt):
            return True
    return False


def get_busy_blocks(ctx: CalendarContext, time_min: datetime, time_max: datetime) -> List[Tuple[datetime, datetime]]:
    token = get_access_token(ctx)
    url = "https://www.googleapis.com/calendar/v3/freeBusy"
    payload = {
        "timeMin": _iso(time_min),
        "timeMax": _iso(time_max),
        "timeZone": ctx.business_timezone,
        "items": [{"id": ctx.calendar_id}],
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
    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        details = _http_error_details(exc)
        logger.error(
            "[CALENDAR freebusy] failed calendar_id=%s time_min=%s time_max=%s details=%s",
            ctx.calendar_id,
            payload["timeMin"],
            payload["timeMax"],
            details,
        )
        raise RuntimeError(f"Google freeBusy failed: {details}") from exc
    busy = data.get("calendars", {}).get(ctx.calendar_id, {}).get("busy", [])
    blocks: List[Tuple[datetime, datetime]] = []
    for item in busy:
        blocks.append((_parse_iso(item["start"], ctx), _parse_iso(item["end"], ctx)))
    return sorted(blocks, key=lambda x: x[0])


def _next_available_slots(
    ctx: CalendarContext,
    search_start: datetime,
    busy: List[Tuple[datetime, datetime]],
    duration_minutes: int,
    limit: int,
    horizon_days: int,
) -> List[Dict[str, str]]:
    if duration_minutes <= 0:
        duration_minutes = 30
    if limit <= 0:
        limit = 3

    now = datetime.now(timezone.utc)
    horizon_end = now + timedelta(days=horizon_days)
    tz = ZoneInfo(ctx.business_timezone)
    slots: List[Dict[str, str]] = []
    cursor_day = search_start.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = horizon_end.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    while cursor_day <= last_day and len(slots) < limit:
        if cursor_day.isoweekday() in _allowed_business_days(ctx):
            day_start, day_end = _business_window_for_day(cursor_day, ctx)
            slot_start = max(day_start, search_start.astimezone(tz), now.astimezone(tz))
            if slot_start.minute % 30 != 0:
                slot_start = slot_start.replace(
                    minute=(slot_start.minute // 30 + 1) * 30,
                    second=0,
                    microsecond=0,
                )
                if slot_start.minute == 60:
                    slot_start = slot_start.replace(hour=slot_start.hour + 1, minute=0)
            while slot_start + timedelta(minutes=duration_minutes) <= day_end:
                slot_end = slot_start + timedelta(minutes=duration_minutes)
                if not _overlaps_busy(slot_start.astimezone(timezone.utc), slot_end.astimezone(timezone.utc), busy):
                    slots.append({"start": slot_start.isoformat(), "end": slot_end.isoformat()})
                    if len(slots) >= limit:
                        break
                slot_start = slot_start + timedelta(minutes=30)
        cursor_day = cursor_day + timedelta(days=1)
    return slots


def _build_day_blocks(ctx: CalendarContext, slots: List[Dict[str, str]], max_days: int = 5) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[datetime]] = {}
    for slot in slots:
        try:
            start = _parse_iso(str(slot.get("start", "")), ctx)
            end = _parse_iso(str(slot.get("end", "")), ctx)
        except Exception:
            continue
        day_key = start.astimezone(ZoneInfo(ctx.business_timezone)).strftime("%Y-%m-%d")
        grouped.setdefault(day_key, []).append(start)
        grouped.setdefault(day_key, []).append(end)

    blocks: List[Dict[str, Any]] = []
    for day_key in list(sorted(grouped.keys()))[:max_days]:
        points = sorted(grouped[day_key])
        if len(points) < 2:
            continue
        ranges: List[str] = []
        for index in range(0, len(points) - 1, 2):
            s = points[index].astimezone(ZoneInfo(ctx.business_timezone))
            e = points[index + 1].astimezone(ZoneInfo(ctx.business_timezone))
            ranges.append(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
        pretty_day = points[0].astimezone(ZoneInfo(ctx.business_timezone)).strftime("%A %d %B")
        blocks.append({"day": pretty_day, "ranges": ranges})
    return blocks


def get_free_slots_next_two_weeks(
    duration_minutes: int = 30,
    max_slots: int = 10,
    context: CalendarContext | None = None,
    horizon_days: int = 14,
) -> Dict[str, Any]:
    ctx = context or DEFAULT_CONTEXT
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=horizon_days)
    busy = get_busy_blocks(ctx, now, end)
    allowed_days = _allowed_business_days(ctx)
    tz = ZoneInfo(ctx.business_timezone)
    slots: List[Dict[str, str]] = []

    cursor_day = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = end.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    while cursor_day <= last_day and len(slots) < max_slots:
        if cursor_day.isoweekday() in allowed_days:
            b_start, b_end = _business_window_for_day(cursor_day, ctx)
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
                        slots.append({"start": slot_start.isoformat(), "end": slot_end.isoformat()})
                        if len(slots) >= max_slots:
                            break
                        slot_start = slot_end
                    if len(slots) >= max_slots:
                        break
        cursor_day = cursor_day + timedelta(days=1)

    return {
        "timezone": ctx.business_timezone,
        "duration_minutes": duration_minutes,
        "horizon_days": horizon_days,
        "slots": slots,
    }


def get_fallback_slots_next_two_weeks(
    duration_minutes: int = 30,
    max_slots: int = 10,
    context: CalendarContext | None = None,
    horizon_days: int = 14,
) -> Dict[str, Any]:
    ctx = context or DEFAULT_CONTEXT
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=horizon_days)
    allowed_days = _allowed_business_days(ctx)
    tz = ZoneInfo(ctx.business_timezone)
    slots: List[Dict[str, str]] = []

    cursor_day = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = end.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    while cursor_day <= last_day and len(slots) < max_slots:
        if cursor_day.isoweekday() in allowed_days:
            b_start, b_end = _business_window_for_day(cursor_day, ctx)
            start = max(b_start, now.astimezone(tz))
            while start + timedelta(minutes=duration_minutes) <= b_end:
                end_slot = start + timedelta(minutes=duration_minutes)
                slots.append({"start": start.isoformat(), "end": end_slot.isoformat()})
                if len(slots) >= max_slots:
                    break
                start = end_slot
        cursor_day = cursor_day + timedelta(days=1)

    return {
        "timezone": ctx.business_timezone,
        "duration_minutes": duration_minutes,
        "horizon_days": horizon_days,
        "slots": slots,
        "fallback": True,
    }


def check_meeting_slot(
    preferred_start_iso: str,
    duration_minutes: int = 30,
    alternatives_limit: int = 3,
    context: CalendarContext | None = None,
    horizon_days: int = 14,
) -> Dict[str, Any]:
    ctx = context or DEFAULT_CONTEXT
    preferred_start = _parse_iso(preferred_start_iso, ctx).astimezone(timezone.utc)
    preferred_end = preferred_start + timedelta(minutes=duration_minutes)
    now = datetime.now(timezone.utc)
    horizon_end = now + timedelta(days=horizon_days)

    if preferred_start < now or preferred_start > horizon_end:
        return {
            "status": "outside_horizon",
            "timezone": ctx.business_timezone,
            "duration_minutes": duration_minutes,
            "next_slots": [],
            "day_blocks": [],
        }

    busy = get_busy_blocks(ctx, now, horizon_end)
    next_slots = _next_available_slots(ctx, preferred_start, busy, duration_minutes, alternatives_limit, horizon_days)
    day_blocks = _build_day_blocks(ctx, next_slots, max_days=5)

    if not _is_within_business_hours(preferred_start, preferred_end, ctx):
        return {
            "status": "outside_hours",
            "timezone": ctx.business_timezone,
            "duration_minutes": duration_minutes,
            "requested_slot": {
                "start": preferred_start.astimezone(ZoneInfo(ctx.business_timezone)).isoformat(),
                "end": preferred_end.astimezone(ZoneInfo(ctx.business_timezone)).isoformat(),
            },
            "next_slots": next_slots,
            "day_blocks": day_blocks,
        }

    if _overlaps_busy(preferred_start, preferred_end, busy):
        return {
            "status": "busy",
            "timezone": ctx.business_timezone,
            "duration_minutes": duration_minutes,
            "requested_slot": {
                "start": preferred_start.astimezone(ZoneInfo(ctx.business_timezone)).isoformat(),
                "end": preferred_end.astimezone(ZoneInfo(ctx.business_timezone)).isoformat(),
            },
            "next_slots": next_slots,
            "day_blocks": day_blocks,
        }

    return {
        "status": "free",
        "timezone": ctx.business_timezone,
        "duration_minutes": duration_minutes,
        "confirmed_slot": {
            "start": preferred_start.astimezone(ZoneInfo(ctx.business_timezone)).isoformat(),
            "end": preferred_end.astimezone(ZoneInfo(ctx.business_timezone)).isoformat(),
        },
    }


def create_meeting_event(
    title: str,
    description: str,
    start_iso: str,
    end_iso: str,
    attendees: Optional[List[str]] = None,
    meeting_link: str = "",
    context: CalendarContext | None = None,
    horizon_days: int = 14,
) -> Dict[str, Any]:
    ctx = context or DEFAULT_CONTEXT
    start_dt = _parse_iso(start_iso, ctx)
    end_dt = _parse_iso(end_iso, ctx)
    now = datetime.now(timezone.utc)
    if end_dt <= start_dt:
        raise ValueError("meeting end must be after meeting start")
    if start_dt > now + timedelta(days=horizon_days):
        return {"created": False, "reason": "outside_horizon"}

    if ctx.enforce_busy_recheck:
        busy = get_busy_blocks(ctx, start_dt, end_dt)
        if _overlaps_busy(start_dt, end_dt, busy):
            return {"created": False, "reason": "slot_not_free"}

    token = get_access_token(ctx)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{parse.quote(ctx.calendar_id, safe='')}/events"
    valid_attendees = [{"email": email.strip()} for email in attendees or [] if "@" in (email or "").strip()]
    full_description = description or ""
    if meeting_link:
        full_description = (full_description + f"\n\nMeeting link: {meeting_link}").strip()

    body = {
        "summary": title,
        "description": full_description,
        "start": {"dateTime": start_dt.astimezone(ZoneInfo(ctx.business_timezone)).isoformat()},
        "end": {"dateTime": end_dt.astimezone(ZoneInfo(ctx.business_timezone)).isoformat()},
        "location": meeting_link or "",
        "attendees": valid_attendees,
    }
    req = request.Request(
        f"{url}?sendUpdates=all",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", **_auth_headers()},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {"created": True, "event_id": data.get("id"), "html_link": data.get("htmlLink")}


def update_calendar_event_with_zoom(
    event_id: str,
    zoom_join_url: str,
    attendees: Optional[List[str]] = None,
    context: CalendarContext | None = None,
) -> Dict[str, Any]:
    ctx = context or DEFAULT_CONTEXT
    if not event_id:
        return {"updated": False, "reason": "missing_event_id"}
    if not zoom_join_url:
        return {"updated": False, "reason": "missing_zoom_link"}

    token = get_access_token(ctx)
    base = f"https://www.googleapis.com/calendar/v3/calendars/{parse.quote(ctx.calendar_id, safe='')}/events/{parse.quote(event_id, safe='')}"
    get_req = request.Request(base, headers={"Authorization": f"Bearer {token}", **_auth_headers()}, method="GET")
    with request.urlopen(get_req, timeout=20) as resp:
        current = json.loads(resp.read().decode("utf-8"))

    description = str(current.get("description", "") or "")
    if zoom_join_url not in description:
        description = (description + f"\n\nMeeting link: {zoom_join_url}").strip()

    patch_body = {"description": description, "location": zoom_join_url}
    valid_attendees = [{"email": email.strip()} for email in attendees or [] if "@" in (email or "").strip()]
    if valid_attendees:
        patch_body["attendees"] = valid_attendees

    patch_req = request.Request(
        f"{base}?sendUpdates=all",
        data=json.dumps(patch_body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", **_auth_headers()},
        method="PATCH",
    )
    with request.urlopen(patch_req, timeout=20) as resp:
        updated = json.loads(resp.read().decode("utf-8"))
    return {"updated": True, "html_link": updated.get("htmlLink")}


def validate_calendar_context(context: CalendarContext) -> Dict[str, Any]:
    token = get_access_token(context)
    return {"ok": True, "calendar_id": context.calendar_id, "token_preview": token[:8] + "..."}
