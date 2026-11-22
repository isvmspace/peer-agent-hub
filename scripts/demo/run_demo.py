#!/usr/bin/env python3
"""A self-contained demo: a hub, two connected machines with stand-in agents, a few people and tasks, and a fake
home whose agent logs show a collaboration for the 「本机协作」page. Nothing calls a model.

    ./scripts/build.sh                                  # console + connector binaries
    python3 scripts/demo/run_demo.py                    # then open http://127.0.0.1:8799/console/  (alice / demo1234)

Options: --port 8799, --dir /tmp/agent-hub-demo, --connector dist/downloads/agent-hub-connector-<os>-<arch>
Stop with Ctrl-C; everything lives in --dir.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PASSWORD = "demo1234"


def default_connector() -> Path:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}[platform.system()]
    arch = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine()]
    return ROOT / "dist" / "downloads" / f"agent-hub-connector-{system}-{arch}{'.exe' if system == 'windows' else ''}"


class Hub:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, body: dict | None = None, token: str = "") -> dict:
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method} {path}: {exc.code} {exc.read().decode()}") from None

    def register(self, user: str) -> str:
        self.call("POST", "/api/auth/register", {"username": user, "password": PASSWORD})
        token = self.call("POST", "/api/auth/login", {"username": user, "password": PASSWORD})["token"]
        self.call("PUT", "/api/auth/profile", {"display_name": user.capitalize()}, token)
        return token


def wait_for(fn, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return
        except Exception:  # noqa: BLE001 - still starting
            pass
        time.sleep(0.5)
    raise SystemExit("timed out waiting for the demo to come up")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--dir", type=Path, default=Path("/tmp/agent-hub-demo"))
    ap.add_argument("--connector", type=Path, default=default_connector())
    args = ap.parse_args()
    if not args.connector.exists():
        raise SystemExit(f"connector binary not found: {args.connector} (run ./scripts/build.sh first)")
    if args.dir.exists():
        shutil.rmtree(args.dir)
    demo = args.dir
    procs: list[subprocess.Popen] = []

    def stop(*_):
        for p in procs:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        run(args, demo, procs)
    finally:
        for p in procs:
            p.terminate()


def run(args, demo: Path, procs: list) -> None:

    # the fake home with agent logs (alice's machine reads it for 本机协作)
    subprocess.run([sys.executable, str(HERE / "demo_home.py"), str(demo / "alice-home")], check=True)
    for ws in ("work/payments", "work/desktop-client"):
        (demo / ws).mkdir(parents=True, exist_ok=True)

    base = f"http://127.0.0.1:{args.port}"
    env = {**os.environ, "AGENT_HUB_DATA_DIR": str(demo / "hub"), "AGENT_HUB_PUBLIC_URL": base, "PYTHONPATH": str(ROOT / "hub" / "src")}
    procs.append(subprocess.Popen([sys.executable, "-m", "agent_hub", "--host", "127.0.0.1", "--port", str(args.port)], env=env,
                                  stdout=open(demo / "hub.log", "w"), stderr=subprocess.STDOUT))
    hub = Hub(base)
    wait_for(lambda: hub.call("GET", "/healthz"))

    alice, bob, carol = hub.register("alice"), hub.register("bob"), hub.register("carol")  # alice registers first: admin

    def connect(owner_token: str, name: str, agents: list[dict], home: Path) -> None:
        token = hub.call("POST", "/api/tokens", {"kind": "connector", "name": name}, owner_token)["token"]
        cfg = demo / f"{name}.json"
        cfg.write_text(json.dumps({"hub_url": base, "token": token, "name": name, "agents": agents}))
        home.mkdir(parents=True, exist_ok=True)
        cenv = {**os.environ, "HOME": str(home), "PATH": f"{HERE / 'fake-agents'}{os.pathsep}{os.environ['PATH']}"}
        procs.append(subprocess.Popen([str(args.connector), "run", "-config", str(cfg)], env=cenv,
                                      stdout=open(demo / f"{name}.log", "w"), stderr=subprocess.STDOUT))

    connect(alice, "alice-macbook", [
        {"key": "claude", "name": "Claude Code", "kind": "claude", "description": "Refactors and multi-file changes",
         "workspaces": [{"name": "payments", "path": str(demo / "work/payments")}]},
        {"key": "codex", "name": "Codex", "kind": "codex", "description": "Reviews and tests in a sandbox",
         "workspaces": [{"name": "payments", "path": str(demo / "work/payments")}]}], demo / "alice-home")
    connect(bob, "bob-windows", [
        {"key": "claude", "name": "Claude Code", "kind": "claude", "description": "Builds the desktop client on Windows",
         "workspaces": [{"name": "desktop-client", "path": str(demo / "work/desktop-client")}]}], demo / "bob-home")
    wait_for(lambda: len(hub.call("GET", "/api/agents", token=alice)["agents"]) >= 3)
    # both connectors run on this computer: show made-up machines instead of its real hostname and addresses
    with sqlite3.connect(demo / "hub" / "agent-hub.db") as db:
        for name, host, os_, ip in (("alice-macbook", "alice-mbp.local", "darwin/arm64", "10.0.0.21"),
                                    ("bob-windows", "BOB-PC", "windows/amd64", "10.0.0.35")):
            db.execute("UPDATE connectors SET hostname = ?, os = ?, local_ips = ?, remote_ip = ? WHERE name = ?",
                       (host, os_, json.dumps([ip]), ip, name))

    # alice and carol may use bob's Claude (alice without approval), and are friends with him
    for asker in (alice, carol):
        fr = hub.call("POST", "/api/friends", {"user": "bob"}, asker)["friendship"]
        hub.call("POST", f"/api/friends/{fr['id']}/accept", {}, bob)
        contact = hub.call("POST", "/api/contacts", {"agent": "bob/claude"}, asker)["contact"]
        hub.call("POST", f"/api/contacts/{contact['id']}/accept",
                 {"trust": asker == alice, "workspaces": [str(demo / "work/desktop-client")]}, bob)

    # tasks: finished, running and waiting for approval
    hub.call("POST", "/api/tasks", {"agent": "alice/codex", "workspace": "payments",
                                    "prompt": "Review PR #42: does it fix the double charge on payment retries?"}, alice)
    hub.call("POST", "/api/tasks", {"agent": "bob/claude", "workspace": "desktop-client",
                                    "prompt": "Run the unit tests in desktop-client, fix what fails, then build the installer."}, alice)
    hub.call("POST", "/api/tasks", {"agent": "bob/claude", "workspace": "desktop-client",
                                    "prompt": "Update the release notes for 2.4.1 and run the unit tests again."}, carol)
    print(f"\nAgent Hub demo: {base}/console/   (alice / {PASSWORD}; also bob, carol)\nCtrl-C to stop.")
    sys.path.insert(0, str(HERE))
    from demo_home import keep_alive
    while True:  # the in-progress sessions keep working
        time.sleep(15)
        keep_alive(demo / "alice-home")


if __name__ == "__main__":
    main()
