from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app_config import DEBUG_LOG_MAX_CHARS


def read_log_tail(path_value: str, max_chars: int = DEBUG_LOG_MAX_CHARS) -> dict[str, Any]:
    path = Path(path_value)
    if not path.exists():
        return {"path": str(path), "exists": False, "content": "", "size": 0}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"path": str(path), "exists": True, "content": f"Unable to read log: {exc}", "size": 0}
    content = raw[-max_chars:] if len(raw) > max_chars else raw
    return {"path": str(path), "exists": True, "content": content, "size": len(raw)}


def truncate_log(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.exists():
        return {"path": str(path), "exists": False, "cleared": False}
    path.write_text("", encoding="utf-8")
    return {"path": str(path), "exists": True, "cleared": True}


def _parse_debug_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _summarize_debug_fields(fields: dict[str, Any]) -> str:
    preferred_keys = (
        "text",
        "new_state",
        "old_state",
        "room_name",
        "provider",
        "reason",
        "name",
        "status",
        "output",
        "notes",
    )
    parts: list[str] = []
    for key in preferred_keys:
        value = fields.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={rendered}")
    if not parts:
        remaining_keys = [key for key in fields.keys() if fields.get(key) not in (None, "", [], {})]
        for key in remaining_keys[:4]:
            value = fields.get(key)
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            parts.append(f"{key}={rendered}")
    summary = " | ".join(parts)
    return summary if len(summary) <= 220 else summary[:217] + "..."


def _parse_debug_log_line(line: str) -> dict[str, Any] | None:
    raw = line.strip()
    if not raw:
        return None
    parts = raw.split(" | ")
    if len(parts) < 3:
        return None
    try:
        timestamp = datetime.fromisoformat(parts[0])
    except ValueError:
        return None
    fields: dict[str, Any] = {}
    for item in parts[3:]:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        fields[key] = _parse_debug_value(value)
    return {
        "raw_line": raw,
        "timestamp": parts[0],
        "dt": timestamp,
        "category": parts[1],
        "event": parts[2],
        "fields": fields,
    }


def _latest_debug_session_entries(raw_content: str) -> list[dict[str, Any]]:
    parsed = [
        entry
        for entry in (_parse_debug_log_line(line) for line in raw_content.splitlines())
        if entry is not None
    ]
    if not parsed:
        return []
    session_start_index = 0
    for idx, entry in enumerate(parsed):
        if entry["category"] == "CALL" and entry["event"] == "session_started":
            session_start_index = idx
    return parsed[session_start_index:]


def _debug_is_user_stop(entry: dict[str, Any]) -> bool:
    return (
        entry["category"] == "TURN"
        and entry["event"] in {"user_state_changed", "USER_STOPPED_SPEAKING"}
        and str(entry["fields"].get("new_state") or "") == "listening"
    )


def _debug_is_user_committed(entry: dict[str, Any]) -> bool:
    return entry["category"] == "TRANSCRIPT" and entry["event"] == "USER_COMMITTED"


def _debug_is_agent_speaking(entry: dict[str, Any]) -> bool:
    return (
        entry["category"] == "AGENT"
        and entry["event"] == "agent_state_changed"
        and str(entry["fields"].get("new_state") or "") == "speaking"
    )


def _build_bridge_metrics(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    bridges: list[dict[str, Any]] = []
    last_user_committed_index = -1

    for idx, entry in enumerate(timeline):
        if not _debug_is_user_committed(entry):
            continue

        speech_end = None
        for back_idx in range(idx - 1, last_user_committed_index, -1):
            candidate = timeline[back_idx]
            if _debug_is_user_stop(candidate):
                speech_end = candidate
                break

        agent_speaking = None
        for forward_idx in range(idx + 1, len(timeline)):
            candidate = timeline[forward_idx]
            if _debug_is_agent_speaking(candidate):
                agent_speaking = candidate
                break
            if _debug_is_user_committed(candidate):
                break

        last_user_committed_index = idx
        if speech_end is None or agent_speaking is None:
            continue

        user_text = str(entry["fields"].get("text") or "").strip()
        speech_end_to_committed_sec = round(entry["elapsed_sec"] - speech_end["elapsed_sec"], 3)
        committed_to_speaking_sec = round(agent_speaking["elapsed_sec"] - entry["elapsed_sec"], 3)
        full_bridge_sec = round(agent_speaking["elapsed_sec"] - speech_end["elapsed_sec"], 3)
        bridges.append(
            {
                "turn_number": len(bridges) + 1,
                "user_text": user_text,
                "speech_end_elapsed_sec": speech_end["elapsed_sec"],
                "user_committed_elapsed_sec": entry["elapsed_sec"],
                "agent_speaking_elapsed_sec": agent_speaking["elapsed_sec"],
                "speech_end_to_committed_sec": speech_end_to_committed_sec,
                "committed_to_speaking_sec": committed_to_speaking_sec,
                "full_bridge_sec": full_bridge_sec,
            }
        )

    if not bridges:
        return {"bridges": [], "summary": {}}

    settle = [item["speech_end_to_committed_sec"] for item in bridges]
    respond = [item["committed_to_speaking_sec"] for item in bridges]
    full = [item["full_bridge_sec"] for item in bridges]
    summary = {
        "bridge_count": len(bridges),
        "average_speech_end_to_committed_sec": round(sum(settle) / len(settle), 3),
        "average_committed_to_speaking_sec": round(sum(respond) / len(respond), 3),
        "average_full_bridge_sec": round(sum(full) / len(full), 3),
        "max_full_bridge_sec": round(max(full), 3),
        "min_full_bridge_sec": round(min(full), 3),
    }
    return {"bridges": bridges, "summary": summary}


def build_debug_timeline(path_value: str, max_chars: int = DEBUG_LOG_MAX_CHARS) -> dict[str, Any]:
    raw_log = read_log_tail(path_value, max_chars=max_chars)
    entries = _latest_debug_session_entries(raw_log.get("content", ""))
    if not entries:
        return {
            "log": raw_log,
            "entries": [],
            "summary": {},
            "bridges": [],
        }

    first_dt = entries[0]["dt"]
    previous_dt = None
    timeline: list[dict[str, Any]] = []
    for entry in entries:
        current_dt = entry["dt"]
        elapsed_sec = round((current_dt - first_dt).total_seconds(), 3)
        delta_prev_sec = round((current_dt - previous_dt).total_seconds(), 3) if previous_dt else 0.0
        previous_dt = current_dt
        timeline.append(
            {
                "timestamp": entry["timestamp"],
                "category": entry["category"],
                "event": entry["event"],
                "elapsed_sec": elapsed_sec,
                "delta_prev_sec": delta_prev_sec,
                "fields": entry["fields"],
                "fields_summary": _summarize_debug_fields(entry["fields"]),
                "raw_line": entry["raw_line"],
            }
        )

    def _first_elapsed(category: str, event: str, field_key: str = "", field_value: str = "") -> float | None:
        for item in timeline:
            if item["category"] != category or item["event"] != event:
                continue
            if field_key and str(item["fields"].get(field_key, "")) != field_value:
                continue
            return item["elapsed_sec"]
        return None

    bridge_metrics = _build_bridge_metrics(timeline)
    summary = {
        "events_count": len(timeline),
        "session_duration_sec": timeline[-1]["elapsed_sec"],
        "first_agent_speaking_sec": _first_elapsed("AGENT", "agent_state_changed", "new_state", "speaking"),
        "first_assistant_committed_sec": _first_elapsed("TRANSCRIPT", "ASSISTANT_COMMITTED"),
        "first_user_speaking_sec": _first_elapsed("TURN", "user_state_changed", "new_state", "speaking"),
        "first_user_committed_sec": _first_elapsed("TRANSCRIPT", "USER_COMMITTED"),
        "first_tool_executed_sec": _first_elapsed("TOOL", "TOOL_EXECUTED"),
        "shutdown_started_sec": _first_elapsed("CALL", "shutdown_started"),
        "shutdown_finished_sec": _first_elapsed("CALL", "shutdown_finished"),
        **bridge_metrics["summary"],
    }

    raw_log["content"] = "\n".join(item["raw_line"] for item in timeline)
    return {"log": raw_log, "entries": timeline, "summary": summary, "bridges": bridge_metrics["bridges"]}


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
