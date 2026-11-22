"""A full-permission agent: only its owner, plus people the owner explicitly allowed, may use it."""

from test_hub_flow import befriend, client, register  # noqa: F401 - pytest fixture
from test_phases import connect, finish, next_task


def test_owner_can_allow_one_person_to_use_a_full_permission_agent(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    try:
        contact_id = befriend(client, bob, alice, workspaces=("/code/repo",))
        hub.db.execute("UPDATE agents SET isolation = 'none' WHERE key = 'claude'")
        ask = {"agent": "alice/claude", "prompt": "run it", "workspace": "repo"}
        assert client.post("/api/tasks", json=ask, headers=bob).status_code == 403
        # only the owner decides, and only for an accepted contact
        assert client.post(f"/api/contacts/{contact_id}/full", json={"allow": True}, headers=bob).status_code == 403
        assert client.post(f"/api/contacts/{contact_id}/full", json={"allow": "yes"}, headers=alice).status_code == 400
        assert client.post(f"/api/contacts/{contact_id}/full", json={"allow": True}, headers=alice).status_code == 200
        r = client.post("/api/tasks", json=ask, headers=bob)
        assert r.status_code == 200, r.text
        task = r.json()["task"]
        assert task["status"] == "pending_approval"  # approval still follows the approval rules
        client.post(f"/api/tasks/{task['id']}/approve", headers=alice)
        finish(ws, next_task(ws), result="done")
        # taking it back blocks new tasks and follow-ups again
        client.post(f"/api/contacts/{contact_id}/full", json={"allow": False}, headers=alice)
        assert client.post("/api/tasks", json=ask, headers=bob).status_code == 403
        assert client.post(f"/api/tasks/{task['id']}/follow-up", json={"message": "again"}, headers=bob).status_code == 403
    finally:
        ws.__exit__(None, None, None)
