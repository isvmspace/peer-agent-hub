"""Connector release catalog: publish/withdraw builds, answer update checks, issue download tickets."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

from .db import Database
from .hub import HubError
from .storage import ArtifactStorage

LOGGER = logging.getLogger(__name__)

PLATFORMS = ("windows/amd64", "darwin/arm64", "darwin/amd64", "linux/amd64")
POLICIES = ("OPTIONAL", "FORCED")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TICKET_SECONDS = 600
MAX_ARTIFACT_BYTES = 200 * 1024 * 1024


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(str(value or "").strip())
    return tuple(int(x) for x in match.groups()) if match else None


def filename_for(platform: str) -> str:
    os_name, arch = platform.split("/")
    return f"agent-hub-connector-{os_name}-{arch}" + (".exe" if os_name == "windows" else "")


class Releases:
    def __init__(self, db: Database, storage: ArtifactStorage):
        self.db = db
        self.storage = storage

    def list(self) -> list[dict]:
        return self.db.all(
            "SELECT r.*, u.username AS publisher FROM connector_releases r LEFT JOIN users u ON u.id = r.created_by "
            "ORDER BY r.id DESC LIMIT 200"
        )

    def latest(self, platform: str) -> dict | None:
        rows = self.db.all(
            "SELECT * FROM connector_releases WHERE platform = ? AND status = 'published'", (platform,)
        )
        return max(rows, key=lambda r: parse_version(r["version"]), default=None)

    def publish(
        self,
        user_id: int | None,
        version: str,
        platform: str,
        source: Path,
        notes: str = "",
        policy: str = "OPTIONAL",
        min_supported: str = "",
    ) -> dict:
        if not parse_version(version):
            raise HubError("版本号必须是 x.y.z 格式")
        if platform not in PLATFORMS:
            raise HubError(f"平台必须是 {', '.join(PLATFORMS)} 之一")
        if policy not in POLICIES:
            raise HubError("更新策略必须是 OPTIONAL 或 FORCED")
        if min_supported and not parse_version(min_supported):
            raise HubError("最低支持版本必须是 x.y.z 格式")
        if self.db.one(
            "SELECT id FROM connector_releases WHERE version = ? AND platform = ? AND status = 'published'",
            (version, platform),
        ):
            raise HubError(f"{platform} 已发布过 {version}，请先撤回或使用新版本号")
        size = source.stat().st_size
        if size <= 0 or size > MAX_ARTIFACT_BYTES:
            raise HubError("安装包大小不合法")
        digest = hashlib.sha256()
        with open(source, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        key = f"{version}/{int(time.time())}-{filename_for(platform)}"
        self.storage.put(key, source, size)
        release_id = self.db.execute(
            "INSERT INTO connector_releases (version, platform, object_key, size, sha256, notes, policy, "
            "min_supported, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?)",
            (version, platform, key, size, digest.hexdigest(), notes[:5000], policy, min_supported, user_id, time.time()),
        )
        self.db.audit(user_id, "release.publish", release_id=release_id, version=version, platform=platform)
        return self.db.one("SELECT * FROM connector_releases WHERE id = ?", (release_id,))

    def withdraw(self, user_id: int, release_id: int) -> None:
        if not self.db.update(
            "UPDATE connector_releases SET status = 'withdrawn' WHERE id = ? AND status = 'published'", (release_id,)
        ):
            raise HubError("版本不存在或已撤回", 404)
        self.db.audit(user_id, "release.withdraw", release_id=release_id)

    def check(self, platform: str, current: str) -> dict:
        latest = self.latest(platform)
        current_v = parse_version(current)
        if not latest or not current_v or parse_version(latest["version"]) <= current_v:
            return {"update_available": False}
        policy = latest["policy"]
        if latest["min_supported"] and current_v < parse_version(latest["min_supported"]):
            policy = "FORCED"
        return {
            "update_available": True,
            "release_id": latest["id"],
            "platform": platform,
            "current_version": current,
            "latest_version": latest["version"],
            "file_size": latest["size"],
            "sha256": latest["sha256"],
            "notes": latest["notes"],
            "policy": policy,
            "min_supported": latest["min_supported"] or None,
            "published_at": latest["created_at"],
        }

    def ticket(self, release_id: int, base_url: str) -> dict:
        row = self.db.one("SELECT * FROM connector_releases WHERE id = ? AND status = 'published'", (release_id,))
        if not row:
            raise HubError("版本不存在或已撤回", 404)
        return {
            "download_url": self.storage.download_url(row["object_key"], filename_for(row["platform"]), TICKET_SECONDS, base_url),
            "expires_at": int(time.time()) + TICKET_SECONDS,
            "file_size": row["size"],
            "sha256": row["sha256"],
        }

    def bootstrap(self, directory: Path) -> None:
        """Import bundled binaries described by manifest.json once (used by the Docker image)."""
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            LOGGER.warning("invalid connector manifest %s", manifest_path)
            return
        version = manifest.get("version", "")
        for platform in PLATFORMS:
            source = directory / filename_for(platform)
            if not source.exists() or self.db.one(
                "SELECT id FROM connector_releases WHERE version = ? AND platform = ?", (version, platform)
            ):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / source.name
                shutil.copyfile(source, copy)
                try:
                    self.publish(None, version, platform, copy, manifest.get("notes", "随镜像内置发布"))
                    LOGGER.info("bootstrapped connector %s %s", version, platform)
                except HubError as exc:
                    LOGGER.warning("bootstrap %s %s skipped: %s", version, platform, exc)
