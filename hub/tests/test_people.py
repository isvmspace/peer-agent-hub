import base64

from test_hub_flow import HELLO, client, make_friends, register, token  # noqa: F401 - fixture
from test_social import connect, finish


def test_setup_admin_and_roles(client):
    assert client.get("/api/auth/options").json()["needs_setup"] is True
    admin, admin_user = register(client, "admin")
    bob, bob_user = register(client, "bob")
    assert admin_user["is_admin"] == 1 and bob_user["is_admin"] == 0
    assert client.get("/api/auth/options").json()["needs_setup"] is False

    assert client.get("/api/admin/users", headers=bob).status_code == 403
    users = client.get("/api/admin/users", headers=admin).json()["users"]
    assert [u["username"] for u in users] == ["admin", "bob"]
    assert client.put(f"/api/admin/users/{admin_user['id']}", json={"is_admin": False}, headers=admin).status_code == 400
    assert client.put(f"/api/admin/users/{bob_user['id']}", json={"disabled": True}, headers=admin).status_code == 200
    assert client.get("/api/auth/me", headers=bob).status_code == 401
    assert client.post("/api/auth/login", json={"username": "bob", "password": "secret123"}).status_code == 403


def test_friends_direct_messages_without_agents(client):
    register(client, "admin")
    bob, bob_user = register(client, "bob")
    carol, carol_user = register(client, "carol")
    # people without any agent can still befriend and chat
    assert client.post(f"/api/dm/{carol_user['id']}", json={"content": "hi"}, headers=bob).status_code == 403
    friendship = client.post("/api/friends", json={"user": "carol"}, headers=bob).json()["friendship"]
    assert friendship["status"] == "pending"
    assert client.get("/api/friends", headers=carol).json()["incoming"][0]["username"] == "bob"
    assert client.post(f"/api/friends/{friendship['id']}/accept", headers=bob).status_code == 403
    assert client.post(f"/api/friends/{friendship['id']}/accept", headers=carol).status_code == 200

    assert client.post(f"/api/dm/{carol_user['id']}", json={"content": "你好 carol"}, headers=bob).status_code == 200
    friends = client.get("/api/friends", headers=carol).json()["friends"]
    assert friends[0]["username"] == "bob" and friends[0]["unread"] == 1
    messages = client.get(f"/api/dm/{bob_user['id']}", headers=carol).json()["messages"]
    assert [m["content"] for m in messages] == ["你好 carol"]
    assert client.get("/api/friends", headers=carol).json()["friends"][0]["unread"] == 0

    client.delete(f"/api/friends/{friendship['id']}", headers=carol)
    assert client.post(f"/api/dm/{carol_user['id']}", json={"content": "x"}, headers=bob).status_code == 403


def test_group_with_people_and_mentions(client):
    register(client, "admin")
    maya, _ = register(client, "maya")
    alice, _ = register(client, "alice")
    bob, bob_user = register(client, "bob")
    with connect(client, token(client, alice, "connector")) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        make_friends(client, maya, alice, "alice")
        make_friends(client, maya, bob, "bob")
        contact = client.post("/api/contacts", json={"agent": "alice/claude"}, headers=maya).json()["contact"]
        client.post(f"/api/contacts/{contact['id']}/accept", json={"trust": True, "workspaces": ["D:/code/client"]}, headers=alice)

        # bob is not alice's friend, but group members only need to be the creator's friends
        group = client.post(
            "/api/groups", json={"name": "协作", "members": [{"agent": "alice/claude"}], "people": ["bob"]}, headers=maya
        ).json()["group"]
        assert client.get(f"/api/groups/{group['id']}", headers=bob).status_code == 200

        # plain chat between people does not wake agents
        r = client.post(f"/api/groups/{group['id']}/messages", json={"content": "@bob 看下这个"}, headers=maya).json()
        assert r["dispatched"] == []
        # bob @mentions the agent: tasks run as the sender, and bob has no access to alice's agent
        r = client.post(f"/api/groups/{group['id']}/messages", json={"content": "@alice/claude 打包"}, headers=bob).json()
        assert r["dispatched"] == ["alice/claude"] and len(r["errors"]) == 1
        r = client.post(f"/api/groups/{group['id']}/messages", json={"content": "@claude 打个包"}, headers=maya).json()
        assert r["dispatched"] == ["alice/claude"] and r["errors"] == []
        msg = ws.receive_json()
        assert "群成员：maya、bob" in msg["task"]["prompt"] and "@bob 看下这个" in msg["task"]["prompt"]

        # the agent returns a generated image, shown in the group timeline
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
        ws.send_json({"type": "task_attachment", "task_id": msg["task"]["id"], "name": "logo.png", "data": png})
        finish(ws, msg["task"]["id"], "画好了")
        view = client.get(f"/api/groups/{group['id']}", headers=bob).json()
        assert [p["username"] for p in view["people"]] == ["maya", "bob"]
        reply = view["timeline"][-1]["replies"][0]
        assert reply["attachments"][0]["name"] == "logo.png"
        file = client.get(f"/api/tasks/{msg['task']['id']}/files/{reply['attachments'][0]['id']}", headers=bob)
        assert file.status_code == 200 and file.content.startswith(b"\x89PNG")

        client.post(f"/api/groups/{group['id']}/leave", headers=bob)
        assert client.get(f"/api/groups/{group['id']}", headers=bob).status_code == 403


def test_agent_list_reports_friendship_separately_from_access(client):
    register(client, "admin")
    maya, _ = register(client, "maya")
    alice, _ = register(client, "alice")
    with connect(client, token(client, alice, "connector")) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        agent = lambda: next(a for a in client.get("/api/agents", headers=maya).json()["agents"] if a["address"] == "alice/claude")
        assert agent()["is_friend"] is False and agent()["access"] == "none"
        make_friends(client, maya, alice, "alice")
        assert agent()["is_friend"] is True and agent()["access"] == "none"


def test_group_unread_and_task_request(client):
    register(client, "admin")
    maya, _ = register(client, "maya")
    bob, _ = register(client, "bob")
    make_friends(client, maya, bob, "bob")
    group = client.post("/api/groups", json={"name": "闲聊", "people": ["bob"]}, headers=maya).json()["group"]
    client.post(f"/api/groups/{group['id']}/messages", json={"content": "在吗"}, headers=maya)
    unread = lambda h: client.get("/api/groups", headers=h).json()["groups"][0]["unread"]
    assert unread(maya) == 0 and unread(bob) == 1
    client.get(f"/api/groups/{group['id']}", headers=bob)
    assert unread(bob) == 0


def test_agent_replies_count_as_group_unread(client):
    register(client, "admin")
    maya, _ = register(client, "maya")
    alice, _ = register(client, "alice")
    with connect(client, token(client, alice, "connector")) as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        group = client.post("/api/groups", json={"name": "干活", "members": [{"agent": "alice/claude"}]}, headers=alice).json()["group"]
        client.post(f"/api/groups/{group['id']}/messages", json={"content": "打包"}, headers=alice)
        unread = lambda: client.get("/api/groups", headers=alice).json()["groups"][0]["unread"]
        assert unread() == 0
        finish(ws, ws.receive_json()["task"]["id"], "好了")
        assert unread() == 1
        client.get(f"/api/groups/{group['id']}", headers=alice)
        assert unread() == 0


def test_onboarding_doc_is_public_and_filled_in(client):
    r = client.get("/docs/onboarding.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "{{HUB}}" not in r.text and "http://testserver/mcp/" in r.text and "NO_PROXY" in r.text


def test_admin_sees_every_host_with_addresses(client):
    admin, _ = register(client, "admin")
    bob, _ = register(client, "bob")
    with connect(client, token(client, bob, "connector")) as ws:
        ws.send_json({**HELLO, "connector": {**HELLO["connector"], "local_ips": ["10.0.0.8", 42]}})
        ws.receive_json()
        assert client.get("/api/admin/hosts", headers=bob).status_code == 403
        host = client.get("/api/admin/hosts", headers=admin).json()["hosts"][0]
        assert host["username"] == "bob" and host["hostname"] == "win-pc" and host["online"] == 1
        assert host["remote_ip"] and host["local_ips"] == ["10.0.0.8"] and host["agents"][0]["key"] == "claude"


def done(ws, task_id, result="ok"):
    """Finish a task; return the tasks the hub sent meanwhile (hand-offs)."""
    ws.send_json({"type": "task_done", "task_id": task_id, "status": "completed", "result": result})
    ws.send_json({"type": "ping"})
    tasks = []
    while (msg := ws.receive_json())["type"] != "pong":
        if msg["type"] == "task":
            tasks.append(msg["task"])
    return tasks


def test_group_mentions_hand_off_in_order(client):
    register(client, "admin")
    maya, _ = register(client, "maya")
    two = {**HELLO, "agents": HELLO["agents"] + [{**HELLO["agents"][0], "key": "codex", "name": "Codex", "kind": "codex"},
                                                 {**HELLO["agents"][0], "key": "hermes", "name": "Hermes", "kind": "hermes"}]}
    with connect(client, token(client, maya, "connector")) as ws:
        ws.send_json(two)
        ws.receive_json()
        agents = {a["key"]: a for a in client.get("/api/agents", headers=maya).json()["agents"]}
        assert {"claude", "codex"} <= set(agents)
        group = client.post("/api/groups", json={"name": "接力", "members": [{"agent": "maya/claude"}, {"agent": "maya/codex"}]},
                            headers=maya).json()["group"]
        r = client.post(f"/api/groups/{group['id']}/messages",
                        json={"content": "@maya/claude 写提示词，让 @maya/codex 生成图片"}, headers=maya).json()
        assert r["dispatched"] == ["maya/claude"] and r["waiting"] == ["maya/codex"]
        first = ws.receive_json()["task"]
        assert "maya/codex" in first["prompt"] and "等你先完成" in first["prompt"]
        second = done(ws, first["id"], "@maya/codex 请按提示词生成：橘猫在吧台")[0]
        assert second["agent_key"] == "codex" and "橘猫在吧台" in second["prompt"] and second["depth"] == 1
        assert done(ws, second["id"], "图好了") == []
        # nothing more is mentioned: the chain ends
        tasks = client.get(f"/api/groups/{group['id']}", headers=maya).json()["timeline"][-1]["replies"]
        assert [t["agent_address"] for t in tasks] == ["maya/claude", "maya/codex"]

        # asked at the same time: both at once
        r = client.post(f"/api/groups/{group['id']}/messages",
                        json={"content": "@maya/claude @maya/codex 同时看看"}, headers=maya).json()
        assert sorted(r["dispatched"]) == ["maya/claude", "maya/codex"] and r["waiting"] == []
        a, b = ws.receive_json()["task"], ws.receive_json()["task"]
        assert done(ws, a["id"]) == [] and done(ws, b["id"]) == []

        # the first agent forgets to hand off: the waiting agent still gets the original message
        r = client.post(f"/api/groups/{group['id']}/messages",
                        json={"content": "@maya/claude 先查，再让 @maya/codex 改"}, headers=maya).json()
        first = ws.receive_json()["task"]
        late = done(ws, first["id"], "查完了")[0]
        assert late["agent_key"] == "codex" and "先查，再让" in late["prompt"] and late["depth"] == 1
        assert done(ws, late["id"]) == []

        # three in a row: strictly one after another, each getting the previous reply
        client.put(f"/api/groups/{group['id']}", json={"name": "接力", "members": [
            {"agent": "maya/claude"}, {"agent": "maya/codex"}, {"agent": "maya/hermes"}]}, headers=maya)
        r = client.post(f"/api/groups/{group['id']}/messages",
                        json={"content": "先 @maya/claude 写方案，再 @maya/codex 评审，最后 @maya/hermes 测试"}, headers=maya).json()
        assert r["waiting"] == ["maya/codex", "maya/hermes"]
        first = ws.receive_json()["task"]
        [second] = done(ws, first["id"], "方案A")
        assert second["agent_key"] == "codex" and "方案A" in second["prompt"]
        [third] = done(ws, second["id"], "评审通过")
        assert third["agent_key"] == "hermes" and "评审通过" in third["prompt"]
        assert done(ws, third["id"]) == []

        # a reply asking two agents at once still goes one at a time, and the second gets the first hand-off too
        client.post(f"/api/groups/{group['id']}/messages", json={"content": "@maya/claude 安排一下"}, headers=maya)
        first = ws.receive_json()["task"]
        [second] = done(ws, first["id"], "@maya/codex 写代码，@maya/hermes 写测试")
        assert second["agent_key"] == "codex"
        [third] = done(ws, second["id"], "代码好了")
        assert third["agent_key"] == "hermes" and "写测试" in third["prompt"] and "代码好了" in third["prompt"]
        assert third["depth"] == 1 and done(ws, third["id"]) == []

        # cancelling the chain stops it: the waiting agent is not started
        client.post(f"/api/groups/{group['id']}/messages",
                    json={"content": "@maya/claude 先查，再让 @maya/codex 改"}, headers=maya)
        first = ws.receive_json()["task"]
        client.post(f"/api/tasks/{first['id']}/cancel", json={"tree": True}, headers=maya)
        ws.send_json({"type": "ping"})
        while (msg := ws.receive_json())["type"] != "pong":
            assert msg["type"] != "task"
        replies = client.get(f"/api/groups/{group['id']}", headers=maya).json()["timeline"][-1]["replies"]
        assert [t["agent_address"] for t in replies] == ["maya/claude"]
