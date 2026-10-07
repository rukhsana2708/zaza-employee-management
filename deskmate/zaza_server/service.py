"""Batch processing: per-record validation, device/employee binding, and
idempotent upsert through the repository. Results are returned in the same
order as the submitted records."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from deskmate.zaza.sync.protocol import (
    SYNC_RECORD_ADAPTER,
    BatchSummary,
    RecordResult,
    SyncBatchRequest,
    SyncBatchResponse,
)

from .repository import CentralRepository, DeviceRecord, IncomingRecord, UpsertOutcome

logger = logging.getLogger("zaza_server.sync")

_MAX_ERROR_LEN = 300


def content_hash(record: Any) -> str:
    """Hash of everything that describes the record's state at a version
    (excluding the version number and updated_at bookkeeping)."""
    body = {
        "employee_id": record.employee_id,
        "local_seq": record.local_seq,
        "created_at": record.created_at.isoformat(),
        "data": record.data.model_dump(mode="json"),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)[:_MAX_ERROR_LEN]


class SyncService:
    def __init__(self, repo: CentralRepository) -> None:
        self.repo = repo

    def process_batch(self, device: DeviceRecord, batch: SyncBatchRequest) -> SyncBatchResponse:
        results: list[RecordResult | None] = [None] * len(batch.records)
        pending: list[tuple[int, IncomingRecord]] = []

        for index, raw in enumerate(batch.records):
            record_type = str(raw.get("record_type", ""))[:64]
            record_id = str(raw.get("record_id", ""))[:64]
            version = raw.get("record_version") if isinstance(raw.get("record_version"), int) else None
            try:
                record = SYNC_RECORD_ADAPTER.validate_python(raw)
            except ValidationError as exc:
                results[index] = RecordResult(
                    record_type=record_type, record_id=record_id, record_version=version,
                    status="rejected", error=validation_message(exc),
                )
                continue
            error = None
            if record.device_id != device.device_id:
                error = "record device_id does not match the authenticated device"
            elif record.employee_id is not None and record.employee_id != device.employee_id:
                error = "record employee_id does not match the device's registered employee"
            if error:
                results[index] = RecordResult(
                    record_type=record.record_type, record_id=str(record.record_id),
                    record_version=record.record_version, status="rejected", error=error,
                )
                continue
            pending.append((
                index,
                IncomingRecord(
                    record_type=record.record_type,
                    record_id=str(record.record_id),
                    record_version=record.record_version,
                    device_id=device.device_id,
                    employee_id=device.employee_id,
                    local_seq=record.local_seq,
                    content_hash=content_hash(record),
                    payload_json=record.model_dump_json(),
                    batch_id=str(batch.batch_id),
                ),
            ))

        outcomes: list[UpsertOutcome] = self.repo.upsert_records([inc for _, inc in pending])
        for (index, incoming), outcome in zip(pending, outcomes):
            results[index] = RecordResult(
                record_type=incoming.record_type, record_id=incoming.record_id,
                record_version=incoming.record_version, status=outcome.status,
                server_version=outcome.server_version, error=outcome.error,
            )

        final = [r for r in results if r is not None]
        counts = Counter(r.status for r in final)
        summary = BatchSummary(**{status: counts.get(status, 0) for status in BatchSummary.model_fields})
        logger.info(
            "batch %s device=%s records=%d %s",
            batch.batch_id, device.device_id, len(final),
            " ".join(f"{k}={v}" for k, v in summary.model_dump().items() if v),
        )
        return SyncBatchResponse(
            batch_id=batch.batch_id,
            server_time=datetime.now(timezone.utc),
            results=final,
            summary=summary,
        )
