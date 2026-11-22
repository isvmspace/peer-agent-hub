import asyncio
import socket
import threading
import time

import httpx
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from agent_hub.app import Settings, create_app
from agent_hub.db import Database


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_DATA_DIR", str(tmp_path))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(Settings(), Database(tmp_path / "hub.db")), port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/healthz")
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    try:
        session = httpx.post(base + "/api/auth/register", json={"username": "maya", "password": "secret123"}).json()["token"]
        auth = {"Authorization": "Bearer " + session}
        personal = httpx.post(base + "/api/tokens", json={"kind": "personal"}, headers=auth).json()["token"]
        alice = httpx.post(base + "/api/auth/register", json={"username": "alice", "password": "secret123"}).json()["token"]
        alice_personal = httpx.post(
            base + "/api/tokens", json={"kind": "personal"}, headers={"Authorization": "Bearer " + alice}
        ).json()["token"]

        async def run():
            transport = StreamableHttpTransport(base + "/mcp/", headers={"Authorization": "Bearer " + personal})
            async with Client(transport) as client:
                names = {t.name for t in await client.list_tools()}
                assert {"list_agents", "create_task", "get_task", "follow_up", "cancel_task", "list_tasks"} <= names
                listed = (await client.call_tool("list_agents", {})).data
                assert listed == {"agents": []}
                missing = (await client.call_tool("create_task", {"to": "alice/claude", "prompt": "hi"})).data
                assert "不存在" in missing["error"]

            # people: befriend, chat and group without opening the console
            mine = StreamableHttpTransport(base + "/mcp/", headers={"Authorization": "Bearer " + personal})
            theirs = StreamableHttpTransport(base + "/mcp/", headers={"Authorization": "Bearer " + alice_personal})
            async with Client(mine) as me, Client(theirs) as her:
                assert (await me.call_tool("add_friend", {"username": "alice"})).data["status"] == "pending"
                inbox = (await her.call_tool("inbox", {})).data
                assert inbox["friend_requests"][0]["from"] == "maya"
                await her.call_tool("answer_friend_request", {"username": "maya", "accept": True})
                await me.call_tool("send_message", {"to": "alice", "content": "包好了吗"})
                inbox = (await her.call_tool("inbox", {})).data
                assert inbox["unread_messages"][0]["last"] == "包好了吗"

                async def reply_later():
                    await asyncio.sleep(1)
                    await her.call_tool("send_message", {"to": "maya", "content": "好了"})

                waited, _ = await asyncio.gather(
                    me.call_tool("send_message", {"to": "alice", "content": "等你回复", "wait_reply_seconds": 10}),
                    reply_later(),
                )
                assert [r["content"] for r in waited.data["replies"]] == ["好了"]

                created = httpx.post(base + "/api/groups", json={"name": "发版", "people": ["alice"]}, headers=auth).json()
                posted = (await me.call_tool("post_group", {"group": "发版", "content": "@alice 今晚发版"})).data
                assert posted["dispatched"] == []
                assert (await her.call_tool("list_groups", {})).data["groups"][0]["unread"] == 1
                timeline = (await her.call_tool("read_group", {"group": str(created["group"]["id"])})).data["timeline"]
                assert timeline[-1]["content"] == "@alice 今晚发版"
                assert (await her.call_tool("list_groups", {})).data["groups"][0]["unread"] == 0

        asyncio.run(run())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
