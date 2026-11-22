"""Direct-message auto-reply by one of the recipient's own agents."""

import time

from test_hub_flow import client, register  # noqa: F401 - pytest fixture
from test_phases import connect, finish, next_task


def befriend(client, a, b, b_name):
    friendship = client.post("/api/friends", json={"user": b_name}, headers=a).json()["friendship"]
    assert client.post(f"/api/friends/{friendship['id']}/accept", headers=b).status_code == 200


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    return fn()


def test_friend_message_is_answered_by_my_agent_in_one_conversation(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    befriend(client, bob, alice, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    agent_id = hub.db.one("SELECT id FROM agents WHERE key = 'claude'")["id"]
    bob_id = hub.db.one("SELECT id FROM users WHERE username = 'bob'")["id"]
    alice_id = hub.db.one("SELECT id FROM users WHERE username = 'alice'")["id"]
    try:
        # a full-permission agent may not take a friend's words as instructions
        hub.db.execute("UPDATE agents SET isolation = 'none' WHERE id = ?", (agent_id,))
        assert client.put("/api/auto-replies", json={"friend_id": 0, "agent_id": agent_id}, headers=alice).status_code == 403
        hub.db.execute("UPDATE agents SET isolation = 'sandbox' WHERE id = ?", (agent_id,))
        r = client.put("/api/auto-replies", json={"friend_id": 0, "agent_id": agent_id}, headers=alice)
        assert r.status_code == 200 and r.json()["default"]["agent_id"] == agent_id
        # someone else's agent cannot be used
        assert client.put("/api/auto-replies", json={"friend_id": 0, "agent_id": agent_id}, headers=bob).status_code == 403

        client.post(f"/api/dm/{alice_id}", json={"content": "你好，在吗"}, headers=bob)
        first = next_task(ws)
        assert "你好，在吗" in first["prompt"] and first["turn"] == 1
        # a second message while it is answering is picked up by the next turn, not a new task
        client.post(f"/api/dm/{alice_id}", json={"content": "顺便问下进度"}, headers=bob)
        finish(ws, first, result="在的，稍等")
        second = next_task(ws)
        assert second["id"] == first["id"] and second["turn"] == 2 and "顺便问下进度" in second["prompt"]

        msgs = lambda: client.get(f"/api/dm/{alice_id}", headers=bob).json()["messages"]  # noqa: E731
        replies = wait_for(lambda: [m for m in msgs() if m["auto"]])
        assert replies[0]["content"] == "在的，稍等" and replies[0]["auto_agent"] == "alice/claude"
        assert replies[0]["from_user_id"] == alice_id
        task = client.get(f"/api/tasks/{first['id']}", headers=alice).json()["task"]
        assert task["source"] == "dm"

        # per-friend override turns it off for bob
        finish(ws, second, result="进度正常")
        wait_for(lambda: len([m for m in msgs() if m["auto"]]) == 2)
        client.put("/api/auto-replies", json={"friend_id": bob_id, "agent_id": None}, headers=alice)
        client.post(f"/api/dm/{alice_id}", json={"content": "还在吗"}, headers=bob)
        time.sleep(0.3)
        assert hub.get_task(first["id"])["turn"] == 2
        # dropping the override falls back to the default again
        client.put("/api/auto-replies", json={"friend_id": bob_id, "inherit": True}, headers=alice)
        assert client.get("/api/auto-replies", headers=alice).json()["friends"] == []
    finally:
        ws.__exit__(None, None, None)


def test_auto_replies_never_trigger_each_other(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    befriend(client, bob, alice, "alice")
    hub = client.app.state.hub
    ws_a, _ = connect(client, alice)
    ws_b, _ = connect(client, bob)
    try:
        a_agent = hub.db.one("SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id WHERE u.username = 'alice' AND a.key = 'claude'")["id"]
        b_agent = hub.db.one("SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id WHERE u.username = 'bob' AND a.key = 'claude'")["id"]
        client.put("/api/auto-replies", json={"friend_id": 0, "agent_id": a_agent}, headers=alice)
        client.put("/api/auto-replies", json={"friend_id": 0, "agent_id": b_agent}, headers=bob)
        alice_id = hub.db.one("SELECT id FROM users WHERE username = 'alice'")["id"]
        client.post(f"/api/dm/{alice_id}", json={"content": "hi"}, headers=bob)
        finish(ws_a, next_task(ws_a), result="hello from alice's agent")
        wait_for(lambda: hub.db.one("SELECT 1 FROM direct_messages WHERE auto_task_id IS NOT NULL"))
        time.sleep(0.3)
        # bob's agent got nothing: an auto reply is not a person writing
        assert hub.db.one("SELECT COUNT(*) AS n FROM tasks WHERE agent_id = ?", (b_agent,))["n"] == 0
    finally:
        ws_a.__exit__(None, None, None)
        ws_b.__exit__(None, None, None)
