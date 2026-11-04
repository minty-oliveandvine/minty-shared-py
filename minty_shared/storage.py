"""Files in Backblaze B2 through the S3 API, stored exactly as uploaded (owner, 2026-11-04).

One upload for every backend that stores an attachment (minty-petty-cash-api's receipts,
minty-payment-request-api's invoices and bank slips): no downsizing and no conversion, so the
bytes in B2 are the bytes the person chose, under the content type they arrived with.

* ``make_client`` - the boto3 S3 client, built the same way everywhere (s3v4 signatures).
* ``put_file`` - one object, retried twice (1 s, 2 s); a final failure raises ``StorageError``.

The client is passed in, never read from settings, so this module needs no framework and each
service keeps its own client factory (and the test seam that patches it). Extra ``storage``.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Any

import boto3
from botocore.config import Config

logger = logging.getLogger("minty-shared.storage")

DEFAULT_RETRIES = 2


class StorageError(Exception):
    """The file could not be stored. The cause is chained and already logged; the caller
    answers with its own sentence."""


def make_client(*, key: str, secret: str, region: str, endpoint_url: str | None) -> Any:
    return boto3.client(
        "s3",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name=region,
        endpoint_url=endpoint_url or None,
        config=Config(signature_version="s3v4"),
    )


def put_file(
    client: Any,
    bucket: str,
    key: str,
    data: bytes,
    *,
    content_type: str,
    retries: int = DEFAULT_RETRIES,
) -> int:
    """Store ``data`` unchanged at ``bucket``/``key`` and return the stored size."""
    if not bucket:
        raise StorageError("No bucket is configured: the file cannot be stored.")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            client.upload_fileobj(
                io.BytesIO(data), bucket, key, ExtraArgs={"ContentType": content_type}
            )
            return len(data)
        except Exception as exc:  # noqa: BLE001 - any transport failure is retried, then raised
            last_error = exc
            if attempt < retries:
                logger.warning("Retry %s/%s for %s: %s", attempt + 1, retries, key, exc)
                time.sleep(2**attempt)
    logger.error("Upload of %s failed after %s attempts: %s", key, retries + 1, last_error)
    raise StorageError(f"Upload of {key} failed") from last_error
