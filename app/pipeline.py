"""
Pipeline orchestrator — runs steps 1–6 sequentially on a Supabase PDF object.

Called by the FastAPI background task. Updates the Job object's progress
as it processes each student, enabling the frontend to show progressive
status updates.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import fitz  # PyMuPDF

from .processing.split import split_by_student
from .processing.extract import extract_student, StudentRecord
from .processing.qr_locate import locate_qr_cell
from .processing.barcode_gen import generate_barcode_png
from .processing.stamp import stamp_barcode
from .processing.merge import finalize_pdf
from .storage import MongoStorage

logger = logging.getLogger(__name__)

_STUDENT_WRITE_BATCH_SIZE = 25


class JobStatus(str, Enum):
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    """Tracks the state of a PDF processing job."""
    job_id: str
    status: JobStatus = JobStatus.PROCESSING
    total_pages: int = 0
    processed_pages: int = 0
    students: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    filename: str = ""
    barcode_success: int = 0
    barcode_failed: int = 0


def process_pdf(
    source_key: str,
    job: Job,
    mongo: MongoStorage,
    status_sink: Callable[[Job], None] | None = None,
) -> None:
    """
    Main pipeline: process a hall ticket PDF end-to-end.

    Modifies the Job object in-place with progress and results.
    This function is designed to be called from a background task.

    Args:
        source_key: Supabase Storage object key for the uploaded PDF.
        job: The Job object to update with progress.
        mongo: MongoStorage instance for persistence.
        status_sink: Optional callback invoked after each progress update.
    """
    pdf_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".pdf",
            prefix="pdf-engine-",
            delete=False,
        ) as temp_file:
            pdf_path = temp_file.name
        mongo.download_source(source_key, pdf_path)

        # Open the PDF from disk so the full upload is not retained in RAM.
        doc = fitz.open(pdf_path)
        job.total_pages = len(doc)
        mongo.update_job(job)
        if status_sink:
            status_sink(job)

        logger.info(f"Job {job.job_id}: Processing {job.total_pages} pages")

        # Step 1: Split into per-student page ranges
        page_ranges = split_by_student(doc)
        logger.info(f"Job {job.job_id}: Found {len(page_ranges)} students")

        # Steps 2–5: Process each student
        pending_students: list[tuple[int, dict[str, Any]]] = []
        for i, page_range in enumerate(page_ranges):
            try:
                start_page, end_page = page_range

                # Step 2: Extract student data
                record = extract_student(doc, page_range)

                # Step 3: Locate the QR cell on the first page of this student
                page = doc[start_page]
                qr_cell = locate_qr_cell(page)

                barcode_placed = False
                if qr_cell is not None and record.uucms:
                    # Step 4: Generate the barcode
                    barcode_png = generate_barcode_png(
                        uucms=record.uucms,
                        cell_width=qr_cell.width,
                        cell_height=qr_cell.height,
                    )

                    # Step 5: Stamp the barcode onto the page
                    barcode_placed = stamp_barcode(page, qr_cell, barcode_png)
                if barcode_placed:
                    job.barcode_success += 1
                else:
                    job.barcode_failed += 1
                    if not record.uucms:
                        logger.warning(
                            f"Job {job.job_id}: Page {start_page + 1}: "
                            "No UUCMS found, skipping barcode."
                        )

                # Update job progress
                student_dict = record.to_dict()
                student_dict["barcode_placed"] = barcode_placed
                pending_students.append((i, student_dict))
                if len(pending_students) >= _STUDENT_WRITE_BATCH_SIZE:
                    mongo.save_students_batch(
                        job_id=job.job_id,
                        filename=job.filename,
                        students=pending_students,
                    )
                    pending_students.clear()
                job.processed_pages = end_page + 1
                if (i + 1) % 10 == 0 or i == len(page_ranges) - 1:
                    mongo.update_job(job)
                    if status_sink:
                        status_sink(job)
                    logger.info(
                        f"Job {job.job_id}: Progress {i + 1}/{len(page_ranges)}"
                    )

            except Exception as e:
                logger.error(
                    f"Job {job.job_id}: Error processing pages "
                    f"{page_range}: {e}",
                    exc_info=True,
                )
                job.barcode_failed += 1
                job.processed_pages = page_range[1] + 1

        mongo.save_students_batch(
            job_id=job.job_id,
            filename=job.filename,
            students=pending_students,
        )

        logger.info(f"Job {job.job_id}: Finalizing modified PDF")
        result_pdf = finalize_pdf(doc)
        logger.info(
            f"Job {job.job_id}: PDF finalized ({len(result_pdf)} bytes); "
            "saving result to Supabase Storage"
        )
        doc.close()
        mongo.save_result(job.job_id, result_pdf)
        logger.info(f"Job {job.job_id}: Result saved to Supabase Storage")

        job.status = JobStatus.DONE
        mongo.update_job(job)
        if status_sink:
            status_sink(job)
        logger.info(
            f"Job {job.job_id}: Complete. "
            f"{job.barcode_success} barcodes placed, "
            f"{job.barcode_failed} failed."
        )

    except Exception as e:
        job.status = JobStatus.ERROR
        job.error_message = str(e)
        mongo.update_job(job)
        if status_sink:
            status_sink(job)
        logger.error(
            f"Job {job.job_id}: Pipeline failed: {e}",
            exc_info=True,
        )
    finally:
        try:
            os.unlink(pdf_path)
        except FileNotFoundError:
            pass
