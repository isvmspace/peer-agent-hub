from test_hub_flow import HELLO, client, make_friends, register, token  # noqa: F401 - fixture


def connect(client, conn_token):
    return client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn_token})


def agent_for(client, headers, address="alice/claude"):
    return next(a for a in client.get("/api/agents", headers=headers).json()["agents"] if a["address"] == address)


def finish(ws, task_id, result="ok"):
    ws.send_json({"type": "task_done", "task_id": task_id, "status": "completed", "result": result})
    ws.send_json({"type": "ping"})
    assert ws.receive_json()["type"] == "pong"


def test_friends_workspace_grants_and_groups(client):
    register(client, "root")  # first account becomes the admin, who sees everything
    maya, _ = register(client, "maya")
    alice, _ = register(client, "alice")
    hello = {**HELLO, "agents": [{**HELLO["agents"][0], "workspaces": [
        {"name": "client", "path": "D:/code/client"}, {"name": "secret", "path": "D:/code/secret"}]}]}

    with connect(client, token(client, alice, "connector")) as ws:
        ws.send_json(hello)
        assert ws.receive_json()["type"] == "welcome"
        mine = agent_for(client, alice)
        assert [w["path"] for w in mine["workspaces"]] == ["D:/code/client", "D:/code/secret"]
        assert mine["isolation"] == "sandbox"
        client_ws = mine["workspaces"][0]["id"]
        # strangers see no folders and cannot dispatch
        assert agent_for(client, maya)["workspaces"] == [] and agent_for(client, maya)["workdir"] == ""
        assert client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x"}, headers=maya).status_code == 403
        assert client.put(f"/api/agents/{mine['id']}/workspaces", json={"workspaces": []}, headers=maya).status_code == 403

        # access needs a friendship between the people first
        assert client.post("/api/contacts", json={"agent": "alice/claude"}, headers=maya).status_code == 403
        make_friends(client, maya, alice, "alice")
        # access request -> owner accepts granting only the client workspace
        contact = client.post("/api/contacts", json={"agent": "alice/claude"}, headers=maya).json()["contact"]
        assert client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x"}, headers=maya).status_code == 403
        assert client.post(f"/api/contacts/{contact['id']}/accept", json={"workspaces": [client_ws]}, headers=maya).status_code == 403
        assert client.post(
            f"/api/contacts/{contact['id']}/accept", json={"trust": True, "workspaces": [client_ws]}, headers=alice
        ).status_code == 200
        followers = client.get("/api/contacts", headers=alice).json()["followers"]
        assert [(f["username"], f["granted"]) for f in followers] == [("maya", [client_ws])]

        seen = agent_for(client, maya)
        assert seen["workspaces"] == [{"id": client_ws, "name": "client"}]  # name only, no path
        secret_id = mine["workspaces"][1]["id"]
        denied = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x", "workspace": secret_id}, headers=maya)
        assert denied.status_code == 400
        by_path = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x", "workspace": "D:/code/client"}, headers=maya)
        assert by_path.status_code == 400  # friends address workspaces by id/name, never by path

        # single grant: workspace may be omitted; requester sees the name, owner and connector the path
        task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "打包"}, headers=maya).json()["task"]
        assert task["status"] == "dispatched" and task["workspace"] == "client"
        assert client.get(f"/api/tasks/{task['id']}", headers=alice).json()["task"]["workspace"] == "D:/code/client"
        msg = ws.receive_json()
        assert msg["task"]["workspace"] == "D:/code/client"
        finish(ws, task["id"])
        threads = client.get(f"/api/agents/{mine['id']}/conversations", headers=maya).json()["tasks"]
        assert [(t["id"], t["workspace"]) for t in threads] == [(task["id"], "client")]

        # group: members must be friends, workspace by id, context carries earlier replies
        group = client.post(
            "/api/groups", json={"name": "打包群", "members": [{"agent": "alice/claude", "workspace": client_ws}]}, headers=maya
        ).json()["group"]
        client.post(f"/api/groups/{group['id']}/messages", json={"content": "开始打包"}, headers=maya)
        msg = ws.receive_json()
        assert "打包群" in msg["task"]["prompt"] and msg["task"]["workspace"] == "D:/code/client"
        finish(ws, msg["task"]["id"], "已打包 v1.2")
        client.post(f"/api/groups/{group['id']}/messages", json={"content": "安装包路径？"}, headers=maya)
        assert "已打包 v1.2" in ws.receive_json()["task"]["prompt"]
        view = client.get(f"/api/groups/{group['id']}", headers=maya).json()
        assert view["members"][0]["group_workspace"] == "client"
        assert view["timeline"][0]["replies"][0]["workspace"] == "client"
        assert client.get(f"/api/groups/{group['id']}", headers=alice).json()["members"][0]["group_workspace"] == "D:/code/client"
        bob, _ = register(client, "bob")
        assert client.get(f"/api/groups/{group['id']}", headers=bob).status_code == 403

        # revoking the grant stops follow-ups in that workspace
        assert client.post(f"/api/contacts/{contact['id']}/workspaces", json={"workspaces": []}, headers=alice).status_code == 200
        r = client.post(f"/api/tasks/{task['id']}/follow-up", json={"message": "继续"}, headers=maya)
        assert r.status_code == 403

    # an agent running with full permissions only takes tasks from its owner
    with connect(client, token(client, alice, "connector")) as ws:
        ws.send_json({**hello, "agents": [{**hello["agents"][0], "isolation": "none"}]})
        ws.receive_json()
        client.post(f"/api/contacts/{contact['id']}/workspaces", json={"workspaces": [client_ws]}, headers=alice)
        r = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x"}, headers=maya)
        assert r.status_code == 403 and "完全权限" in r.json()["error"]
        assert client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "x"}, headers=alice).status_code == 200


def test_pubsub_wakes_subscribers(client):
    import asyncio

    hub = client.app.state.hub

    async def run():
        event = hub.subscribe("task:1")
        hub.publish("task:2")
        assert not event.is_set()
        hub.publish("task:1")
        assert event.is_set()
        hub.unsubscribe("task:1", event)
        assert "task:1" not in hub._subscribers

    asyncio.run(run())
