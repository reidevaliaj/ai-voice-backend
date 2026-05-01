from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outgoing_models import OutgoingBulkBatch, OutgoingBulkItem, OutgoingCall
from services.outgoing import normalize_outgoing_template_tag_key
from services.tenants import normalize_phone_number

BULK_BATCH_ACTIVE_STATUSES = {"queued", "running", "stopping"}
BULK_BATCH_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
BULK_ITEM_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
OUTGOING_CALL_ACTIVE_STATUSES = {
    "queued",
    "dialing",
    "initiated",
    "answered",
    "awaiting_machine_detection",
    "human_detected",
    "livekit_transfer_requested",
    "bridged",
}
REQUIRED_BULK_HEADERS = {"number", "website"}
NUMBER_HEADER_ALIASES = ("number", "phone", "target_number")
NAME_HEADER_ALIASES = ("name", "target_name")
NOTES_HEADER_ALIASES = ("notes", "note")
DEFAULT_BULK_DELAY_SECONDS = 20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_header_lookup(headers: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for header in headers:
        normalized = normalize_outgoing_template_tag_key(header)
        if normalized and normalized not in lookup:
            lookup[normalized] = header
    return lookup


def _first_present_key(lookup: dict[str, str], candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
    return ""


def parse_bulk_csv_upload(filename: str, raw_bytes: bytes) -> dict[str, Any]:
    if not raw_bytes:
        raise ValueError("Upload a CSV file first")
    try:
        decoded = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = raw_bytes.decode("latin-1")
    reader = csv.DictReader(io.StringIO(decoded))
    headers = [str(item or "").strip() for item in (reader.fieldnames or []) if str(item or "").strip()]
    normalized_headers = {normalize_outgoing_template_tag_key(item) for item in headers}
    missing = [header for header in REQUIRED_BULK_HEADERS if header not in normalized_headers]
    if missing:
        raise ValueError(f"CSV must include these headers: {', '.join(sorted(REQUIRED_BULK_HEADERS))}")
    header_lookup = _normalized_header_lookup(headers)
    number_header = _first_present_key(header_lookup, NUMBER_HEADER_ALIASES)
    if not number_header:
        raise ValueError("CSV must include a number column")
    name_header = _first_present_key(header_lookup, NAME_HEADER_ALIASES)
    notes_header = _first_present_key(header_lookup, NOTES_HEADER_ALIASES)

    rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    source_row_count = 0
    for index, row in enumerate(reader, start=2):
        source_row_count += 1
        row_map = {str(key or "").strip(): str(value or "").strip() for key, value in dict(row or {}).items()}
        target_number = normalize_phone_number(row_map.get(number_header, ""))
        if not target_number:
            invalid_rows.append({"row_index": index, "error": "Missing number", "row": row_map})
            continue
        target_name = row_map.get(name_header, "").strip() if name_header else ""
        notes = row_map.get(notes_header, "").strip() if notes_header else ""
        row_tags: dict[str, str] = {}
        for header, value in row_map.items():
            normalized_key = normalize_outgoing_template_tag_key(header)
            if not normalized_key or not value:
                continue
            if header == number_header:
                continue
            if name_header and header == name_header:
                continue
            if notes_header and header == notes_header:
                continue
            row_tags[normalized_key] = value
        rows.append(
            {
                "row_index": index,
                "target_number": target_number,
                "target_name": target_name,
                "notes": notes,
                "row_tags_json": row_tags,
                "raw_row_json": row_map,
            }
        )

    return {
        "filename": filename,
        "headers": headers,
        "rows": rows,
        "invalid_rows": invalid_rows,
        "source_row_count": source_row_count,
    }


def create_bulk_batch(
    session: Session,
    *,
    tenant: Any,
    profile: Any,
    provider: str,
    from_number: str,
    source_filename: str,
    source_headers: list[str],
    rows: list[dict[str, Any]],
    max_calls: int,
    delay_seconds: int = DEFAULT_BULK_DELAY_SECONDS,
    extra_json: dict[str, Any] | None = None,
) -> OutgoingBulkBatch:
    normalized_max_calls = max(1, int(max_calls or 1))
    selected_rows = list(rows[:normalized_max_calls])
    batch = OutgoingBulkBatch(
        profile_id=profile.id if profile else None,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        provider=str(provider or "telnyx").strip().lower() or "telnyx",
        from_number=normalize_phone_number(from_number),
        source_filename=str(source_filename or "").strip(),
        source_headers_json=list(source_headers or []),
        max_calls=normalized_max_calls,
        delay_seconds=max(1, int(delay_seconds or DEFAULT_BULK_DELAY_SECONDS)),
        total_rows=len(selected_rows),
        status="queued",
        next_run_at=_utcnow(),
        extra_json=dict(extra_json or {}),
    )
    session.add(batch)
    session.flush()
    for offset, row in enumerate(selected_rows, start=1):
        session.add(
            OutgoingBulkItem(
                batch_id=batch.id,
                tenant_id=tenant.id,
                tenant_slug=tenant.slug,
                row_index=int(row.get("row_index") or offset),
                target_number=row["target_number"],
                target_name=str(row.get("target_name") or "").strip(),
                notes=str(row.get("notes") or "").strip(),
                row_tags_json=dict(row.get("row_tags_json") or {}),
                raw_row_json=dict(row.get("raw_row_json") or {}),
                status="queued",
            )
        )
    session.flush()
    refresh_bulk_batch_counts(session, batch)
    return batch


def list_recent_bulk_batches(session: Session, tenant_id: str, limit: int = 20) -> list[OutgoingBulkBatch]:
    stmt = (
        select(OutgoingBulkBatch)
        .where(OutgoingBulkBatch.tenant_id == tenant_id)
        .order_by(OutgoingBulkBatch.created_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def get_bulk_batch(session: Session, batch_id: str, tenant_id: str = "") -> OutgoingBulkBatch | None:
    batch = session.get(OutgoingBulkBatch, batch_id)
    if batch is None:
        return None
    if tenant_id and batch.tenant_id != tenant_id:
        return None
    return batch


def list_bulk_items(session: Session, batch_id: str) -> list[OutgoingBulkItem]:
    stmt = (
        select(OutgoingBulkItem)
        .where(OutgoingBulkItem.batch_id == batch_id)
        .order_by(OutgoingBulkItem.row_index.asc(), OutgoingBulkItem.created_at.asc())
    )
    return list(session.scalars(stmt))


def request_stop_bulk_batch(session: Session, batch: OutgoingBulkBatch) -> OutgoingBulkBatch:
    batch.stop_requested = True
    if batch.status == "queued":
        batch.status = "stopping"
    elif batch.status == "running":
        batch.status = "stopping"
    batch.updated_at = _utcnow()
    session.flush()
    return batch


def refresh_bulk_batch_counts(session: Session, batch: OutgoingBulkBatch) -> OutgoingBulkBatch:
    items = list_bulk_items(session, batch.id)
    batch.total_rows = len(items)
    batch.launched_count = sum(1 for item in items if item.status in {"launched", "completed", "failed"})
    batch.completed_count = sum(1 for item in items if item.status == "completed")
    batch.failed_count = sum(1 for item in items if item.status == "failed")
    batch.stopped_count = sum(1 for item in items if item.status == "stopped")
    batch.updated_at = _utcnow()
    session.flush()
    return batch


def get_active_bulk_item(session: Session, batch_id: str) -> OutgoingBulkItem | None:
    stmt = (
        select(OutgoingBulkItem)
        .where(
            OutgoingBulkItem.batch_id == batch_id,
            OutgoingBulkItem.status == "launched",
        )
        .order_by(OutgoingBulkItem.launched_at.asc(), OutgoingBulkItem.created_at.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def get_next_bulk_item(session: Session, batch_id: str) -> OutgoingBulkItem | None:
    stmt = (
        select(OutgoingBulkItem)
        .where(
            OutgoingBulkItem.batch_id == batch_id,
            OutgoingBulkItem.status == "queued",
        )
        .order_by(OutgoingBulkItem.row_index.asc(), OutgoingBulkItem.created_at.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def get_active_outgoing_call_for_tenant(session: Session, tenant_id: str) -> OutgoingCall | None:
    stmt = (
        select(OutgoingCall)
        .where(
            OutgoingCall.tenant_id == tenant_id,
            OutgoingCall.status.in_(OUTGOING_CALL_ACTIVE_STATUSES),
        )
        .order_by(OutgoingCall.created_at.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def mark_bulk_item_launched(session: Session, item: OutgoingBulkItem, outgoing_call_id: str) -> OutgoingBulkItem:
    item.status = "launched"
    item.outgoing_call_id = outgoing_call_id
    item.launched_at = item.launched_at or _utcnow()
    item.updated_at = _utcnow()
    session.flush()
    return item


def sync_bulk_item_from_call(session: Session, item: OutgoingBulkItem, call: OutgoingCall | None) -> OutgoingBulkItem:
    if call is None:
        return item
    item.outgoing_call_id = call.id
    if call.status in OUTGOING_CALL_ACTIVE_STATUSES:
        if item.status != "launched":
            item.status = "launched"
            item.launched_at = item.launched_at or _utcnow()
    elif call.status == "completed":
        item.status = "completed"
        item.completed_at = call.ended_at or _utcnow()
        item.last_error = ""
    elif call.status in {"failed", "machine_detected"}:
        item.status = "failed"
        item.completed_at = call.ended_at or _utcnow()
        item.last_error = call.last_error or call.status
    item.updated_at = _utcnow()
    session.flush()
    return item


def finalize_bulk_batch(session: Session, batch: OutgoingBulkBatch) -> OutgoingBulkBatch:
    items = list_bulk_items(session, batch.id)
    if batch.stop_requested:
        for item in items:
            if item.status == "queued":
                item.status = "stopped"
                item.completed_at = _utcnow()
                item.updated_at = _utcnow()
        batch.status = "stopped"
    elif any(item.status == "failed" for item in items) and all(item.status in BULK_ITEM_TERMINAL_STATUSES for item in items):
        batch.status = "completed"
    else:
        batch.status = "completed"
    batch.finished_at = batch.finished_at or _utcnow()
    batch.next_run_at = None
    refresh_bulk_batch_counts(session, batch)
    session.flush()
    return batch


def schedule_next_bulk_run(session: Session, batch: OutgoingBulkBatch) -> OutgoingBulkBatch:
    batch.next_run_at = _utcnow() + timedelta(seconds=max(1, int(batch.delay_seconds or DEFAULT_BULK_DELAY_SECONDS)))
    batch.updated_at = _utcnow()
    session.flush()
    return batch


def get_runnable_bulk_batches(session: Session, limit: int = 10) -> list[OutgoingBulkBatch]:
    now = _utcnow()
    stmt = (
        select(OutgoingBulkBatch)
        .where(
            OutgoingBulkBatch.status.in_(BULK_BATCH_ACTIVE_STATUSES),
        )
        .order_by(OutgoingBulkBatch.created_at.asc())
        .limit(limit)
    )
    batches = list(session.scalars(stmt))
    return [batch for batch in batches if batch.next_run_at is None or batch.next_run_at <= now or batch.status == "stopping"]
