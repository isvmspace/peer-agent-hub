import threading

from test_hub_flow import HELLO, client, make_friends, register, token  # noqa: F401 - fixture
from test_social import connect, finish


def test_share_conversation_read_only_and_continue(client):
    register(client, "admin")
    alice, _ = register(client, "alice")
    bob, bob_user = register(client, "bob")
    carol, _ = register(client, "carol")
    with connect(client, token(client, alice, "connector")) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "查一下构建", "workspace": "D:/code/client"},
                           headers=alice).json()["task"]
        finish(ws, ws.receive_json()["task"]["id"], "构建正常")
        assert client.get(f"/api/tasks/{task['id']}", headers=bob).status_code == 403
        # only friends can receive a share
        assert client.post(f"/api/tasks/{task['id']}/shares", json={"user": "bob"}, headers=alice).status_code == 403
        make_friends(client, alice, bob, "bob")
        make_friends(client, alice, carol, "carol")
        client.post(f"/api/tasks/{task['id']}/shares", json={"user": "bob"}, headers=alice)
        view = client.get(f"/api/tasks/{task['id']}", headers=bob).json()["task"]
        assert view["can_continue"] is False and view["shared_by"] == "alice" and view["workspace"] == "client"
        assert client.post(f"/api/tasks/{task['id']}/follow-up", json={"message": "再看"}, headers=bob).status_code == 403
        assert [t["id"] for t in client.get("/api/tasks?box=shared", headers=bob).json()["tasks"]] == [task["id"]]
        # the shared thread also shows in bob's conversation list for that agent, carol's stays empty
        convo = lambda h: [t["id"] for t in client.get(f"/api/agents/{task['agent_id']}/conversations", headers=h).json()["tasks"]]
        assert convo(bob) == [task["id"]] and convo(carol) == []
        # a sharee cannot reshare
        assert client.post(f"/api/tasks/{task['id']}/shares", json={"user": "carol"}, headers=bob).status_code == 403

        # continue: bob has no workspace grant, the share alone lets him ask; the owner approves
        client.post(f"/api/tasks/{task['id']}/shares", json={"user": "bob", "can_continue": True}, headers=alice)
        r = client.post(f"/api/tasks/{task['id']}/follow-up", json={"message": "失败的用例是哪个"}, headers=bob).json()["task"]
        assert r["status"] == "pending_approval"
        client.post(f"/api/tasks/{task['id']}/approve", json={}, headers=alice)
        msg = ws.receive_json()["task"]
        assert msg["turn"] == 2 and msg["from"] == "bob" and msg["workspace"] == "D:/code/client"
        finish(ws, msg["id"], "没有失败")

        client.delete(f"/api/tasks/{task['id']}/shares/{bob_user['id']}", headers=alice)
        assert client.get(f"/api/tasks/{task['id']}", headers=bob).status_code == 403


def test_import_local_session_then_share(client):
    register(client, "admin")
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    make_friends(client, alice, bob, "bob")
    with connect(client, token(client, alice, "connector")) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        agent_id = next(a["id"] for a in client.get("/api/agents", headers=alice).json()["agents"])
        assert client.get(f"/api/agents/{agent_id}/local-sessions", headers=bob).status_code == 403

        def call(method, **kw):
            box = {}
            t = threading.Thread(target=lambda: box.update(r=getattr(client, method)(f"/api/agents/{agent_id}/local-sessions", headers=alice, **kw)))
            t.start()
            return t, box

        t, box = call("get")
        req = ws.receive_json()
        assert req["type"] == "list_sessions" and req["agent_key"] == "claude"
        ws.send_json({"type": "rpc_result", "request_id": req["request_id"],
                      "sessions": [{"id": "s1", "cwd": "D:/code/client", "title": "修登录", "updated_at": 1}]})
        t.join(5)
        assert box["r"].json()["sessions"][0]["task_id"] is None

        t, box = call("post", json={"session_id": "s1"})
        req = ws.receive_json()
        assert req["type"] == "read_session" and req["session_id"] == "s1"
        ws.send_json({"type": "rpc_result", "request_id": req["request_id"], "workspace": "D:/code/client",
                      "session": {"id": "s1", "title": "修登录", "turns": [{"prompt": "登录报错", "result": "改好了"},
                                                                          {"prompt": "加测试", "result": "加了"}]}})
        t.join(5)
        imported = box["r"].json()["task"]
        assert imported["status"] == "completed" and imported["turn"] == 2 and imported["source"] == "local"

        client.post(f"/api/tasks/{imported['id']}/shares", json={"user": "bob", "can_continue": True, "auto_approve": True}, headers=alice)
        events = client.get(f"/api/tasks/{imported['id']}", headers=bob).json()["events"]
        assert [e["content"] for e in events if e["kind"] == "prompt"] == ["登录报错", "加测试"]
        client.post(f"/api/tasks/{imported['id']}/follow-up", json={"message": "还有别的问题吗"}, headers=bob)
        msg = ws.receive_json()["task"]
        # the follow-up resumes the original local session in its folder
        assert msg["session_id"] == "s1" and msg["turn"] == 3 and msg["workspace"] == "D:/code/client"


def test_open_stream_stops_when_share_revoked(tmp_path, monkeypatch):
    """A share revoked while the sharee has the task stream open must cut that stream off."""
    import asyncio

    from agent_hub.app import Settings, create_app
    from agent_hub.db import Database
    from starlette.testclient import TestClient

    monkeypatch.setenv("AGENT_HUB_DATA_DIR", str(tmp_path))
    with TestClient(create_app(Settings(), Database(tmp_path / "hub.db"))) as client:
        def reg(name):
            r = client.post("/api/auth/register", json={"username": name, "password": "secret123"})
            return {"Authorization": "Bearer " + r.json()["token"]}, r.json()["user"]
        a, _ = reg("alice")
        b, b_user = reg("bob")
        f = client.post("/api/friends", json={"user": "bob"}, headers=a).json()["friendship"]
        client.post(f"/api/friends/{f['id']}/accept", headers=b)
        conn = client.post("/api/tokens", json={"kind": "connector", "name": "c"}, headers=a).json()["token"]
        hello = {"type": "hello", "connector": {"hostname": "h", "os": "darwin"},
                 "agents": [{"key": "claude", "name": "C", "kind": "claude", "isolation": "sandbox"}], "running": []}
        with client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn}) as ws:
            ws.send_json(hello)
            ws.receive_json()
        hub = client.app.state.hub
        task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x"}, headers=a).json()["task"]
        assert client.post(f"/api/tasks/{task['id']}/shares", json={"user": "bob"}, headers=a).status_code == 200

        events = []
        with client.stream("GET", f"/api/tasks/{task['id']}/stream", headers=b) as resp:
            lines = resp.iter_lines()
            for line in lines:
                if line.startswith("event:"):
                    events.append(line)
                    break
            hub.db.execute("DELETE FROM task_shares WHERE task_id = ?", (task["id"],))
            hub._publish_task(task["id"])
            for line in lines:
                if line.startswith("event:"):
                    events.append(line)
        assert events == ["event: task", "event: denied"]
