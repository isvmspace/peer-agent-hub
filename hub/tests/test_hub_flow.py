import pytest
from starlette.testclient import TestClient

from agent_hub.app import Settings, create_app
from agent_hub.db import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_DATA_DIR", str(tmp_path))
    app = create_app(Settings(), Database(tmp_path / "hub.db"))
    with TestClient(app) as c:
        yield c


def register(client, username):
    r = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}, r.json()["user"]


def token(client, headers, kind):
    r = client.post("/api/tokens", json={"kind": kind, "name": kind}, headers=headers)
    return r.json()["token"]


HELLO = {
    "type": "hello",
    "connector": {"hostname": "win-pc", "os": "windows", "version": "test"},
    "agents": [{"key": "claude", "name": "Claude Code", "kind": "claude", "tags": ["windows"], "isolation": "sandbox",
                "workspaces": [{"name": "client", "path": "D:/code/client"}]}],
    "running": [],
}


def make_friends(client, requester, other, other_username):
    friendship = client.post("/api/friends", json={"user": other_username}, headers=requester).json()["friendship"]
    if friendship["status"] == "pending":
        assert client.post(f"/api/friends/{friendship['id']}/accept", headers=other).status_code == 200
    return friendship["id"]


def befriend(client, requester, owner, agent="alice/claude", workspaces=("D:/code/client",)):
    make_friends(client, requester, owner, agent.split("/")[0])
    contact = client.post("/api/contacts", json={"agent": agent}, headers=requester).json()["contact"]
    r = client.post(f"/api/contacts/{contact['id']}/accept", json={"workspaces": list(workspaces)}, headers=owner)
    assert r.status_code == 200, r.text
    return contact["id"]


def test_full_flow(client):
    maya, maya_user = register(client, "maya")
    alice, alice_user = register(client, "alice")
    assert maya_user["is_admin"] == 1 and alice_user["is_admin"] == 0

    alice_conn = token(client, alice, "connector")
    with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + alice_conn}) as ws:
        ws.send_json(HELLO)
        assert ws.receive_json()["type"] == "welcome"

        agents = client.get("/api/agents", headers=maya).json()["agents"]
        assert [(a["address"], a["online"]) for a in agents] == [("alice/claude", True)]
        befriend(client, maya, alice)

        # maya -> alice needs approval
        task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "打包桌面客户端"}, headers=maya).json()["task"]
        assert task["status"] == "pending_approval"
        assert client.post(f"/api/tasks/{task['id']}/approve", headers=maya).status_code == 403
        assert client.post(f"/api/tasks/{task['id']}/approve", headers=alice).json()["task"]["status"] == "dispatched"

        msg = ws.receive_json()
        assert msg["type"] == "task" and msg["task"]["prompt"].endswith("打包桌面客户端") and msg["task"]["from"] == "maya"
        tid = msg["task"]["id"]
        ws.send_json({"type": "task_started", "task_id": tid})
        ws.send_json({"type": "task_event", "task_id": tid, "kind": "output", "content": "building..."})
        ws.send_json({"type": "task_done", "task_id": tid, "status": "completed", "result": "安装包已生成", "session_id": "s-1"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"

        detail = client.get(f"/api/tasks/{tid}", headers=maya).json()
        assert detail["task"]["status"] == "completed" and detail["task"]["result"] == "安装包已生成"
        assert "building..." in [e["content"] for e in detail["events"]]

        # follow-up reuses the session and carries history
        # follow-up of a manually approved task needs approval again
        fu = client.post(f"/api/tasks/{tid}/follow-up", json={"message": "把安装包路径发我"}, headers=maya).json()["task"]
        assert fu["status"] == "pending_approval"
        assert client.post(f"/api/tasks/{tid}/approve", headers=alice).status_code == 200
        msg = ws.receive_json()["task"]
        assert msg["turn"] == 2 and msg["session_id"] == "s-1"
        assert msg["history"] == [{"prompt": "打包桌面客户端", "result": "安装包已生成"}]
        # a late report tagged with turn 1 must not finish turn 2
        ws.send_json({"type": "task_done", "task_id": tid, "turn": 1, "status": "completed", "result": "stale"})
        ws.send_json({"type": "task_done", "task_id": tid, "turn": 2, "status": "failed", "error": "boom"})
        assert client.get(f"/api/tasks/{tid}", headers=alice).json()["task"]["status"] == "failed"

        # alice exempts maya from approval
        users = {u["username"]: u["id"] for u in client.get("/api/users", headers=alice).json()["users"]}
        assert client.post("/api/rules", json={"from_user_id": users["maya"]}, headers=alice).status_code == 200
        task2 = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "再来一次"}, headers=maya).json()["task"]
        assert task2["status"] == "dispatched" and task2["auto_approved"]
        assert ws.receive_json()["task"]["id"] == task2["id"]

        # cancel a running task notifies the connector
        client.post(f"/api/tasks/{task2['id']}/cancel", headers=maya)
        assert ws.receive_json() == {"type": "cancel", "task_id": task2["id"]}

    # disconnect marks offline; queued tasks wait for reconnect
    assert client.get("/api/agents", headers=maya).json()["agents"][0]["online"] is False
    task3 = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "离线排队"}, headers=maya).json()["task"]
    assert task3["status"] == "queued"
    with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + alice_conn}) as ws:
        ws.send_json(HELLO)
        assert ws.receive_json()["type"] == "welcome"
        assert ws.receive_json()["task"]["id"] == task3["id"]


def test_depth_limit_and_visibility(client):
    maya, _ = register(client, "maya")
    bob, _ = register(client, "bob")
    conn = token(client, maya, "connector")
    with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn}) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
    # connector offline: tasks stay queued, so no cross-loop socket writes happen here
    import asyncio

    app_hub = client.app.state.hub
    user = app_hub.get_user(1)
    parent = None
    for _ in range(6):
        parent = asyncio.run(app_hub.create_task(user, "maya/claude", "x", parent_task_id=parent))["id"]
    with pytest.raises(Exception, match="层级"):
        asyncio.run(app_hub.create_task(user, "maya/claude", "x", parent_task_id=parent))
    assert client.get(f"/api/tasks/{parent}", headers=bob).status_code == 403


def test_rejects_bad_tokens(client):
    maya, _ = register(client, "maya")
    personal = token(client, maya, "personal")
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + personal}) as ws:
            ws.receive_json()
    assert client.post("/mcp/", json={}).status_code == 401
    assert client.get("/api/tasks").status_code == 401


def test_admin_cannot_drive_other_users_tasks(client):
    admin, _ = register(client, "admin")
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    conn = token(client, alice, "connector")
    with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn}) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        t = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x"}, headers=alice).json()["task"]
        tid = ws.receive_json()["task"]["id"]
        ws.send_json({"type": "task_done", "task_id": tid, "status": "completed", "result": "ok"})
        client.get(f"/api/tasks/{tid}", headers=alice)
        assert client.get(f"/api/tasks/{t['id']}", headers=admin).status_code == 200
        assert client.post(f"/api/tasks/{tid}/follow-up", json={"message": "rm -rf ~"}, headers=admin).status_code == 403
        assert client.post(f"/api/tasks/{tid}/cancel", headers=bob).status_code == 403
        assert client.post("/api/rules", json={"from_user_id": "abc"}, headers=alice).status_code == 400


def test_result_replayed_after_reconnect(client):
    alice, _ = register(client, "alice")
    conn = token(client, alice, "connector")
    hdr = {"Authorization": "Bearer " + conn}
    with client.websocket_connect("/ws/connector", headers=hdr) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x"}, headers=alice)
        tid = ws.receive_json()["task"]["id"]
        ws.send_json({"type": "task_started", "task_id": tid, "turn": 1})
        ws.send_json({"type": "ping"})
        ws.receive_json()
    with client.websocket_connect("/ws/connector", headers=hdr) as ws:
        ws.send_json({**HELLO, "finished": [tid]})
        ws.receive_json()
        ws.send_json({"type": "task_done", "task_id": tid, "turn": 1, "status": "completed", "result": "离线完成"})
        ws.send_json({"type": "ping"})
        ws.receive_json()
    task = client.get(f"/api/tasks/{tid}", headers=alice).json()["task"]
    assert task["status"] == "completed" and task["result"] == "离线完成"


def test_task_token_lifecycle_and_no_borrowed_access(client):
    import asyncio

    from agent_hub.auth import resolve_api_token

    maya, _ = register(client, "maya")
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    hub = client.app.state.hub
    alice_conn = token(client, alice, "connector")
    with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + alice_conn}) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "自己的任务"}, headers=alice).json()["task"]
        payload = ws.receive_json()["task"]
        tok = payload["mcp_token"]
        assert tok.startswith("aht_") and resolve_api_token(hub.db, tok, "task")["username"] == "alice"
        assert "aht_" not in str(client.get("/api/tokens", headers=alice).json())
        ws.send_json({"type": "task_done", "task_id": task["id"], "status": "completed", "result": "ok"})
        ws.send_json({"type": "ping"})
        ws.receive_json()
        assert resolve_api_token(hub.db, tok, "task") is None

    # bob asks alice/claude for work; it must not use alice's access to maya/claude on bob's behalf
    bob_conn = token(client, maya, "connector")
    with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + bob_conn}) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
    befriend(client, alice, maya, "maya/claude")
    befriend(client, bob, alice, "alice/claude")
    parent = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "bob 的任务"}, headers=bob).json()["task"]
    alice_user = hub.get_user(2)
    with pytest.raises(Exception, match="借你的权限"):
        asyncio.run(hub.create_task(alice_user, "maya/claude", "转派", parent_task_id=parent["id"], workspace="client"))
    # sub-tasks to the requester's own agents or the owner's own agents stay allowed
    own = asyncio.run(hub.create_task(alice_user, "alice/claude", "自己干", parent_task_id=parent["id"]))
    assert own["parent_task_id"] == parent["id"] and own["depth"] == 1


def test_profile_password_and_hidden_agents(client):
    maya, _ = register(client, "maya")
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")

    r = client.put("/api/auth/profile", json={"display_name": "  王小明  "}, headers=maya)
    assert r.json()["user"]["display_name"] == "王小明"
    assert client.put("/api/auth/profile", json={"display_name": " "}, headers=maya).status_code == 400
    assert client.post("/api/auth/password", json={"old_password": "wrong", "new_password": "newpass1"}, headers=maya).status_code == 400
    assert client.post("/api/auth/password", json={"old_password": "secret123", "new_password": "secret123"}, headers=maya).status_code == 400
    assert client.post("/api/auth/password", json={"old_password": "secret123", "new_password": "newpass1"}, headers=maya).status_code == 200
    assert client.post("/api/auth/login", json={"username": "maya", "password": "newpass1"}).status_code == 200

    conn = token(client, alice, "connector")
    with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn}) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
    befriend(client, bob, alice)  # bob already has access
    make_friends(client, maya, alice, "alice")
    agent_id = client.get("/api/agents", headers=alice).json()["agents"][0]["id"]
    assert client.put(f"/api/agents/{agent_id}/visibility", json={"hidden": True}, headers=bob).status_code == 403
    assert client.put(f"/api/agents/{agent_id}/visibility", json={"hidden": True}, headers=alice).status_code == 200

    addresses = lambda h: [a["address"] for a in client.get("/api/agents", headers=h).json()["agents"]]
    assert addresses(alice) == ["alice/claude"]          # owner
    assert addresses(bob) == ["alice/claude"]            # existing grant keeps working
    assert addresses(maya) == ["alice/claude"]         # admin sees everything
    carol, _ = register(client, "carol")
    make_friends(client, carol, alice, "alice")
    assert addresses(carol) == []
    r = client.post("/api/contacts", json={"agent": "alice/claude"}, headers=carol)
    assert r.status_code == 404
    client.put(f"/api/agents/{agent_id}/visibility", json={"hidden": False}, headers=alice)
    assert addresses(carol) == ["alice/claude"]
