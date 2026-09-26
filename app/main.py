"""
FastAPI application — HTTP layer for the hall ticket barcode tool.

Endpoints:
    POST /upload-url    — Create a signed Supabase upload URL
    POST /process       — Start processing an uploaded Supabase object
  GET  /status/{id}   — Job progress + extracted student data (streamable)
  GET  /result/{id}   — Download the processed PDF

Jobs are stored in an in-process dict with TTL eviction (30 min after completion).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from redis import Redis

from .job_state import PROCESSING_QUEUE_KEY, QUEUE_KEY, load_job, save_job
from .pipeline import Job, JobStatus, process_pdf
from .storage import MongoStorage

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Job store ───────────────────────────────────────────────────────────────

_MAX_RETRIES = 2
jobs_enqueued = Counter("pdf_engine_jobs_enqueued_total", "Jobs added to the Redis queue")
jobs_completed = Counter("pdf_engine_jobs_completed_total", "Jobs completed successfully")
jobs_failed = Counter("pdf_engine_jobs_failed_total", "Jobs that exhausted retries")
jobs_retried = Counter("pdf_engine_jobs_retried_total", "Job retry attempts")
queue_depth = Gauge("pdf_engine_queue_depth", "Current Redis queue depth")
job_duration = Histogram("pdf_engine_job_duration_seconds", "PDF processing duration")


class UploadUrlRequest(BaseModel):
    filename: str


class ProcessRequest(BaseModel):
    job_id: str
    object_key: str
    filename: str


async def _queue_worker():
    """Consume Redis jobs without blocking the FastAPI event loop."""
    while True:
        queued = await asyncio.to_thread(
            app.state.redis.brpoplpush,
            QUEUE_KEY,
            PROCESSING_QUEUE_KEY,
            1,
        )
        if not queued:
            continue

        raw_job = queued
        payload = json.loads(raw_job)
        job = Job(job_id=payload["job_id"], filename=payload["filename"])
        logger.info("Starting queued job %s", job.job_id)
        started_at = time.perf_counter()
        try:
            await asyncio.to_thread(
                process_pdf,
                payload["object_key"],
                job,
                app.state.mongo,
                lambda current_job: save_job(app.state.redis, current_job),
            )
        finally:
            job_duration.observe(time.perf_counter() - started_at)
            queue_depth.set(await asyncio.to_thread(app.state.redis.llen, QUEUE_KEY))

        attempt = payload.get("attempt", 0)
        if job.status == JobStatus.ERROR and attempt < _MAX_RETRIES:
            payload["attempt"] = attempt + 1
            job.status = JobStatus.PROCESSING
            job.error_message = "Retry scheduled."
            save_job(app.state.redis, job)
            await asyncio.to_thread(app.state.mongo.update_job, job)
            app.state.redis.lpush(QUEUE_KEY, json.dumps(payload))
            jobs_retried.inc()
            logger.warning("Retrying job %s (attempt %d)", job.job_id, attempt + 1)
        elif job.status == JobStatus.DONE:
            jobs_completed.inc()
        else:
            jobs_failed.inc()
        await asyncio.to_thread(
            app.state.redis.lrem,
            PROCESSING_QUEUE_KEY,
            1,
            raw_job,
        )


# ─── App lifecycle ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the eviction loop on startup."""
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is not configured")
    app.state.redis = Redis.from_url(redis_url, decode_responses=True)
    app.state.redis.ping()
    app.state.mongo = MongoStorage()
    app.state.mongo.mark_processing_jobs_interrupted()
    task = asyncio.create_task(_queue_worker())
    try:
        yield
    finally:
        task.cancel()
        app.state.mongo.close()
        await asyncio.to_thread(app.state.redis.close)


app = FastAPI(
    title="Hall Ticket Barcode Worker",
    version="1.0.0",
    lifespan=lifespan,
)

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:3000,http://localhost:3000,https://forino-web-tools.vercel.app",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Endpoints ───────────────────────────────────────────────────────────────

async def _get_job(job_id: str) -> Job | None:
    """Load job state from Redis only. Fast — used for status/result checks."""
    saved = await asyncio.to_thread(load_job, app.state.redis, job_id)
    if saved is None:
        return None

    return Job(
        job_id=saved["job_id"],
        status=JobStatus(saved["status"]),
        total_pages=int(saved.get("total_pages", 0)),
        processed_pages=int(saved.get("processed_pages", 0)),
        error_message=saved.get("error_message", ""),
        filename=saved.get("filename", ""),
        barcode_success=int(saved.get("barcode_success", 0)),
        barcode_failed=int(saved.get("barcode_failed", 0)),
    )


async def _get_job_with_students(job_id: str) -> Job | None:
    """Load job state from Redis + student records from MongoDB. Used only for /extracted-data."""
    job = await _get_job(job_id)
    if job is None:
        return None
    job.students = await asyncio.to_thread(app.state.mongo.get_students, job_id)
    return job

@app.post("/upload-url")
async def create_upload_url(request: UploadUrlRequest):
    if not request.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    job_id = str(uuid.uuid4())
    upload = await asyncio.to_thread(
        app.state.mongo.create_upload_url, job_id, request.filename
    )
    return JSONResponse({"job_id": job_id, **upload})


@app.post("/process")
async def process_upload(request: ProcessRequest):
    """
    Start processing a hall ticket PDF already uploaded to Supabase.

    Returns { job_id } immediately. The PDF is processed in a background
    thread to avoid blocking the event loop.
    """
    if not request.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    expected_prefix = f"uploads/{request.job_id}/"
    if not request.object_key.startswith(expected_prefix):
        raise HTTPException(400, "Invalid upload object key.")

    job = Job(job_id=request.job_id, filename=request.filename)
    await asyncio.to_thread(app.state.mongo.create_job, job.job_id, job.filename)
    await asyncio.to_thread(save_job, app.state.redis, job)

    logger.info(
        f"Received PDF reference: {request.object_key} → job {request.job_id}"
    )

    await asyncio.to_thread(app.state.redis.rpush, QUEUE_KEY, json.dumps({
        "job_id": request.job_id,
        "object_key": request.object_key,
        "filename": request.filename,
        "attempt": 0,
    }))
    jobs_enqueued.inc()

    return JSONResponse({"job_id": request.job_id})


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """
    Get the current status of a processing job.

    Returns progress counts only — no student records. The frontend polls
    this every ~2 seconds to update the progress bar. To fetch student
    records, call /extracted-data/{job_id}.
    """
    job = await _get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")

    return JSONResponse({
        "job_id": job.job_id,
        "status": job.status.value,
        "total_pages": job.total_pages,
        "processed_pages": job.processed_pages,
        "barcode_success": job.barcode_success,
        "barcode_failed": job.barcode_failed,
        "error_message": job.error_message,
        "filename": job.filename,
    })


@app.get("/result/{job_id}")
async def get_result(job_id: str):
    """
    Create a signed URL for the processed PDF with barcodes stamped.
    
    Only available after the job reaches "done" status.
    The result remains in Supabase Storage so interrupted downloads can be retried.
    """
    job = await _get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")

    if job.status == JobStatus.PROCESSING:
        raise HTTPException(202, "Job is still processing.")

    if job.status == JobStatus.ERROR:
        raise HTTPException(500, f"Job failed: {job.error_message}")

    download_url = await asyncio.to_thread(
        app.state.mongo.get_result_url, job_id
    )
    if download_url is None:
        raise HTTPException(500, "No result PDF available.")

    # Build the output filename
    original = job.filename.rsplit(".", 1)[0] if job.filename else "halltickets"
    out_filename = f"{original}_barcoded.pdf"

    return JSONResponse({"download_url": download_url, "filename": out_filename})


@app.get("/health")
async def health():
    """Health check endpoint."""
    await asyncio.to_thread(app.state.redis.ping)
    queued_jobs = await asyncio.to_thread(app.state.redis.llen, QUEUE_KEY)
    processing_jobs = await asyncio.to_thread(
        app.state.redis.llen, PROCESSING_QUEUE_KEY
    )
    return {
        "status": "ok",
        "queued_jobs": queued_jobs,
        "processing_jobs": processing_jobs,
        "redis": "ok",
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics for queue and processing health."""
    queue_depth.set(
        await asyncio.to_thread(app.state.redis.llen, QUEUE_KEY)
    )
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/lookup/{barcode}")
async def lookup_barcode(barcode: str):
    """Find the extracted student record associated with a barcode value."""
    normalized_barcode = barcode.strip()
    if not normalized_barcode:
        raise HTTPException(400, "Barcode value is required.")

    document = await asyncio.to_thread(
        app.state.mongo.find_student_by_barcode, normalized_barcode
    )
    if document is None:
        raise HTTPException(404, "No student record found for this barcode.")

    return JSONResponse(document)


@app.get("/extracted-data/{job_id}")
async def get_extracted_data(job_id: str):
    """
    All student data extracted for a job, as JSON.

    Works while the job is still processing (returns what has been extracted
    so far — check `status`) and after it finishes. Each entry is one student
    record plus `barcode_placed`. Data is persisted in MongoDB.
    """
    job = await _get_job_with_students(job_id)
    if job is None:
        raise HTTPException(404, "Job not found (it may have expired — jobs are kept 30 minutes).")

    if job.status == JobStatus.ERROR:
        raise HTTPException(500, f"Job failed: {job.error_message}")

    return JSONResponse({
        "job_id": job.job_id,
        "filename": job.filename,
        "status": job.status.value,
        "is_complete": job.status == JobStatus.DONE,
        "student_count": len(job.students),
        "students": job.students,
    })
