"""Connector release artifacts: MinIO (S3-compatible) when configured, otherwise a local directory."""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode


class ArtifactStorage:
    kind = "base"

    def put(self, key: str, path: Path, size: int) -> None:
        raise NotImplementedError

    def download_url(self, key: str, filename: str, expires: int, base_url: str) -> str:
        raise NotImplementedError


class LocalStorage(ArtifactStorage):
    """Files under a directory, served by the hub through short-lived HMAC-signed URLs."""

    kind = "local"

    def __init__(self, root: Path, secret: str):
        self.root = root
        self.secret = secret.encode()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("invalid object key")
        return target

    def put(self, key: str, path: Path, size: int) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # copy then rename: the source is usually in /tmp, often a different filesystem
        partial = target.with_name(target.name + ".part")
        shutil.copyfile(path, partial)
        os.replace(partial, target)

    def sign(self, key: str, expires_at: int) -> str:
        return hmac.new(self.secret, f"{key}\n{expires_at}".encode(), hashlib.sha256).hexdigest()

    def verify(self, key: str, expires_at: int, signature: str) -> bool:
        return expires_at >= time.time() and hmac.compare_digest(self.sign(key, expires_at), signature)

    def download_url(self, key: str, filename: str, expires: int, base_url: str) -> str:
        expires_at = int(time.time()) + expires
        query = urlencode({"key": key, "exp": expires_at, "sig": self.sign(key, expires_at), "name": filename})
        return f"{base_url.rstrip('/')}/artifacts?{query}"


class MinioStorage(ArtifactStorage):
    kind = "minio"

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, prefix: str, secure: bool):
        from minio import Minio

        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, path: Path, size: int) -> None:
        with open(path, "rb") as fh:
            self.client.put_object(self.bucket, self._key(key), fh, size, content_type="application/octet-stream")
        path.unlink(missing_ok=True)

    def download_url(self, key: str, filename: str, expires: int, base_url: str) -> str:
        return self.client.presigned_get_object(
            self.bucket,
            self._key(key),
            expires=timedelta(seconds=expires),
            response_headers={"response-content-disposition": f'attachment; filename="{filename}"'},
        )


def build_storage(data_dir: Path, secret: str) -> ArtifactStorage:
    endpoint = os.getenv("AGENT_HUB_S3_ENDPOINT", "").strip()
    if endpoint:
        return MinioStorage(
            endpoint=endpoint.removeprefix("http://").removeprefix("https://"),
            access_key=os.environ["AGENT_HUB_S3_ACCESS_KEY"],
            secret_key=os.environ["AGENT_HUB_S3_SECRET_KEY"],
            bucket=os.getenv("AGENT_HUB_S3_BUCKET", "agent-hub"),
            prefix=os.getenv("AGENT_HUB_S3_PREFIX", "connector"),
            secure=endpoint.startswith("https://") or os.getenv("AGENT_HUB_S3_SECURE", "").lower() == "true",
        )
    return LocalStorage(data_dir / "releases", secret)
