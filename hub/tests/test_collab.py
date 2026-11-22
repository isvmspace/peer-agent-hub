"""「本机协作」: the owner reads their machine's agent sessions through its connector; the hub keeps none of it."""

import queue
import threading

from test_hub_flow import HELLO, client, register, token  # noqa: F401 - pytest fixture

SECRET_TITLE = "只在本机的会话标题-7f3a"


class FakeMachine:
    """A connector that answers collab_* requests and remembers what it was asked."""

    def __init__(self, client, headers, version="test"):
        self.ws = client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + token(client, headers, "connector")})
        self.ws.__enter__()
        self.ws.send_json({**HELLO, "connector": {**HELLO["connector"], "version": version}})
        assert self.ws.receive_json()["type"] == "welcome"
        self.asked: queue.Queue = queue.Queue()
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop:
            try:
                msg = self.ws.receive_json()
            except Exception:  # noqa: BLE001 - socket closed at teardown
                return
            if not msg["type"].startswith("collab_"):
                continue
            self.asked.put(msg)
            reply = {"type": "rpc_result", "request_id": msg["request_id"]}
            if msg["type"] == "collab_overview":
                reply["overview"] = {"agents": {"claude": 1}, "edges": [],
                                     "sessions": [{"key": "claude:s1", "agent": "claude", "title": SECRET_TITLE, "children": []}]}
            elif msg["type"] == "collab_discussion":
                reply["discussion"] = {"key": msg["key"], "rounds": [{"call": "t1", "prompt": "讨论", "replies": []}]}
            else:
                reply["session"] = {"key": msg["key"], "title": SECRET_TITLE, "start": msg.get("start"), "turns": []}
            self.ws.send_json(reply)

    def close(self):
        self.stop = True
        self.ws.__exit__(None, None, None)


def setup(client):
    admin, _ = register(client, "admin")  # the first account is the administrator
    alice, _ = register(client, "alice")
    machine = FakeMachine(client, alice)
    try:
        cid = client.get("/api/collab/machines", headers=alice).json()["machines"][0]["id"]
    except Exception:
        machine.close()  # an open socket would keep the test client waiting forever
        raise
    return admin, alice, machine, cid


def test_collab_owner_only(client):
    admin, alice, machine, cid = setup(client)
    bob, _ = register(client, "bob")
    try:
        assert client.get("/api/collab/machines", headers=bob).json()["machines"] == []
        for who in (admin, bob):
            r = client.get(f"/api/collab/{cid}/overview?days=3", headers=who)
            assert r.status_code == 403 and "只能查看自己电脑上的协作" in r.text
            assert client.put(f"/api/collab/{cid}", json={"enabled": False}, headers=who).status_code == 403
        assert machine.asked.empty()
    finally:
        machine.close()


def test_collab_forwards_without_storing(client):
    _, alice, machine, cid = setup(client)
    try:
        machines = client.get("/api/collab/machines", headers=alice).json()["machines"]
        assert machines[0]["online"] and machines[0]["enabled"]
        r = client.get(f"/api/collab/{cid}/overview?days=7", headers=alice)
        assert r.status_code == 200 and r.json()["sessions"][0]["title"] == SECRET_TITLE
        asked = machine.asked.get(timeout=5)
        assert (asked["type"], asked["days"]) == ("collab_overview", 7)
        r = client.get(f"/api/collab/{cid}/session?key=claude:s1&days=7&start=20", headers=alice)
        assert r.status_code == 200 and r.json()["start"] == 20
        asked = machine.asked.get(timeout=5)
        assert (asked["type"], asked["key"], asked["start"], asked["days"]) == ("collab_session", "claude:s1", 20, 7)
        # nothing of what the machine sent is kept: no row anywhere holds it
        db = client.app.state.hub.db
        for (table,) in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in db._conn.execute(f"SELECT * FROM {table}").fetchall():
                assert SECRET_TITLE not in " ".join(str(v) for v in row), table
    finally:
        machine.close()


def test_collab_switch_off_stops_queries(client):
    _, alice, machine, cid = setup(client)
    try:
        assert client.put(f"/api/collab/{cid}", json={"enabled": False}, headers=alice).status_code == 200
        assert client.get("/api/collab/machines", headers=alice).json()["machines"][0]["enabled"] is False
        r = client.get(f"/api/collab/{cid}/overview?days=3", headers=alice)
        assert r.status_code == 409 and "已关闭" in r.text
        assert machine.asked.empty()  # the machine was not asked anything
        client.put(f"/api/collab/{cid}", json={"enabled": True}, headers=alice)
        assert client.get(f"/api/collab/{cid}/overview?days=3", headers=alice).status_code == 200
    finally:
        machine.close()


def test_old_connector_told_to_upgrade_at_once(client):
    """Review #4: a connector from before 本机协作 would ignore the request; say so right away instead of timing out."""
    _, alice = register(client, "admin")[0], register(client, "alice")[0]
    machine = FakeMachine(client, alice, version="0.9.10")
    try:
        m = client.get("/api/collab/machines", headers=alice).json()["machines"][0]
        assert m["supported"] is False
        r = client.get(f"/api/collab/{m['id']}/overview?days=3", headers=alice)
        assert r.status_code == 409 and "升级" in r.text
        assert machine.asked.empty()
    finally:
        machine.close()


def test_collab_discussion_forwards(client):
    admin, alice, machine, cid = setup(client)
    try:
        assert client.get(f"/api/collab/{cid}/discussion?key=claude:s1", headers=admin).status_code == 403
        r = client.get(f"/api/collab/{cid}/discussion?key=claude:s1&days=7", headers=alice)
        assert r.status_code == 200 and r.json()["rounds"][0]["prompt"] == "讨论"
        asked = machine.asked.get(timeout=5)
        assert (asked["type"], asked["key"], asked["days"]) == ("collab_discussion", "claude:s1", 7)
    finally:
        machine.close()
