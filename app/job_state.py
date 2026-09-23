"""Redis-backed job state and queue helpers."""

from __future__ import annotations

import json
from typing import Any

from .pipeline import Job, JobStatus


QUEUE_KEY = "pdf-engine:queue"
PROCESSING_QUEUE_KEY = "pdf-engine:queue:processing"
JOB_KEY_PREFIX = "pdf-engine:job:"
JOB_TTL_SECONDS = 60 * 60


def job_key(job_id: str) -> str:
    return f"{JOB_KEY_PREFIX}{job_id}"


def snapshot(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "total_pages": job.total_pages,
        "processed_pages": job.processed_pages,
        "barcode_success": job.barcode_success,
        "barcode_failed": job.barcode_failed,
        "error_message": job.error_message,
        "filename": job.filename,
        "created_at": job.created_at,
    }


def save_job(redis: Any, job: Job) -> None:
    key = job_key(job.job_id)
    redis.set(key, json.dumps(snapshot(job)))
    redis.expire(key, JOB_TTL_SECONDS)


def load_job(redis: Any, job_id: str) -> dict[str, Any] | None:
    value = redis.get(job_key(job_id))
    if not value:
        return None
    return json.loads(value)
