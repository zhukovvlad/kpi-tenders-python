import logging
from dataclasses import dataclass
from io import BytesIO

from minio import Minio
from minio.error import S3Error

from app.config import get_settings

log = logging.getLogger(__name__)


class MinIOError(Exception):
    pass


@dataclass(frozen=True)
class ObjectLocation:
    bucket: str
    object_name: str


def parse_storage_path(storage_path: str, default_bucket: str) -> ObjectLocation:
    """
    storage_path formats:
        "bucket/prefix/uuid.ext"  -> explicit bucket
        "prefix/uuid.ext"         -> use default bucket

    Note: if the leading path segment does not match default_bucket, the entire
    path (including the segment) becomes the object_name inside default_bucket.
    Unknown bucket prefixes are absorbed silently rather than raising an error,
    because storage_path values come from Go and are always trusted inputs.
    """
    normalized = storage_path.lstrip("/")
    if "/" not in normalized:
        raise MinIOError(f"invalid storage_path: {storage_path!r}")

    head, _, tail = normalized.partition("/")
    if head == default_bucket:
        return ObjectLocation(bucket=default_bucket, object_name=tail)
    return ObjectLocation(bucket=default_bucket, object_name=normalized)


class MinIOClient:
    def __init__(self) -> None:
        s = get_settings()
        self._client = Minio(
            endpoint=s.minio_endpoint,
            access_key=s.minio_access_key,
            secret_key=s.minio_secret_key,
            secure=s.minio_use_ssl,
        )
        self._default_bucket = s.minio_bucket

    @property
    def default_bucket(self) -> str:
        """The default MinIO bucket name (read-only)."""
        return self._default_bucket

    def download(self, storage_path: str) -> bytes:
        """Download an object from MinIO and return its raw bytes.

        Raises ``MinIOError`` if the object does not exist or the request fails.
        """
        loc = parse_storage_path(storage_path, self._default_bucket)
        try:
            response = self._client.get_object(loc.bucket, loc.object_name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as exc:
            raise MinIOError(
                f"minio get_object failed for {loc.bucket}/{loc.object_name}: {exc}"
            ) from exc

    def upload(
        self,
        object_name: str,
        data: bytes,
        content_type: str = "text/markdown; charset=utf-8",
    ) -> str:
        """Upload bytes to the default bucket.

        Returns the storage_path in the format ``'{bucket}/{object_name}'``
        so callers can store it as a reference back to MinIO.
        """
        stream = BytesIO(data)
        try:
            self._client.put_object(
                bucket_name=self._default_bucket,
                object_name=object_name,
                data=stream,
                length=len(data),
                content_type=content_type,
            )
        except S3Error as exc:
            raise MinIOError(
                f"minio put_object failed for {self._default_bucket}/{object_name}: {exc}"
            ) from exc
        return f"{self._default_bucket}/{object_name}"
