from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app_config import OUTGOING_BULK_WORKER_POLL_SECONDS
from db import db_session
from outgoing_db import init_outgoing_db, outgoing_db_session
from services.outgoing import ensure_outgoing_profile, get_outgoing_call
from services.outgoing_bulk import (
    BULK_ITEM_TERMINAL_STATUSES,
    finalize_bulk_batch,
    get_active_bulk_item,
    get_active_outgoing_call_for_tenant,
    get_next_bulk_item,
    get_runnable_bulk_batches,
    list_bulk_items,
    mark_bulk_item_launched,
    refresh_bulk_batch_counts,
    schedule_next_bulk_run,
    sync_bulk_item_from_call,
)
from services.outgoing_launch import OutgoingLaunchError, OutgoingLaunchRequest, launch_outgoing_call
from services.tenants import get_active_config, get_tenant_by_id

logger = logging.getLogger("bulk-outgoing-worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_tags_raw(row_tags: dict[str, str]) -> str:
    lines: list[str] = []
    for key, value in row_tags.items():
        if key in {"website", "reason", "specific"}:
            continue
        if not str(value or "").strip():
            continue
        lines.append(f"{key}={str(value).strip()}")
    return "\n".join(lines)


def _mark_item_failed(item, message: str, *, call_id: str = "") -> None:
    item.status = "failed"
    item.last_error = str(message or "").strip()
    item.outgoing_call_id = call_id or item.outgoing_call_id
    item.completed_at = _utcnow()
    item.updated_at = _utcnow()


def _step_batch(primary_db, outgoing_db, batch) -> None:
    refresh_bulk_batch_counts(outgoing_db, batch)

    active_item = get_active_bulk_item(outgoing_db, batch.id)
    if active_item is not None and active_item.outgoing_call_id:
        call = get_outgoing_call(outgoing_db, outgoing_call_id=active_item.outgoing_call_id)
        sync_bulk_item_from_call(outgoing_db, active_item, call)
        if active_item.status in BULK_ITEM_TERMINAL_STATUSES:
            refresh_bulk_batch_counts(outgoing_db, batch)
            if batch.stop_requested:
                finalize_bulk_batch(outgoing_db, batch)
                return
            if get_next_bulk_item(outgoing_db, batch.id) is None:
                finalize_bulk_batch(outgoing_db, batch)
                return
            batch.status = "queued"
            schedule_next_bulk_run(outgoing_db, batch)
            return

        batch.status = "stopping" if batch.stop_requested else "running"
        batch.updated_at = _utcnow()
        outgoing_db.flush()
        return

    if batch.stop_requested:
        finalize_bulk_batch(outgoing_db, batch)
        return

    if get_active_outgoing_call_for_tenant(outgoing_db, batch.tenant_id) is not None:
        batch.status = "running"
        batch.updated_at = _utcnow()
        outgoing_db.flush()
        return

    next_item = get_next_bulk_item(outgoing_db, batch.id)
    if next_item is None:
        finalize_bulk_batch(outgoing_db, batch)
        return

    tenant = get_tenant_by_id(primary_db, batch.tenant_id)
    if tenant is None:
        batch.status = "failed"
        batch.last_error = "Tenant not found for bulk batch"
        batch.finished_at = _utcnow()
        _mark_item_failed(next_item, batch.last_error)
        refresh_bulk_batch_counts(outgoing_db, batch)
        return

    active_config = get_active_config(primary_db, tenant.id)
    profile = ensure_outgoing_profile(outgoing_db, tenant, active_config=active_config)
    row_tags = dict(next_item.row_tags_json or {})
    try:
        call = asyncio.run(
            launch_outgoing_call(
                outgoing_db,
                OutgoingLaunchRequest(
                    tenant=tenant,
                    profile=profile,
                    active_config=active_config,
                    target_number=next_item.target_number,
                    target_name=next_item.target_name,
                    notes=next_item.notes,
                    from_number=batch.from_number,
                    tag_website=str(row_tags.get("website") or ""),
                    tag_reason=str(row_tags.get("reason") or ""),
                    tag_specific=str(row_tags.get("specific") or ""),
                    extra_tags_raw=_row_tags_raw(row_tags),
                    extra_json={
                        "launch_source": "bulk",
                        "bulk_batch_id": batch.id,
                        "bulk_item_id": next_item.id,
                        "bulk_row_index": next_item.row_index,
                    },
                ),
            )
        )
        mark_bulk_item_launched(outgoing_db, next_item, call.id)
        batch.status = "running"
        batch.started_at = batch.started_at or _utcnow()
        batch.next_run_at = None
        batch.last_error = ""
        refresh_bulk_batch_counts(outgoing_db, batch)
    except OutgoingLaunchError as exc:
        if exc.call is not None:
            next_item.outgoing_call_id = exc.call.id
        _mark_item_failed(next_item, str(exc), call_id=next_item.outgoing_call_id or "")
        batch.status = "queued"
        batch.last_error = str(exc)
        refresh_bulk_batch_counts(outgoing_db, batch)
        if get_next_bulk_item(outgoing_db, batch.id) is None:
            finalize_bulk_batch(outgoing_db, batch)
        else:
            schedule_next_bulk_run(outgoing_db, batch)


def run_loop() -> None:
    init_outgoing_db()
    poll_seconds = max(1.0, float(OUTGOING_BULK_WORKER_POLL_SECONDS or 3))
    logger.info("bulk outgoing worker started with poll_seconds=%s", poll_seconds)
    while True:
        try:
            with db_session() as primary_db, outgoing_db_session() as outgoing_db:
                for batch in get_runnable_bulk_batches(outgoing_db):
                    _step_batch(primary_db, outgoing_db, batch)
        except Exception:
            logger.exception("bulk outgoing worker loop failed")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    run_loop()
