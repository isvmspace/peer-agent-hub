"""Execution flow (phase 1), reliable execution (phase 2), artifacts and workflows (phase 3)."""

from test_hub_flow import client, register, token  # noqa: F401 - pytest fixture

HELLO2 = {
    "type": "hello",
    "connector": {"hostname": "mac", "os": "darwin/arm64", "version": "test"},
    "agents": [
        {"key": "claude", "name": "Claude Code", "kind": "claude", "isolation": "sandbox",
         "capabilities": {"tool_trace": "lifecycle", "usage": "final"},
         "workspaces": [{"name": "repo", "path": "/code/repo"}]},
        {"key": "codex", "name": "Codex", "kind": "codex", "isolation": "sandbox",
         "workspaces": [{"name": "repo", "path": "/code/repo"}]},
    ],
    "running": [],
}


BUFFERED: dict[int, list] = {}


def next_task(ws):
    stash = BUFFERED.setdefault(id(ws), [])
    while True:
        msg = stash.pop(0) if stash else ws.receive_json()
        if msg["type"] == "task":
            return msg["task"]


CONNECTOR_TOKENS: dict[str, str] = {}


def connect(client, headers):
    """Reconnecting with the same headers reuses the connector token, i.e. the same connector."""
    key = headers["Authorization"]
    conn = CONNECTOR_TOKENS.get(key) or token(client, headers, "connector")
    CONNECTOR_TOKENS[key] = conn
    ws = client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn})
    ws.__enter__()
    ws.send_json(HELLO2)
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return ws, welcome


def sync(client, ws, headers):
    """Round-trip a ping so everything sent before it was handled; keep tasks that arrive meanwhile."""
    ws.send_json({"type": "ping"})
    while True:
        msg = ws.receive_json()
        if msg["type"] == "pong":
            return
        if msg["type"] == "task":
            BUFFERED.setdefault(id(ws), []).append(msg)


def test_structured_events_timeline_capabilities_and_precheck(client):
    alice, _ = register(client, "alice")
    ws, welcome = connect(client, alice)
    assert {"events_v2", "ack", "telemetry", "execution_id"} <= set(welcome["features"])
    agents = {a["key"]: a for a in client.get("/api/agents", headers=alice).json()["agents"]}
    assert agents["claude"]["capabilities"] == {"tool_trace": "lifecycle", "usage": "final"}
    # an older connector declares nothing: shown as unknown, never assumed
    assert agents["codex"]["capabilities"] == {"tool_trace": "unknown", "usage": "unknown"}

    pre = client.post("/api/tasks/precheck", json={"agent": "alice/codex"}, headers=alice).json()
    assert pre["ok"] and pre["online"] and not pre["needs_approval"]
    assert any("结构化工具轨迹" in n for n in pre["notes"])

    task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "看下代码"}, headers=alice).json()["task"]
    payload = next_task(ws)
    assert payload["execution_id"]
    tid, ex = payload["id"], payload["execution_id"]
    ws.send_json({"type": "task_started", "task_id": tid, "turn": 1, "execution_id": ex})
    ws.send_json({"type": "task_event", "task_id": tid, "turn": 1, "execution_id": ex, "kind": "tool", "content": "Read a.py",
                  "payload": {"call_id": "c1", "name": "Read", "phase": "started", "summary": "a.py", "observed_at": 100.0}})
    sync(client, ws, alice)
    detail = client.get(f"/api/tasks/{tid}", headers=alice).json()["task"]
    assert detail["wait"]["code"] == "tool" and "Read" in detail["wait"]["text"]
    ws.send_json({"type": "task_event", "task_id": tid, "turn": 1, "execution_id": ex, "kind": "tool", "content": "",
                  "payload": {"call_id": "c1", "name": "Read", "phase": "completed", "summary": "120 行", "observed_at": 101.5}})
    # only a finish was observed: no invented start time
    ws.send_json({"type": "task_event", "task_id": tid, "turn": 1, "execution_id": ex, "kind": "tool", "content": "",
                  "payload": {"call_id": "c2", "name": "Bash", "phase": "failed", "summary": "exit 1", "exit_code": 1}})
    # malformed structure keeps the text only
    ws.send_json({"type": "task_event", "task_id": tid, "turn": 1, "execution_id": ex, "kind": "tool", "content": "raw text",
                  "payload": {"name": "X"}})
    ws.send_json({"type": "task_done", "task_id": tid, "turn": 1, "execution_id": ex, "status": "completed", "result": "ok",
                  "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01, "bogus": "x"},
                  "code_ref": {"commit": "abc123", "dirty": "false", "secret": "no"}})
    sync(client, ws, alice)

    tree = client.get(f"/api/tasks/{tid}/tree", headers=alice).json()
    node = tree["nodes"][0]
    tools = [s for s in node["timeline"] if s["type"] == "tool"]
    assert tools[0]["name"] == "Read" and tools[0]["start"] == 100.0 and tools[0]["end"] == 101.5 and tools[0]["phase"] == "completed"
    assert tools[1]["name"] == "Bash" and tools[1]["start"] is None and tools[1]["exit_code"] == 1
    usage = [s for s in node["timeline"] if s["type"] == "usage"][0]["usage"]
    assert usage == {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01, "source": "reported"}
    events = client.get(f"/api/tasks/{tid}", headers=alice).json()["events"]
    assert any(e["kind"] == "output" and e["content"] == "raw text" for e in events)
    assert client.get(f"/api/tasks/{tid}", headers=alice).json()["task"]["code_ref"] == {"commit": "abc123", "dirty": "false"}
    found = client.get("/api/tasks?q=" + "看下", headers=alice).json()["tasks"]
    assert [t["id"] for t in found] == [tid]
    assert [t["id"] for t in client.get(f"/api/tasks?q=%23{tid}", headers=alice).json()["tasks"]] == [tid]
    ws.__exit__(None, None, None)


def test_acked_reports_apply_once_and_stale_attempts_are_ignored(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "a"}, headers=alice)
    first = next_task(ws)
    tid = first["id"]
    # connector drops; the dispatched task is sent again with a new execution id
    ws.__exit__(None, None, None)
    ws, _ = connect(client, alice)
    second = next_task(ws)
    assert second["id"] == tid and second["execution_id"] != first["execution_id"]
    ws.send_json({"type": "task_started", "task_id": tid, "turn": 1, "execution_id": second["execution_id"]})
    # the old attempt reports late: ignored
    ws.send_json({"type": "task_done", "task_id": tid, "turn": 1, "execution_id": first["execution_id"], "status": "failed", "error": "old"})
    sync(client, ws, alice)
    assert client.get(f"/api/tasks/{tid}", headers=alice).json()["task"]["status"] == "running"
    done = {"type": "task_done", "task_id": tid, "turn": 1, "execution_id": second["execution_id"], "status": "completed",
            "result": "fine", "msg_id": "m-1"}
    ws.send_json(done)
    assert ws.receive_json() == {"type": "ack", "msg_id": "m-1"}
    ws.send_json(done)  # replay after a lost ack: acknowledged again, applied once
    assert ws.receive_json() == {"type": "ack", "msg_id": "m-1"}
    sync(client, ws, alice)
    events = client.get(f"/api/tasks/{tid}", headers=alice).json()["events"]
    assert [e["content"] for e in events if e["kind"] == "result"] == ["fine"]
    detail = client.get(f"/api/tasks/{tid}", headers=alice).json()["task"]
    assert detail["status"] == "completed" and detail["attempts"] == 2
    ws.__exit__(None, None, None)


def test_tree_cancel_waits_for_stop_confirmation(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    parent = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "root"}, headers=alice).json()["task"]
    p = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"]})
    child = client.post("/api/tasks", json={"agent": "alice/codex", "prompt": "child"}, headers=alice).json()["task"]
    hub = client.app.state.hub
    hub.db.execute("UPDATE tasks SET parent_task_id = ?, depth = 1 WHERE id = ?", (parent["id"], child["id"]))
    c = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": c["id"], "turn": 1, "execution_id": c["execution_id"]})
    sync(client, ws, alice)
    r = client.post(f"/api/tasks/{parent['id']}/cancel", json={"tree": True}, headers=alice)
    assert r.status_code == 200, r.text
    cancels = []
    while len(cancels) < 2:
        msg = ws.receive_json()
        if msg["type"] == "cancel":
            cancels.append(msg["task_id"])
    assert sorted(cancels) == sorted([parent["id"], child["id"]])
    for tid in (parent["id"], child["id"]):
        t = client.get(f"/api/tasks/{tid}", headers=alice).json()["task"]
        assert t["status"] == "canceled" and t["stop_pending"] == 1
    ws.send_json({"type": "task_done", "task_id": child["id"], "turn": 1, "status": "failed", "error": "已取消"})
    sync(client, ws, alice)
    t = client.get(f"/api/tasks/{child['id']}", headers=alice)
    assert t.json()["task"]["stop_pending"] == 0 and any("确认进程已停止" in e["content"] for e in t.json()["events"])
    # the parent's process is confirmed gone when the connector comes back without it
    ws.__exit__(None, None, None)
    ws, _ = connect(client, alice)
    assert client.get(f"/api/tasks/{parent['id']}", headers=alice).json()["task"]["stop_pending"] == 0
    assert client.post(f"/api/tasks/{parent['id']}/cancel", json={"tree": True}, headers=alice).status_code == 400
    ws.__exit__(None, None, None)


def test_wait_cycle_detection(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    a = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "A"}, headers=alice).json()["task"]
    pa = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": pa["id"], "turn": 1, "execution_id": pa["execution_id"]})
    sync(client, ws, alice)
    # B queued on the same agent A occupies: A blocking on B would never end
    b = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "B"}, headers=alice).json()["task"]
    assert hub.wait_cycle(a["id"], b["id"]) == [a["id"], b["id"], a["id"]]
    detail = client.get(f"/api/tasks/{b['id']}", headers=alice).json()["task"]
    assert detail["wait"]["code"] in ("queued", "starting")
    # a task on another agent is fine…
    c = client.post("/api/tasks", json={"agent": "alice/codex", "prompt": "C"}, headers=alice).json()["task"]
    pc = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": pc["id"], "turn": 1, "execution_id": pc["execution_id"]})
    sync(client, ws, alice)
    assert hub.wait_cycle(a["id"], c["id"]) is None
    # …until it waits for a workspace lock A holds
    ws.send_json({"type": "telemetry", "agents": [
        {"key": "claude", "running": a["id"], "queued": [b["id"]], "oldest_wait_s": 3},
        {"key": "codex", "running": None, "queued": [c["id"]], "oldest_wait_s": 1,
         "lock_wait": {"task_id": c["id"], "holder_task_id": a["id"], "path": "/code/repo"}},
    ]})
    sync(client, ws, alice)
    assert hub.wait_cycle(a["id"], c["id"]) == [a["id"], c["id"], a["id"]]
    detail = client.get(f"/api/tasks/{c['id']}", headers=alice).json()["task"]
    assert detail["wait"]["code"] == "workspace_lock" and f"#{a['id']}" in detail["wait"]["text"]
    load = {x["key"]: x["load"] for x in client.get("/api/agents", headers=alice).json()["agents"]}
    assert load["claude"]["source"] == "connector" and load["claude"]["queued"] == 1 and load["claude"]["running"] == 1
    # lock released: the edge disappears
    ws.send_json({"type": "telemetry", "agents": [{"key": "codex", "running": c["id"], "queued": [], "oldest_wait_s": 0}]})
    sync(client, ws, alice)
    assert hub.wait_cycle(a["id"], c["id"]) is None
    ws.__exit__(None, None, None)


def test_artifacts_versions_review_and_cross_owner_reads(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "写报告"}, headers=alice).json()["task"]
    p = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"]})
    sync(client, ws, alice)
    running = hub.get_task(task["id"])
    a1 = hub.submit_artifact(running, "review.md", "report", "第一版")
    a2 = hub.submit_artifact(running, "review.md", "report", "第二版")
    assert (a1["version"], a2["version"]) == (1, 2) and a1["sha256"] != a2["sha256"]
    listed = client.get(f"/api/tasks/{task['id']}/artifacts", headers=alice).json()["artifacts"]
    assert [a["ref"] for a in listed] == [a1["ref"], a2["ref"]]
    r = client.get(f"/api/artifacts/{a1['id']}", headers=alice)
    assert r.content.decode() == "第一版" and r.headers["x-content-sha256"] == a1["sha256"]
    assert client.get(f"/api/artifacts/{a1['id']}", headers=bob).status_code == 403
    # tampered file is refused rather than served
    (hub.attachments_dir / str(task["id"]) / a1["stored"]).write_text("改过了")
    assert client.get(f"/api/artifacts/{a1['id']}", headers=alice).status_code == 409
    for bad in (("", "report", "x"), ("a.md", "zip", "x"), ("a.md", "report", "")):
        try:
            hub.submit_artifact(running, *bad)
            raise AssertionError("accepted bad artifact")
        except Exception as exc:  # noqa: BLE001
            assert "产物" in str(exc) or "kind" in str(exc)
    # a task dispatched by the same requester may read it, a stranger's task may not
    other = hub.get_task(task["id"])
    assert hub.readable_artifact(hub.get_user(other["owner_user_id"]), a2["id"], None)["id"] == a2["id"]
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "status": "completed", "result": "done"})
    sync(client, ws, alice)
    assert client.post(f"/api/artifacts/{a2['id']}/review", json={"verdict": "rework"}, headers=alice).status_code == 400
    assert client.post(f"/api/artifacts/{a2['id']}/review", json={"verdict": "accepted"}, headers=bob).status_code == 403
    r = client.post(f"/api/artifacts/{a2['id']}/review", json={"verdict": "rework", "note": "补测试"}, headers=alice)
    assert r.json()["artifact"]["review"] == "rework"
    again = client.get(f"/api/tasks/{task['id']}", headers=alice).json()["task"]
    assert again["turn"] == 2 and again["status"] in ("queued", "dispatched")
    ws.__exit__(None, None, None)


def finish(ws, payload, result="ok", status="completed"):
    ws.send_json({"type": "task_started", "task_id": payload["id"], "turn": payload["turn"], "execution_id": payload["execution_id"]})
    ws.send_json({"type": "task_done", "task_id": payload["id"], "turn": payload["turn"], "execution_id": payload["execution_id"],
                  "status": status, "result": result})


def test_workflow_runs_steps_once_in_order(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    templates = client.get("/api/workflows", headers=alice).json()["templates"]
    assert {t["id"] for t in templates} == {"build_review_test", "dual_review"}
    steps = [{"agent": "alice/codex"}, {"agent": "alice/claude", "gate": True}, {"agent": "alice/codex"}]
    run = client.post("/api/workflows", json={"template": "build_review_test", "input": "加一个开关", "steps": steps},
                      headers=alice).json()["run"]
    assert [s["status"] for s in run["steps"]] == ["running", "waiting", "waiting"]
    impl = next_task(ws)
    assert "加一个开关" in impl["prompt"] and "实现" in impl["title"]
    finish(ws, impl, "实现完成")
    sync(client, ws, alice)
    # duplicate finish report must not start the next step twice
    ws.send_json({"type": "task_done", "task_id": impl["id"], "turn": 1, "status": "completed", "result": "实现完成"})
    sync(client, ws, alice)
    run = client.get(f"/api/workflows/{run['id']}", headers=alice).json()["run"]
    assert [s["status"] for s in run["steps"]] == ["completed", "gated", "waiting"]
    run = client.post(f"/api/workflows/{run['id']}/continue", json={"step": 1}, headers=alice).json()["run"]
    review = next_task(ws)
    assert "实现完成" in review["prompt"] and f"任务 #{impl['id']}" in review["prompt"]
    finish(ws, review, "通过")
    sync(client, ws, alice)
    test = next_task(ws)
    finish(ws, test, "测试通过")
    sync(client, ws, alice)
    run = client.get(f"/api/workflows/{run['id']}", headers=alice).json()["run"]
    assert run["status"] == "completed" and [s["status"] for s in run["steps"]] == ["completed"] * 3
    counts = client.app.state.db.one("SELECT COUNT(*) AS n FROM tasks WHERE workflow_step_id IS NOT NULL")["n"]
    assert counts == 3
    ws.__exit__(None, None, None)


def test_workflow_failure_skips_rest_and_fan_in_waits(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    steps = [{"agent": "alice/claude"}, {"agent": "alice/codex"}, {"agent": "alice/claude"}]
    run = client.post("/api/workflows", json={"template": "dual_review", "input": "评审方案", "steps": steps}, headers=alice).json()["run"]
    assert [s["status"] for s in run["steps"]] == ["running", "running", "waiting"]
    first, second = next_task(ws), next_task(ws)
    finish(ws, first, "A 意见")
    sync(client, ws, alice)
    run = client.get(f"/api/workflows/{run['id']}", headers=alice).json()["run"]
    assert run["steps"][2]["status"] == "waiting"  # fan-in waits for both reviews
    finish(ws, second, "失败了", status="failed")
    sync(client, ws, alice)
    run = client.get(f"/api/workflows/{run['id']}", headers=alice).json()["run"]
    assert run["status"] == "failed" and run["steps"][2]["status"] == "skipped"
    bad = client.post("/api/workflows", json={"template": "dual_review", "input": "x", "steps": steps[:2]}, headers=alice)
    assert bad.status_code == 400
    ws.__exit__(None, None, None)


# ---- regressions from the 0.8.0 QA round (QA080-xx) -------------------------------------------------


def start(ws, client, headers, agent="alice/claude", prompt="p"):
    task = client.post("/api/tasks", json={"agent": agent, "prompt": prompt}, headers=headers).json()["task"]
    p = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": p["id"], "turn": p["turn"], "execution_id": p["execution_id"]})
    sync(client, ws, headers)
    return task, p


def test_qa080_01_ack_only_after_the_report_committed(client, monkeypatch):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task, p = start(ws, client, alice)
    real = hub._set_status
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(hub, "_set_status", flaky)
    done = {"type": "task_done", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "status": "completed",
            "result": "ok", "msg_id": "m-crash"}
    ws.send_json(done)
    # the failure ends the session (as a crash would); nothing was applied, recorded as seen, or acked
    try:
        while ws.receive_json()["type"] != "ack":
            pass
        raise AssertionError("acked a report that was not applied")
    except Exception as exc:  # noqa: BLE001 - socket closed
        assert not isinstance(exc, AssertionError), exc
    try:
        ws.__exit__(None, None, None)
    except Exception:  # noqa: BLE001 - the server already closed it
        pass
    assert client.get(f"/api/tasks/{p['id']}", headers=alice).json()["task"]["status"] == "running"
    assert not client.app.state.db.one("SELECT 1 FROM connector_messages WHERE msg_id = 'm-crash'")
    assert not [e for e in client.get(f"/api/tasks/{p['id']}", headers=alice).json()["events"] if e["kind"] == "result"]
    # the connector reconnects with the result still in its outbox and replays it
    conn = CONNECTOR_TOKENS[alice["Authorization"]]
    ws = client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn})
    ws.__enter__()
    ws.send_json({**HELLO2, "finished": [p["id"]]})
    assert ws.receive_json()["type"] == "welcome"
    ws.send_json(done)
    while (m := ws.receive_json())["type"] != "ack":
        pass
    assert m == {"type": "ack", "msg_id": "m-crash"}
    sync(client, ws, alice)
    detail = client.get(f"/api/tasks/{p['id']}", headers=alice).json()
    assert detail["task"]["status"] == "completed"
    assert [e["content"] for e in detail["events"] if e["kind"] == "result"] == ["ok"]
    ws.__exit__(None, None, None)


def test_qa080_02_disabled_users_tokens_stop_working(client):
    admin, _ = register(client, "root")
    bob, bob_user = register(client, "bob")
    personal = token(client, bob, "personal")
    from agent_hub.auth import resolve_api_token
    db = client.app.state.db
    assert resolve_api_token(db, personal, "personal")
    assert client.put(f"/api/admin/users/{bob_user['id']}", json={"disabled": True}, headers=admin).status_code == 200
    assert resolve_api_token(db, personal, ("personal", "task")) is None


def test_qa080_03_cancel_is_confirmed_even_when_offline_or_not_started(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    # dispatched but never reported started
    client.post("/api/tasks", json={"agent": "alice/codex", "prompt": "a"}, headers=alice)
    d = next_task(ws)
    client.post(f"/api/tasks/{d['id']}/cancel", json={}, headers=alice)
    t = client.get(f"/api/tasks/{d['id']}", headers=alice).json()["task"]
    assert t["status"] == "canceled" and t["stop_pending"] == 1 and t["wait"]["code"] == "stopping"
    # running, then the connector drops before the cancel
    task, p = start(ws, client, alice)
    ws.__exit__(None, None, None)
    client.post(f"/api/tasks/{p['id']}/cancel", json={}, headers=alice)
    t = client.get(f"/api/tasks/{p['id']}", headers=alice).json()["task"]
    assert t["stop_pending"] == 1 and "离线" in t["wait"]["text"]
    # it comes back still running it: the stop is sent again and stays unconfirmed until it reports
    conn = CONNECTOR_TOKENS[alice["Authorization"]]
    ws = client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + conn})
    ws.__enter__()
    ws.send_json({**HELLO2, "running": [p["id"]]})
    assert ws.receive_json()["type"] == "welcome"
    msgs = []
    ws.send_json({"type": "ping"})
    while True:
        m = ws.receive_json()
        if m["type"] == "pong":
            break
        msgs.append(m)
    assert {"type": "cancel", "task_id": p["id"]} in msgs
    assert client.get(f"/api/tasks/{p['id']}", headers=alice).json()["task"]["stop_pending"] == 1
    # the never-started one was not in `running`: confirmed stopped
    assert client.get(f"/api/tasks/{d['id']}", headers=alice).json()["task"]["stop_pending"] == 0
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 1, "status": "failed", "error": "已取消"})
    sync(client, ws, alice)
    assert client.get(f"/api/tasks/{p['id']}", headers=alice).json()["task"]["stop_pending"] == 0
    ws.__exit__(None, None, None)


def test_qa080_04_cancel_while_a_step_is_being_created(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    app = client.app
    hub, flows = app.state.hub, None
    for cb in hub.on_finished:
        if type(getattr(cb, "__self__", None)).__name__ == "Workflows":
            flows = cb.__self__
    real = hub.create_task
    user = hub.get_user(1)

    async def create_then_cancel(*args, **kwargs):
        task = await real(*args, **kwargs)
        run_id = app.state.db.one("SELECT run_id FROM workflow_steps WHERE id = ?", (kwargs["workflow_step_id"],))["run_id"]
        await flows.cancel(user, run_id)
        return task

    hub.create_task = create_then_cancel
    steps = [{"agent": "alice/claude"}, {"agent": "alice/codex"}, {"agent": "alice/claude"}]
    r = client.post("/api/workflows", json={"template": "build_review_test", "input": "x", "steps": steps}, headers=alice)
    hub.create_task = real
    run = client.get(f"/api/workflows/{r.json()['run']['id']}", headers=alice).json()["run"]
    assert run["status"] == "canceled"
    tasks = app.state.db.all("SELECT id, status FROM tasks WHERE workflow_step_id IS NOT NULL")
    assert tasks and all(t["status"] == "canceled" for t in tasks)
    ws.__exit__(None, None, None)


def test_qa080_06_invisible_tree_nodes_show_only_structure(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    from test_hub_flow import make_friends
    make_friends(client, alice, bob, "bob")
    ws, _ = connect(client, alice)
    parent, pp = start(ws, client, alice, prompt="secret parent")
    ws.send_json({"type": "task_event", "task_id": pp["id"], "turn": 1, "execution_id": pp["execution_id"], "kind": "tool",
                  "content": "", "payload": {"call_id": "s", "name": "PrivateSecretTool", "phase": "started", "summary": "x"}})
    child, cp = start(ws, client, alice, agent="alice/codex", prompt="shared child")
    client.app.state.db.execute("UPDATE tasks SET parent_task_id = ?, depth = 1, from_agent = 'claude' WHERE id = ?", (pp["id"], cp["id"]))
    assert client.post(f"/api/tasks/{cp['id']}/shares", json={"user": "bob"}, headers=alice).status_code == 200
    tree = client.get(f"/api/tasks/{cp['id']}/tree", headers=bob).json()
    hidden = next(n for n in tree["nodes"] if n["id"] == pp["id"])
    assert set(hidden) == {"id", "parent_task_id", "status", "depth", "created_at", "updated_at", "finished_at", "visible", "stop_pending"}
    assert "PrivateSecretTool" not in str(tree["nodes"])
    ws.__exit__(None, None, None)


def test_qa080_07_08_per_turn_baseline_and_queued_rework(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task, p = start(ws, client, alice)
    a1 = hub.submit_artifact(hub.get_task(p["id"]), "r.md", "report", "v1")
    assert a1["code_ref"] == {}  # unknown until the turn ends
    # rework while the turn still runs is queued, not lost
    r = client.post(f"/api/artifacts/{a1['id']}/review", json={"verdict": "rework", "note": "补测试"}, headers=alice).json()
    assert r["artifact"]["rework"] == "queued"
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "status": "completed",
                  "result": "r1", "code_ref": {"commit": "aaa"}})
    turn2 = next_task(ws)  # the queued rework went out exactly once
    assert turn2["turn"] == 2 and "补测试" in turn2["prompt"]
    ws.send_json({"type": "task_started", "task_id": p["id"], "turn": 2, "execution_id": turn2["execution_id"]})
    sync(client, ws, alice)
    a2 = hub.submit_artifact(hub.get_task(p["id"]), "r.md", "report", "v2")
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 2, "execution_id": turn2["execution_id"], "status": "completed",
                  "result": "r2", "code_ref": {"commit": "bbb"}})
    sync(client, ws, alice)
    assert hub.artifact(a1["id"])["code_ref"] == {"commit": "aaa"}
    assert hub.artifact(a2["id"])["code_ref"] == {"commit": "bbb"}
    assert client.get(f"/api/tasks/{p['id']}", headers=alice).json()["task"]["turn"] == 2
    ws.__exit__(None, None, None)


def test_qa080_09_one_recover_pass_settles_and_moves_on(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    app = client.app
    steps = [{"agent": "alice/codex"}, {"agent": "alice/claude"}, {"agent": "alice/codex"}]
    run = client.post("/api/workflows", json={"template": "build_review_test", "input": "x", "steps": steps}, headers=alice).json()["run"]
    first = next_task(ws)
    finish(ws, first)
    sync(client, ws, alice)
    second = next_task(ws)
    # crash window: the step row never got its task id, though the task exists and finished
    db = app.state.db
    db.execute("UPDATE workflow_steps SET status = 'starting', task_id = NULL WHERE run_id = ? AND idx = 1", (run["id"],))
    finish(ws, second)
    sync(client, ws, alice)
    flows = next(cb.__self__ for cb in app.state.hub.on_finished if type(getattr(cb, "__self__", None)).__name__ == "Workflows")
    client.portal.call(flows.recover)
    run = client.get(f"/api/workflows/{run['id']}", headers=alice).json()["run"]
    assert [s["status"] for s in run["steps"]] == ["completed", "completed", "running"]
    assert db.one("SELECT COUNT(*) AS n FROM tasks WHERE workflow_step_id IS NOT NULL")["n"] == 3
    ws.__exit__(None, None, None)


def test_qa080_10_stale_or_old_session_telemetry_is_dropped(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    ws.send_json({"type": "telemetry", "agents": [{"key": "claude", "running": None, "queued": [1, 2], "oldest_wait_s": 44}]})
    sync(client, ws, alice)
    load = lambda: {a["key"]: a["load"] for a in client.get("/api/agents", headers=alice).json()["agents"]}["claude"]  # noqa: E731
    assert load()["source"] == "connector" and load()["queued"] == 2
    for live in hub.telemetry.values():
        live["at"] -= 3600
    assert load()["source"] == "hub"
    ws.__exit__(None, None, None)
    ws, _ = connect(client, alice)  # reconnect without telemetry
    assert load()["source"] == "hub" and load()["queued"] == 0
    ws.__exit__(None, None, None)


def test_qa080_16_hidden_agent_hidden_from_pending_requesters(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    from test_hub_flow import make_friends
    make_friends(client, bob, alice, "alice")
    ws, _ = connect(client, alice)
    assert client.post("/api/contacts", json={"agent": "alice/claude"}, headers=bob).status_code == 200
    agent_id = next(a["id"] for a in client.get("/api/agents", headers=alice).json()["agents"] if a["key"] == "claude")
    client.put(f"/api/agents/{agent_id}/visibility", json={"hidden": True}, headers=alice)
    assert "alice/claude" not in [a["address"] for a in client.get("/api/agents", headers=bob).json()["agents"]]
    assert client.post("/api/contacts", json={"agent": "alice/claude"}, headers=bob).status_code == 404
    ws.__exit__(None, None, None)


def test_qa081_01_queued_rework_survives_a_restart_and_is_sent_once(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task, p = start(ws, client, alice)
    a = hub.submit_artifact(hub.get_task(p["id"]), "r.md", "report", "v1")
    client.post(f"/api/artifacts/{a['id']}/review", json={"verdict": "rework", "note": "补测试"}, headers=alice)
    # the turn finishes, but the hub stops before its finish callback runs
    callbacks, hub.on_finished = hub.on_finished, []
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "status": "completed", "result": "r"})
    sync(client, ws, alice)
    hub.on_finished = callbacks
    assert hub.get_task(p["id"])["turn"] == 1 and hub.artifact(a["id"])["rework_pending"] == 1
    client.portal.call(hub.recover_rework)  # start-up recovery
    turn2 = next_task(ws)
    assert turn2["turn"] == 2 and "补测试" in turn2["prompt"]
    assert hub.artifact(a["id"])["rework_pending"] == 0
    client.portal.call(hub.recover_rework)  # running it again sends nothing more
    assert hub.get_task(p["id"])["turn"] == 2
    # crash after the follow-up but before the flag was cleared: recovery only clears it
    client.app.state.db.execute("UPDATE artifacts SET rework_pending = 1 WHERE id = ?", (a["id"],))
    client.portal.call(hub.recover_rework)
    assert hub.get_task(p["id"])["turn"] == 2 and hub.artifact(a["id"])["rework_pending"] == 0
    ws.__exit__(None, None, None)


def test_qa081_02_stale_lock_wait_is_not_evidence_of_a_deadlock(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    a, pa = start(ws, client, alice)
    client.post("/api/tasks", json={"agent": "alice/codex", "prompt": "B"}, headers=alice)
    pb = next_task(ws)
    ws.send_json({"type": "telemetry", "agents": [{"key": "codex", "running": None, "queued": [pb["id"]], "oldest_wait_s": 1,
                                                   "lock_wait": {"task_id": pb["id"], "holder_task_id": pa["id"]}}]})
    sync(client, ws, alice)
    assert hub.wait_cycle(pa["id"], pb["id"])  # fresh: a real cycle
    hub.lock_waits[pb["id"]] = (pa["id"], hub.lock_waits[pb["id"]][1] - 3600)
    assert hub.lock_holder(pb["id"]) is None and hub.wait_cycle(pa["id"], pb["id"]) is None
    assert hub.wait_reason(hub.get_task(pb["id"]))["code"] != "workspace_lock"
    # and a task that started holds its workspace
    ws.send_json({"type": "telemetry", "agents": [{"key": "codex", "running": None, "queued": [pb["id"]], "oldest_wait_s": 1,
                                                   "lock_wait": {"task_id": pb["id"], "holder_task_id": pa["id"]}}]})
    ws.send_json({"type": "task_started", "task_id": pb["id"], "turn": 1, "execution_id": pb["execution_id"]})
    sync(client, ws, alice)
    assert hub.lock_holder(pb["id"]) is None
    ws.__exit__(None, None, None)


def test_qa082_01_rework_made_while_the_old_one_is_sent_is_kept(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task, p = start(ws, client, alice)
    a = hub.submit_artifact(hub.get_task(p["id"]), "r.md", "report", "v1")
    client.post(f"/api/artifacts/{a['id']}/review", json={"verdict": "rework", "note": "第一次"}, headers=alice)
    real = hub.follow_up

    async def follow_up_with_new_review(*args, **kwargs):
        result = await real(*args, **kwargs)
        # while the first rework is being sent (turn 2 now exists), the same artifact is sent back again
        hub.db.execute("UPDATE artifacts SET review = 'rework', review_note = '第二次', rework_pending = 1, rework_turn = 2, "
                       "reviewed_at = reviewed_at + 1 WHERE id = ?", (a["id"],))
        return result

    hub.follow_up = follow_up_with_new_review
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "status": "completed", "result": "r"})
    turn2 = next_task(ws)
    hub.follow_up = real
    assert "第一次" in turn2["prompt"]
    assert hub.artifact(a["id"])["rework_pending"] == 1  # the second request survived the first one's cleanup
    ws.send_json({"type": "task_started", "task_id": p["id"], "turn": 2, "execution_id": turn2["execution_id"]})
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 2, "execution_id": turn2["execution_id"], "status": "completed", "result": "r2"})
    turn3 = next_task(ws)
    assert turn3["turn"] == 3 and "第二次" in turn3["prompt"] and "第一次" not in turn3["prompt"]
    ws.__exit__(None, None, None)


def test_qa082_03_load_drops_the_lock_wait_once_the_task_started(client):
    alice, _ = register(client, "alice")
    ws, _ = connect(client, alice)
    a, pa = start(ws, client, alice)
    client.post("/api/tasks", json={"agent": "alice/codex", "prompt": "B"}, headers=alice)
    pb = next_task(ws)
    ws.send_json({"type": "telemetry", "agents": [{"key": "codex", "running": None, "queued": [pb["id"]], "oldest_wait_s": 1,
                                                   "lock_wait": {"task_id": pb["id"], "holder_task_id": pa["id"]}}]})
    sync(client, ws, alice)
    load = lambda: {x["key"]: x["load"] for x in client.get("/api/agents", headers=alice).json()["agents"]}["codex"]  # noqa: E731
    assert load()["lock_wait"]["holder_task_id"] == pa["id"]
    ws.send_json({"type": "task_started", "task_id": pb["id"], "turn": 1, "execution_id": pb["execution_id"]})
    sync(client, ws, alice)
    now = load()
    assert now["lock_wait"] is None and now["running"] == 1 and now["queued"] == 0
    ws.__exit__(None, None, None)


def test_prompt_sender_and_redacted_process_for_the_requester(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "查服务"}, headers=alice).json()["task"]
    p = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"]})
    ws.send_json({"type": "task_event", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "kind": "tool",
                  "content": "Bash ssh -i ~/.ssh/key root@10.0.0.1",
                  "payload": {"call_id": "c1", "name": "Bash", "phase": "started", "summary": "ssh -i ~/.ssh/key", "observed_at": 1.0}})
    ws.send_json({"type": "task_event", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "kind": "tool",
                  "content": "Bash done", "payload": {"call_id": "c1", "name": "Bash", "phase": "completed", "summary": "uptime 3 days"}})
    ws.send_json({"type": "task_event", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "kind": "tool",
                  "content": "Bash curl", "payload": {"call_id": "c2", "name": "Bash", "phase": "started",
                                                      "summary": "curl -H 'Authorization: Bearer abcdefghijkl' http://x/health"}})
    ws.send_json({"type": "task_event", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"], "kind": "output",
                  "content": "先检查服务，token=zzzzzzzz"})
    ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"],
                  "status": "completed", "result": "正常"})
    sync(client, ws, alice)
    bob_user = hub.db.one("SELECT * FROM users WHERE username = 'bob'")
    hub.db.execute("UPDATE tasks SET from_user_id = ? WHERE id = ?", (bob_user["id"], task["id"]))
    # a prompt stored before senders were recorded falls back to the status note
    hub.db.execute("UPDATE task_events SET payload = NULL WHERE task_id = ? AND kind = 'prompt'", (task["id"],))
    seen = client.get(f"/api/tasks/{task['id']}", headers=bob).json()
    assert seen["task"]["sees_process"] is False
    tools = [e["payload"]["summary"] for e in seen["events"] if e["kind"] == "tool"]
    # the requester follows the process, but a call touching keys is hidden start to finish and values are masked
    assert tools[:2] == ["（涉及凭据或密钥，内容已隐藏）"] * 2
    assert "abcdefghijkl" not in tools[2] and "http://x/health" in tools[2]
    assert [e["content"] for e in seen["events"] if e["kind"] == "output"] == ["先检查服务，token=***"]
    tree = client.get(f"/api/tasks/{task['id']}/tree", headers=bob).json()
    spans = [s for n in tree["nodes"] for s in n.get("timeline", []) if s["type"] == "tool"]
    assert spans and spans[0]["summary"] == "（涉及凭据或密钥，内容已隐藏）" and spans[0]["result"] == "（涉及凭据或密钥，内容已隐藏）"
    assert [e["payload"]["by"] for e in seen["events"] if e["kind"] == "prompt"] == ["bob"]
    owner = client.get(f"/api/tasks/{task['id']}", headers=alice).json()
    assert owner["task"]["sees_process"] is True and [e for e in owner["events"] if e["kind"] == "tool"]
    client.post(f"/api/tasks/{task['id']}/follow-up", json={"message": "再看下"}, headers=alice)
    q = next_task(ws)
    assert q["prompt"].startswith("【保密要求】") and q["prompt"].endswith("再看下")
    events = client.get(f"/api/tasks/{task['id']}", headers=bob).json()["events"]
    assert [e["payload"]["by"] for e in events if e["kind"] == "prompt"] == ["bob", "alice"]
    tree = client.get(f"/api/tasks/{task['id']}/tree", headers=bob).json()["nodes"][0]
    assert all("abcdefghijkl" not in s["summary"] for s in tree["timeline"] if s["type"] == "tool")
    ws.__exit__(None, None, None)


def test_redaction_covers_split_keys_long_calls_errors_paths_and_activity(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "看看"}, headers=alice).json()["task"]
    p = next_task(ws)
    ws.send_json({"type": "task_started", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"]})
    bob_id = hub.db.one("SELECT id FROM users WHERE username = 'bob'")["id"]
    hub.db.execute("UPDATE tasks SET from_user_id = ? WHERE id = ?", (bob_id, task["id"]))

    try:
        def send(kind, content, payload=None):
            ws.send_json({"type": "task_event", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"],
                          "kind": kind, "content": content, **({"payload": payload} if payload else {})})

        send("output", "-----BEGIN PRIVATE KEY-----")
        send("output", "KEYBODY0123456789")
        send("output", "-----END PRIVATE KEY----- 读取 /Users/alice/proj/app/main.py")
        long = "echo " + "x" * 650 + "; cat .env"
        send("tool", long, {"call_id": "c1", "name": "Bash", "phase": "started", "summary": long})
        send("tool", "done", {"call_id": "c1", "name": "Bash", "phase": "completed", "summary": "OPAQUE_VALUE_0246"})
        sync(client, ws, alice)
        tid = task["id"]
        listed = next(t for t in client.get("/api/tasks", headers=bob).json()["tasks"] if t["id"] == tid)
        tree = client.get(f"/api/tasks/{tid}/tree", headers=bob).json()["nodes"][0]
        assert "OPAQUE" not in str(listed.get("activity")) and "OPAQUE" not in str(tree)
        events = client.get(f"/api/tasks/{tid}", headers=bob).json()["events"]
        text = " ".join(e["content"] for e in events)
        assert "KEYBODY" not in text and "OPAQUE" not in text and "/Users/alice" not in text and "…/main.py" in text
        owner = client.get(f"/api/tasks/{tid}", headers=alice).json()["events"]
        assert "/Users/alice/proj" in " ".join(e["content"] for e in owner)
        ws.send_json({"type": "task_done", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"],
                      "status": "failed", "error": "exit 7\npassword=STDERR_PW_123"})
        sync(client, ws, alice)
        seen = client.get(f"/api/tasks/{tid}", headers=bob).json()
        assert "STDERR_PW" not in str(seen["task"]) and "STDERR_PW" not in str(seen["events"])
    finally:
        ws.__exit__(None, None, None)


def test_open_private_key_stays_hidden_in_summaries_and_later_pages(client):
    alice, _ = register(client, "alice")
    bob, _ = register(client, "bob")
    ws, _ = connect(client, alice)
    hub = client.app.state.hub
    task = client.post("/api/tasks", json={"agent": "alice/claude", "prompt": "看看"}, headers=alice).json()["task"]
    p = next_task(ws)
    try:
        ws.send_json({"type": "task_started", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"]})
        bob_id = hub.db.one("SELECT id FROM users WHERE username = 'bob'")["id"]
        hub.db.execute("UPDATE tasks SET from_user_id = ? WHERE id = ?", (bob_id, task["id"]))
        chunks = ["-----BEGIN PRIVATE KEY-----"] + [f"KEYBODY{i:04d}" for i in range(620)]
        for c in chunks:
            ws.send_json({"type": "task_event", "task_id": p["id"], "turn": 1, "execution_id": p["execution_id"],
                          "kind": "output", "content": c})
        sync(client, ws, alice)
        tid = task["id"]
        listed = next(t for t in client.get("/api/tasks", headers=bob).json()["tasks"] if t["id"] == tid)
        assert "KEYBODY" not in str(listed["activity"])
        last_id = hub.db.one("SELECT id FROM task_events WHERE task_id = ? AND content = 'KEYBODY0610'", (tid,))["id"]
        page = client.get(f"/api/tasks/{tid}/events?after={last_id}", headers=bob)
        if page.status_code == 200:
            assert "KEYBODY" not in page.text
        assert "KEYBODY" not in str(client.get(f"/api/tasks/{tid}/tree", headers=bob).json())
        assert hub._in_key(tid, hub.db.one("SELECT id, content, payload FROM task_events WHERE id = ?", (last_id + 1,)))
    finally:
        ws.__exit__(None, None, None)
