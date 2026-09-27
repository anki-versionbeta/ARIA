"""S3 object store.

Goes through `api.backend.da_platform.aws`, so the bucket, region, credential chain, TLS bundle
and expiry retry all come from the shared layer. A silo never names a bucket.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import BinaryIO

from botocore.exceptions import ClientError

from api.backend.da_platform import aws
from api.backend.da_platform.credentials import aws_credentials
from api.backend.da_platform.storage.base import PayloadTooLarge, StorageError


class S3ObjectStore:
    def __init__(self, bucket: str | None = None) -> None:
        credentials = aws_credentials()
        resolved = bucket or credentials.s3_bucket
        if not resolved:
            raise StorageError(
                "S3_BUCKET is not set; either set it or use STORAGE_BACKEND=local"
            )
        self._bucket = resolved
        # Optional extra base, normally empty: the silo prefix is already part of the
        # key so that BOP and ISO keep their existing locations in this bucket.
        self._base = credentials.s3_prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self._base}/{key.lstrip('/')}" if self._base else key.lstrip("/")

    def put(self, key: str, data: bytes) -> int:
        aws.get_clients().call(
            "s3", "put_object", Bucket=self._bucket, Key=self._key(key), Body=data
        )
        return len(data)

    def put_stream(
        self, key: str, chunks: Iterator[bytes], max_bytes: int | None = None
    ) -> int:
        """Buffered upload with an enforced ceiling.

        `MAX_UPLOAD_MB` is small enough that a single-part upload is fine; if the
        limit is ever raised past a few hundred MB this should become a multipart
        upload so memory stays flat.
        """
        buffer = io.BytesIO()
        written = 0
        for chunk in chunks:
            written += len(chunk)
            if max_bytes is not None and written > max_bytes:
                raise PayloadTooLarge(f"Upload exceeded {max_bytes} bytes")
            buffer.write(chunk)
        buffer.seek(0)
        aws.get_clients().call(
            "s3",
            "put_object",
            Bucket=self._bucket,
            Key=self._key(key),
            Body=buffer.getvalue(),
        )
        return written

    def open(self, key: str) -> BinaryIO:
        response = aws.get_clients().call(
            "s3", "get_object", Bucket=self._bucket, Key=self._key(key)
        )
        return io.BytesIO(response["Body"].read())

    def exists(self, key: str) -> bool:
        try:
            aws.get_clients().call(
                "s3", "head_object", Bucket=self._bucket, Key=self._key(key)
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def delete(self, key: str) -> None:
        aws.get_clients().call(
            "s3", "delete_object", Bucket=self._bucket, Key=self._key(key)
        )
