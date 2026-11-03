from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from app.config import settings


class PrivateStorage(Protocol):
    def put(self, data: bytes) -> str: ...

    def read(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


def _validate_key(key: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise FileNotFoundError("Document not found")


class LocalPrivateStorage:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or settings.document_storage_path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        key = f"{uuid.uuid4().hex}"
        target = (self.root / key).resolve()
        if target.parent != self.root:
            raise ValueError("Invalid generated storage key")
        temp = target.with_suffix(".tmp")
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)
        return key

    def read(self, key: str) -> bytes:
        _validate_key(key)
        path = (self.root / key).resolve()
        if path.parent != self.root:
            raise FileNotFoundError("Document not found")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        _validate_key(key)
        path = (self.root / key).resolve()
        if path.parent != self.root:
            raise FileNotFoundError("Document not found")
        path.unlink(missing_ok=True)


class S3PrivateStorage:
    """Private object storage using opaque keys and server-side encryption."""

    def __init__(self, *, client=None, bucket: str | None = None):
        self.bucket = bucket or settings.s3_bucket
        if not self.bucket:
            raise RuntimeError("S3_BUCKET is required for S3 document storage")
        self.client = client or boto3.client(
            "s3",
            region_name=settings.aws_region or None,
            endpoint_url=settings.s3_endpoint_url or None,
        )

    def put(self, data: bytes) -> str:
        key = uuid.uuid4().hex
        args = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "ServerSideEncryption": settings.s3_server_side_encryption,
        }
        if settings.s3_server_side_encryption == "aws:kms":
            args["SSEKMSKeyId"] = settings.s3_kms_key_id
        self.client.put_object(**args)
        return key

    def read(self, key: str) -> bytes:
        _validate_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"NoSuchKey", "NotFound", "404"}:
                raise FileNotFoundError("Document not found") from None
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def delete(self, key: str) -> None:
        _validate_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_private_storage() -> PrivateStorage:
    if settings.storage_backend == "s3":
        return S3PrivateStorage()
    if settings.storage_backend == "local":
        return LocalPrivateStorage()
    raise RuntimeError("Unsupported private document storage backend")
