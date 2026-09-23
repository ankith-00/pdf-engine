"""MongoDB persistence for extracted student records."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import boto3
from pymongo import ASCENDING, MongoClient, UpdateOne


COLLECTION_NAME = "candidate-data"
JOBS_COLLECTION_NAME = "pdf-processor-jobs"


class MongoStorage:
    def __init__(self) -> None:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI is not configured")

        database_name = os.getenv("MONGODB_DATABASE", "ocr-module")
        self.s3_bucket = os.getenv("SUPABASE_S3_BUCKET")
        s3_endpoint = os.getenv("SUPABASE_S3_ENDPOINT")
        s3_region = os.getenv("SUPABASE_S3_REGION", "us-east-1")
        s3_access_key = os.getenv("SUPABASE_S3_ACCESS_KEY_ID")
        s3_secret_key = os.getenv("SUPABASE_S3_SECRET_ACCESS_KEY")
        if not all((self.s3_bucket, s3_endpoint, s3_access_key, s3_secret_key)):
            raise RuntimeError(
                "Supabase S3 storage is not configured. Set "
                "SUPABASE_S3_BUCKET, SUPABASE_S3_ENDPOINT, "
                "SUPABASE_S3_REGION, SUPABASE_S3_ACCESS_KEY_ID, and "
                "SUPABASE_S3_SECRET_ACCESS_KEY."
            )
        self.s3 = boto3.client(
            "s3",
            endpoint_url=s3_endpoint,
            region_name=s3_region,
            aws_access_key_id=s3_access_key,
            aws_secret_access_key=s3_secret_key,
        )
        self.client = MongoClient(
            uri,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=5000,
            socketTimeoutMS=30000,
            waitQueueTimeoutMS=5000,
        )
        self.client.admin.command("ping")
        database = self.client[database_name]
        self.collection = database[COLLECTION_NAME]
        self.jobs = database[JOBS_COLLECTION_NAME]
        self.collection.create_index(
            [("job_id", ASCENDING), ("student_index", ASCENDING)],
            unique=True,
        )
        self.collection.create_index("student.uucms")
        self.jobs.create_index("job_id", unique=True)

    def create_job(self, job_id: str, filename: str) -> None:
        self.jobs.insert_one({
            "job_id": job_id,
            "filename": filename,
            "status": "processing",
            "total_pages": 0,
            "processed_pages": 0,
            "barcode_success": 0,
            "barcode_failed": 0,
            "error_message": "",
            "created_at": datetime.now(timezone.utc),
        })

    def update_job(self, job: Any) -> None:
        self.jobs.update_one(
            {"job_id": job.job_id},
            {"$set": {
                "status": job.status.value,
                "total_pages": job.total_pages,
                "processed_pages": job.processed_pages,
                "barcode_success": job.barcode_success,
                "barcode_failed": job.barcode_failed,
                "error_message": job.error_message,
            }},
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.find_one({"job_id": job_id}, {"_id": 0})

    def get_students(self, job_id: str) -> list[dict[str, Any]]:
        records = self.collection.find(
            {"job_id": job_id},
            {"_id": 0, "student": 1},
        ).sort("student_index", ASCENDING)
        return [record["student"] for record in records]

    def mark_processing_jobs_interrupted(self) -> None:
        self.jobs.update_many(
            {"status": "processing"},
            {"$set": {
                "status": "error",
                "error_message": "Processing was interrupted by a server restart.",
            }},
        )

    def create_upload_url(self, job_id: str, filename: str) -> dict[str, str]:
        safe_filename = os.path.basename(filename).replace(" ", "_")
        object_key = f"uploads/{job_id}/{safe_filename}"
        upload_url = self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.s3_bucket,
                "Key": object_key,
                "ContentType": "application/pdf",
            },
            ExpiresIn=900,
        )
        return {"object_key": object_key, "upload_url": upload_url}

    def download_source(self, object_key: str, destination: str) -> None:
        with open(destination, "wb") as output:
            self.s3.download_fileobj(self.s3_bucket, object_key, output)

    def save_result(self, job_id: str, result_pdf: bytes) -> None:
        object_key = f"results/{job_id}.pdf"
        self.s3.put_object(
            Bucket=self.s3_bucket,
            Key=object_key,
            Body=result_pdf,
            ContentType="application/pdf",
        )
        self.jobs.update_one(
            {"job_id": job_id},
            {"$set": {"result_object_key": object_key}},
        )

    def get_result_url(self, job_id: str) -> str | None:
        job = self.get_job(job_id)
        if not job or "result_object_key" not in job:
            return None
        return self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.s3_bucket,
                "Key": job["result_object_key"],
            },
            ExpiresIn=900,
        )

    def delete_result(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job and "result_object_key" in job:
            self.s3.delete_object(
                Bucket=self.s3_bucket,
                Key=job["result_object_key"],
            )
            self.jobs.update_one(
                {"job_id": job_id},
                {"$unset": {"result_object_key": ""}},
            )

    def save_students_batch(
        self,
        job_id: str,
        filename: str,
        students: list[tuple[int, dict[str, Any]]],
    ) -> None:
        if not students:
            return

        now = datetime.now(timezone.utc)
        operations = [
            UpdateOne(
                {"job_id": job_id, "student_index": student_index},
                {
                    "$set": {
                        "filename": filename,
                        "student": student,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "job_id": job_id,
                        "student_index": student_index,
                        "created_at": now,
                    },
                },
                upsert=True,
            )
            for student_index, student in students
        ]
        self.collection.bulk_write(operations, ordered=False)

    def find_student_by_barcode(self, barcode: str) -> dict[str, Any] | None:
        document = self.collection.find_one(
            {"student.uucms": barcode},
            {"_id": 0, "job_id": 1, "filename": 1, "student": 1},
        )
        return document

    def close(self) -> None:
        self.client.close()