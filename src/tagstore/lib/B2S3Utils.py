"""Backblaze B2 access via the S3-compatible API.

Adapted from the Backblaze quick-start sample
https://github.com/backblaze-b2-samples/b2-python-s3-sample/ (``sample.py``).
That sample talks to B2 through ``boto3`` and B2's S3-compatible endpoint
rather than ``b2sdk``; the helpers here do the same, authenticating to and
syncing against a single private bucket.

Everything is loaded from a ``.env`` file (see the repo's ``.env``) - there are
no CLI arguments for configuration:

* ``BUCKET_ENDPOINT`` - B2 S3 endpoint, host or URL, e.g. ``s3.us-east-005.backblazeb2.com``
* ``KEY_ID``          - B2 application key id (the ``keyID`` from the console)
* ``APPLICATION_KEY`` - B2 application key secret
* ``BUCKET_NAME``     - name of the bucket to operate on
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import find_dotenv, load_dotenv

@dataclass(frozen=True)
class B2Credentials:
    """A B2 endpoint plus one application key pair."""

    endpoint: str
    key_id: str
    application_key: str

    @classmethod
    def from_env(cls) -> "B2Credentials":
        """Load ``BUCKET_ENDPOINT`` / ``KEY_ID`` / ``APPLICATION_KEY`` from ``.env``.

        The ``.env`` is located by walking up from the current working directory;
        values already set in the real environment win over the file. Raises
        ``RuntimeError`` listing any names that are missing. A scheme-less
        endpoint (``s3.us-east-005.backblazeb2.com``) is upgraded to ``https://``.
        """
        load_dotenv(find_dotenv(usecwd=True) or None)

        env_vars = {
            name: os.getenv(name)
            for name in ("BUCKET_ENDPOINT", "KEY_ID", "APPLICATION_KEY")
        }
        missing = [name for name, value in env_vars.items() if not value]
        if missing:
            raise RuntimeError(f"Missing B2 credentials in environment: {', '.join(missing)}")

        endpoint = env_vars["BUCKET_ENDPOINT"]
        if "://" not in endpoint:
            endpoint = "https://" + endpoint

        return cls(
            endpoint=endpoint,
            key_id=env_vars["KEY_ID"],
            application_key=env_vars["APPLICATION_KEY"],
        )


def bucket_from_env() -> str:
    """Return the ``BUCKET_NAME`` from the ``.env`` file / environment."""
    load_dotenv(find_dotenv(usecwd=True) or None)
    bucket = os.getenv("BUCKET_NAME")
    if not bucket:
        raise RuntimeError("Missing B2 bucket name in environment: BUCKET_NAME")
    return bucket


def get_b2_client(creds: B2Credentials) -> BaseClient:
    """Return a ``boto3`` S3 *client* bound to B2."""
    return boto3.client(
        service_name="s3",
        endpoint_url=creds.endpoint,
        aws_access_key_id=creds.key_id,
        aws_secret_access_key=creds.application_key,
        config=Config(signature_version="s3v4"),
    )


def get_b2_resource(creds: B2Credentials):
    """Return a ``boto3`` S3 *resource* bound to B2."""
    return boto3.resource(
        service_name="s3",
        endpoint_url=creds.endpoint,
        aws_access_key_id=creds.key_id,
        aws_secret_access_key=creds.application_key,
        config=Config(signature_version="s3v4"),
    )


def authenticate(bucket: str, creds: B2Credentials) -> BaseClient:
    """Authenticate to B2 and confirm ``bucket`` is reachable with ``creds``.

    Returns the authenticated client. Raises ``botocore`` exceptions
    (``ClientError``, ``EndpointConnectionError``, ...) if the credentials are
    rejected or the bucket cannot be reached.
    """
    client = get_b2_client(creds)
    client.head_bucket(Bucket=bucket)
    return client


def list_object_keys(bucket: str, client: BaseClient, prefix: str = "") -> list[str]:
    """Return every object key in ``bucket`` (optionally under ``prefix``)."""
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys


def get_presigned_url(
    bucket: str,
    key: str,
    client: BaseClient,
    expires_in: int = 3600,
) -> str:
    """Return a presigned GET URL for ``key`` - useful for private buckets."""
    return client.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def download_file(bucket: str, key: str, dest: Path, client: BaseClient) -> Path:
    """Download ``key`` from ``bucket`` to ``dest`` on the local filesystem."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(bucket, key, str(dest))
    return dest


# ---------------------------------------------------------------------------
# Remote object metadata + uploads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectMetadata:
    """Tidied metadata for one remote object."""

    key: str
    size: int
    last_modified: datetime  # server-side mtime, set by B2 on write
    etag: str  # S3 ETag; for a B2 single-part upload this is the MD5 hex digest
    content_type: str | None = None
    file_hash_sha1: str | None = None  # SHA-1 of the file body, when we recorded one
    utime: datetime | None = None  # when the user requested the upload (from user metadata)
    version_id: str | None = None  # this specific version's id, on a versioned bucket
    user_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def md5(self) -> str | None:
        """ETag as an MD5 hex digest, or ``None`` for a multipart ETag (``"<hex>-<n>"``)."""
        tag = self.etag.strip('"')
        return tag if len(tag) == 32 and "-" not in tag else None


def _parse_dt(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp we previously wrote to user metadata."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _metadata_from_head(key: str, resp: dict) -> ObjectMetadata:
    user_md = dict(resp.get("Metadata", {}))
    file_hash_sha1 = (
        resp.get("ChecksumSHA1")
        or user_md.get("file_hash_sha1")
        or user_md.get("sha1")
    )
    return ObjectMetadata(
        key=key,
        size=resp["ContentLength"],
        last_modified=resp["LastModified"],
        etag=resp["ETag"].strip('"'),
        content_type=resp.get("ContentType"),
        file_hash_sha1=file_hash_sha1,
        utime=_parse_dt(user_md.get("utime")),
        version_id=resp.get("VersionId"),
        user_metadata=user_md,
    )


def head_object(bucket: str, key: str, client: BaseClient) -> dict:
    """Return the raw ``head_object`` response for ``key`` (every S3 field).

    Raises ``ClientError`` with error code ``"404"`` if the object is absent.
    """
    return client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")


def object_exists(bucket: str, key: str, client: BaseClient) -> bool:
    """Return whether ``key`` exists in ``bucket``."""
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def object_metadata(bucket: str, key: str, client: BaseClient) -> ObjectMetadata:
    """HEAD ``key`` and return size, mtime, etag/md5, ``file_hash_sha1``, ``utime``."""
    return _metadata_from_head(key, head_object(bucket, key, client))


def list_object_metadata(
    bucket: str, client: BaseClient, prefix: str = ""
) -> list[ObjectMetadata]:
    """Like :func:`list_object_keys`, but carrying size / mtime / etag per key.

    Backed by ``list_objects_v2``, so ``file_hash_sha1``, ``utime`` and
    ``user_metadata`` are left empty here - call :func:`object_metadata` per key
    when those are needed.
    """
    out: list[ObjectMetadata] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out.append(
                ObjectMetadata(
                    key=obj["Key"],
                    size=obj["Size"],
                    last_modified=obj["LastModified"],
                    etag=obj["ETag"].strip('"'),
                )
            )
    return out


def upload_file(
    bucket: str,
    key: str,
    src: Path,
    client: BaseClient,
    *,
    content_type: str | None = None,
    file_hash_sha1: str | None = None,
    metadata: dict[str, str] | None = None,
    prune_old_versions: bool = False,
) -> ObjectMetadata:
    """Upload local file ``src`` to ``bucket`` as ``key``; return the stored metadata.

    Uses boto3's managed transfer, which switches to multipart automatically for
    large files. Everything below is written as S3 user metadata
    (``x-amz-meta-*``) so a later sync can compare without re-reading the body:

    * ``file_hash_sha1`` - SHA-1 of the file body, if the caller computed one
    * ``utime``          - set here to the moment this upload was requested
    * ``metadata``       - any additional key/value pairs

    With ``prune_old_versions=True``, the upload lands first (so ``key`` is
    never briefly missing), then every *other* version of ``key`` - including
    any delete markers - is permanently removed, leaving only the version just
    uploaded.
    """
    stored = {k: str(v) for k, v in (metadata or {}).items()}
    stored["utime"] = datetime.now(timezone.utc).isoformat()
    if file_hash_sha1:
        stored["file_hash_sha1"] = file_hash_sha1

    extra: dict = {"Metadata": stored}
    if content_type:
        extra["ContentType"] = content_type
    client.upload_file(str(src), bucket, key, ExtraArgs=extra)
    result = object_metadata(bucket, key, client)

    if prune_old_versions:
        stale = [
            v
            for v in list_object_versions(bucket, client, prefix=key)
            if v.key == key and v.version_id != result.version_id
        ]
        _batch_delete_versions(bucket, client, stale)

    return result


def delete_object(bucket: str, key: str, client: BaseClient) -> None:
    """Delete ``key`` from ``bucket`` (no error if it is already gone).

    On a versioned bucket this removes only the *current* version - B2, like
    S3, records a delete marker rather than erasing history. Use
    :func:`delete_all_versions` to erase every version and marker for ``key``.
    """
    client.delete_object(Bucket=bucket, Key=key)


# ---------------------------------------------------------------------------
# Object versions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectVersion:
    """One historical version - or delete marker - of an object key."""

    key: str
    version_id: str
    is_latest: bool
    last_modified: datetime
    size: int | None = None  # None for a delete marker
    etag: str | None = None  # None for a delete marker
    is_delete_marker: bool = False

    @property
    def md5(self) -> str | None:
        """ETag as an MD5 hex digest, or ``None`` (delete marker / multipart ETag)."""
        if not self.etag:
            return None
        tag = self.etag.strip('"')
        return tag if len(tag) == 32 and "-" not in tag else None


def list_object_versions(
    bucket: str, client: BaseClient, prefix: str = ""
) -> list[ObjectVersion]:
    """Return every stored version and delete marker under ``prefix``.

    B2 keeps prior versions by default (there is no opt-in "enable versioning"
    step like plain S3). Results are sorted by key, newest version first.
    Note ``prefix`` matches by leading substring, same as ``list_object_keys``
    - filter on ``v.key == key`` yourself if you only want one exact key.
    """
    out: list[ObjectVersion] = []
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for v in page.get("Versions", []):
            out.append(
                ObjectVersion(
                    key=v["Key"],
                    version_id=v["VersionId"],
                    is_latest=v.get("IsLatest", False),
                    last_modified=v["LastModified"],
                    size=v.get("Size"),
                    etag=(v.get("ETag") or "").strip('"') or None,
                )
            )
        for m in page.get("DeleteMarkers", []):
            out.append(
                ObjectVersion(
                    key=m["Key"],
                    version_id=m["VersionId"],
                    is_latest=m.get("IsLatest", False),
                    last_modified=m["LastModified"],
                    is_delete_marker=True,
                )
            )
    out.sort(key=lambda v: (v.key, -v.last_modified.timestamp()))
    return out


def _batch_delete_versions(
    bucket: str, client: BaseClient, versions: list[ObjectVersion]
) -> int:
    """Permanently delete each of ``versions`` (a specific key + version id each)."""
    to_delete = [{"Key": v.key, "VersionId": v.version_id} for v in versions]
    # Consider eradicating the cap
    for i in range(0, len(to_delete), 1000):  # delete_objects caps out at 1000/call
        chunk = to_delete[i : i + 1000]
        resp = client.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
        errors = resp.get("Errors")
        if errors:
            raise RuntimeError(f"Failed to delete some object versions: {errors}")
    return len(to_delete)


def delete_object_version(bucket: str, key: str, version_id: str, client: BaseClient) -> None:
    """Permanently delete one specific version (or delete marker) of ``key``."""
    client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)


def delete_all_versions(bucket: str, key: str, client: BaseClient) -> int:
    """Permanently delete every version and delete marker of ``key``.

    Returns the number removed. Unlike a plain :func:`delete_object`, nothing
    of ``key`` is recoverable afterwards.
    """
    versions = [v for v in list_object_versions(bucket, client, prefix=key) if v.key == key]
    return _batch_delete_versions(bucket, client, versions)
