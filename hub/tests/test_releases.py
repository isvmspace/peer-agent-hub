import hashlib
import shutil
import socket
import subprocess
import time

import httpx
import pytest
from starlette.testclient import TestClient

from agent_hub.app import Settings, create_app
from agent_hub.db import Database
from test_hub_flow import HELLO, register, token


def make_client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("AGENT_HUB_DATA_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(Settings(), Database(tmp_path / "hub.db")))


def publish(client, headers, version, platform="darwin/arm64", data=b"binary-v", **extra):
    params = {"version": version, "platform": platform, **extra}
    return client.post("/api/releases", params=params, content=data + version.encode(), headers=headers)


def exercise(client):
    admin, _ = register(client, "admin")
    alice, _ = register(client, "alice")
    assert publish(client, alice, "0.2.0").status_code == 403
    assert publish(client, admin, "bad").status_code == 400
    assert publish(client, admin, "0.2.0").status_code == 200
    assert publish(client, admin, "0.2.0").status_code == 400  # duplicate
    assert publish(client, admin, "0.1.5").status_code == 200

    conn = token(client, alice, "connector")
    hdr = {"Authorization": "Bearer " + conn}
    assert client.get("/api/connector/latest", params={"platform": "darwin/arm64", "current": "0.1.0"}).status_code == 401
    info = client.get("/api/connector/latest", params={"platform": "darwin/arm64", "current": "0.1.0"}, headers=hdr).json()
    assert info["update_available"] and info["latest_version"] == "0.2.0" and info["policy"] == "OPTIONAL"
    assert not client.get("/api/connector/latest", params={"platform": "darwin/arm64", "current": "0.2.0"}, headers=hdr).json()["update_available"]

    ticket = client.post("/api/connector/download-ticket", json={"release_id": info["release_id"]}, headers=hdr).json()
    url = ticket["download_url"]
    payload = client.get(url).content if url.startswith("http://testserver") else httpx.get(url).content
    assert hashlib.sha256(payload).hexdigest() == info["sha256"] == ticket["sha256"]
    assert payload == b"binary-v0.2.0"

    # web download redirects to the latest artifact
    r = client.get("/downloads/latest/darwin-arm64", follow_redirects=False)
    assert r.status_code == 302

    # connector on an old version is told on connect; min_supported forces the update
    assert publish(client, admin, "0.3.0", policy="OPTIONAL", min_supported="0.2.0").status_code == 200
    with client.websocket_connect("/ws/connector", headers=hdr) as ws:
        ws.send_json({**HELLO, "connector": {**HELLO["connector"], "os": "darwin/arm64", "version": "0.1.0"}})
        assert ws.receive_json()["type"] == "welcome"
        notice = ws.receive_json()
        assert notice["type"] == "update_available" and notice["latest_version"] == "0.3.0" and notice["policy"] == "FORCED"

    rid = info["release_id"]
    assert client.post(f"/api/releases/{rid}/withdraw", headers=admin).status_code == 200
    assert client.post("/api/connector/download-ticket", json={"release_id": rid}, headers=hdr).status_code == 404
    downloads = client.get("/api/downloads", headers=alice).json()["files"]
    assert [(f["platform"], f["version"]) for f in downloads] == [("darwin/arm64", "0.3.0")]


def test_releases_local_storage(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        exercise(client)
        bad = client.get("/artifacts", params={"key": "x", "exp": int(time.time()) - 1, "sig": "0"})
        assert bad.status_code == 403


def test_bootstrap_manifest(tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "agent-hub-connector-windows-amd64.exe").write_bytes(b"win")
    (dl / "manifest.json").write_text('{"version": "0.1.0"}')
    with make_client(tmp_path, monkeypatch, AGENT_HUB_DOWNLOAD_DIR=str(dl)) as client:
        admin, _ = register(client, "admin")
        rows = client.get("/api/releases", headers=admin).json()["releases"]
        assert [(r["platform"], r["version"]) for r in rows] == [("windows/amd64", "0.1.0")]


@pytest.mark.skipif(not shutil.which("minio"), reason="minio server binary not installed")
def test_releases_minio(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [shutil.which("minio"), "server", "--address", f"127.0.0.1:{port}", "--console-address", "127.0.0.1:0", str(tmp_path / "minio")],
        # minio needs a writable HOME for its client config; sandboxes often have none
        env={"MINIO_ROOT_USER": "hubtest", "MINIO_ROOT_PASSWORD": "hubtest-secret", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail(f"minio exited early ({proc.returncode}): {proc.stderr.read().decode(errors='replace')[-500:]}")
            try:
                if httpx.get(f"http://127.0.0.1:{port}/minio/health/live").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("minio did not become healthy within 10s")
        env = {
            "AGENT_HUB_S3_ENDPOINT": f"http://127.0.0.1:{port}",
            "AGENT_HUB_S3_ACCESS_KEY": "hubtest",
            "AGENT_HUB_S3_SECRET_KEY": "hubtest-secret",
            "AGENT_HUB_S3_BUCKET": "agent-hub-test",
        }
        with make_client(tmp_path / "hub", monkeypatch, **env) as client:
            exercise(client)
            assert client.get("/api/downloads", headers={"Authorization": "Bearer x"}).status_code == 401
    finally:
        proc.terminate()
        proc.wait(timeout=10)
