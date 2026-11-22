"""「工作」menu: a colleague's own container on a work server, directories, repos, sessions and pushes."""

import base64
import json
import queue
import threading
import time

from test_hub_flow import client, register, token  # noqa: F401 - pytest fixture

from agent_hub.work import Vault

WORKHOST_HELLO = {
    "type": "hello",
    "connector": {"hostname": "work-01", "os": "linux/amd64", "version": "test", "name": "研发工作服务器 01"},
    "agents": [{"key": "codex", "name": "Codex", "kind": "codex", "isolation": "none", "workhost": True,
                "description": "沙箱执行，适合写测试"}],
    "running": [],
}


class FakeWorkhost:
    """A workhost connector: answers the hub's work_* requests on its own thread and queues tasks."""

    def __init__(self, client, headers):
        self.ws = client.websocket_connect("/ws/connector", headers={"Authorization": "Bearer " + token(client, headers, "connector")})
        self.ws.__enter__()
        self.ws.send_json(WORKHOST_HELLO)
        assert self.ws.receive_json()["type"] == "welcome"
        self.tasks: queue.Queue = queue.Queue()
        self.requests: list[dict] = []
        self.stop = False
        self.base = "main"
        self.unpushed = 1
        # two Codex accounts: c1 nearly spent, c2 fresh (used percent of the 5-hour window)
        self.accounts = [
            {"id": "c1", "kind": "codex", "plan": "pro", "email": "op***@x.com", "usable": True,
             "primary": {"used_percent": 85, "window_minutes": 300, "resets_at": 4102444800}, "secondary": None},
            {"id": "c2", "kind": "codex", "plan": "plus", "email": "op***@y.com", "usable": True,
             "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 4102444800}, "secondary": None},
        ]
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop:
            try:
                msg = self.ws.receive_json()
            except Exception:  # noqa: BLE001 - socket closed at teardown
                return
            if msg["type"] == "task":
                self.tasks.put(msg["task"])
            elif msg["type"] == "set_work_agents":
                self.requests.append(msg)
            elif msg["type"].startswith("work_"):
                self.requests.append(msg)
                reply = {"type": "rpc_result", "request_id": msg["request_id"]}
                if msg["type"] == "work_status":
                    reply["repos"] = [{"repo": "demo", "files": [{"path": "a.py", "status": "M"}] if self.unpushed else [],
                                       "commits": [{"sha": "abc1234", "subject": "改 a.py", "author": "张三"}],
                                       "unpushed": self.unpushed, "base": self.base}]
                if msg["type"] == "work_push":
                    reply["pushed"] = [{"repo": "demo", "pushed": True}]
                if msg["type"] == "work_admin":
                    reply.update(accounts=self.accounts, host={"cpus": 10, "memory_bytes": 8 << 30, "idle_minutes": 30},
                                 containers=[{"user": "u2", "name": "ahw-u2", "state": "running", "running_turns": 0}])
                self.ws.send_json(reply)

    def next_task(self, timeout=5):
        return self.tasks.get(timeout=timeout)

    def finish(self, task, status="completed", result="完成", error="", usage=None):
        for kind in ("task_started", "task_done"):
            msg = {"type": kind, "task_id": task["id"], "turn": task["turn"], "execution_id": task["execution_id"],
                   "status": status, "result": result, "error": error, "session_id": "thread-1"}
            if usage:
                msg["usage"] = usage
            self.ws.send_json(msg)

    def close(self):
        self.stop = True
        self.ws.__exit__(None, None, None)


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    return fn()


def setup_work(client):
    admin, _ = register(client, "admin")
    alice, _ = register(client, "alice")
    host = FakeWorkhost(client, admin)
    server = client.get("/api/work", headers=alice).json()["servers"][0]
    return admin, alice, host, server


def test_vault_round_trip_and_tamper():
    vault = Vault("secret")
    sealed = vault.seal("glpat-abc")
    assert "glpat" not in sealed and vault.open(sealed) == "glpat-abc"
    broken = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    try:
        vault.open(broken)
        raise AssertionError("tampered token must not decrypt")
    except Exception as exc:  # noqa: BLE001
        assert "重新连接" in str(exc)


def test_connect_dirs_repos_sessions_and_push(client):
    admin, alice, host, server = setup_work(client)
    hub = client.app.state.hub
    try:
        assert server["name"] == "研发工作服务器 01" and server["online"] and server["agents"][0]["kind"] == "codex"
        agent_id = server["agents"][0]["id"]
        # offering an agent to the 「工作」menu keeps it an ordinary agent: listed and taking tasks as before
        assert any(a["key"] == "codex" for a in client.get("/api/agents", headers=admin).json()["agents"])
        r = client.post("/api/tasks", json={"agent": "admin/codex", "prompt": "hi"}, headers=admin)
        assert r.status_code == 200, r.text
        ordinary = host.next_task()
        assert "work" not in ordinary  # an ordinary task runs as before, not in a colleague's container
        host.finish(ordinary)

        r = client.post("/api/work/connect", json={"connector_id": server["id"], "git_name": "张三",
                                                    "git_email": "zs@example.com", "token": "glpat-secret"}, headers=alice)
        assert r.status_code == 200, r.text
        profile = r.json()
        assert profile["has_token"] and profile["container"].startswith("u")
        ensure = host.requests[-1]
        assert ensure["type"] == "work_ensure" and ensure["work"]["git_email"] == "zs@example.com"
        assert "glpat" not in str(hub.db.one("SELECT git_token FROM work_profiles"))

        d = client.post("/api/work/dirs", json={"name": "AI平台", "description": "后端一起改"}, headers=alice).json()
        assert d["name"] == "AI平台"
        r = client.post(f"/api/work/dirs/{d['id']}/repos", json={"url": "file:///srv/git/demo.git", "branch": "main"},
                        headers=alice)
        assert r.status_code == 200, r.text
        clone = host.requests[-1]
        assert clone["type"] == "work_clone" and clone["repo"]["name"] == "demo" and clone["work"]["token"] == "glpat-secret"
        assert clone["work"]["folder"].startswith("d-")  # real folder is an id, not the label
        # renaming only changes the label
        client.put(f"/api/work/dirs/{d['id']}", json={"name": "平台", "description": ""}, headers=alice)
        assert hub.db.one("SELECT folder FROM work_dirs")["folder"] == clone["work"]["folder"]
        # the branch new sessions start from can change later; the server checks it on the remote first
        repo_id = hub.db.one("SELECT id FROM work_repos")["id"]
        assert client.put(f"/api/work/repos/{repo_id}", json={"branch": "feature/v1.4.0"}, headers=alice).status_code == 200
        assert host.requests[-1]["type"] == "work_clone" and host.requests[-1]["repo"]["branch"] == "feature/v1.4.0"
        assert client.put(f"/api/work/repos/{repo_id}", json={"branch": "../x"}, headers=alice).status_code == 400
        client.put(f"/api/work/repos/{repo_id}", json={"branch": "main"}, headers=alice)
        assert hub.db.one("SELECT branch FROM work_repos")["branch"] == "main"

        r = client.post("/api/work/sessions", json={"agent_id": agent_id, "dir_id": d["id"], "prompt": "给 a.py 加注释"},
                        headers=alice)
        assert r.status_code == 200, r.text
        session = r.json()
        task = host.next_task()
        assert task["work"]["session"] == f"s-{session['id']}" and task["work"]["branch"] == f"agent/alice/s-{session['id']}"
        assert task["work"]["repos"] == [{"name": "demo", "url": "file:///srv/git/demo.git", "branch": "main"}]
        assert "mcp_token" not in task  # no hub MCP access from inside a container
        # work sessions are not in the ordinary task list, of either party
        assert client.get("/api/tasks", headers=alice).json()["tasks"] == []
        assert all(t["id"] != task["id"] for t in client.get("/api/tasks", headers=admin).json()["tasks"])
        # another colleague cannot see or drive it
        bob, _ = register(client, "bob")
        assert client.get(f"/api/work/sessions/{session['id']}", headers=bob).status_code == 409 or \
            client.get(f"/api/work/sessions/{session['id']}", headers=bob).status_code == 404

        host.base = "feature/v1.4.0"  # the agent moved the session onto another branch during the turn
        host.finish(task)
        wait_for(lambda: client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["status"] == "completed")
        # the directory follows: it shows the branch the session is on, and new sessions start from it
        wait_for(lambda: hub.db.one("SELECT branch FROM work_repos")["branch"] == "feature/v1.4.0")
        host.base = "main"
        r = client.post(f"/api/work/sessions/{session['id']}/continue", json={"message": "再补个测试"}, headers=alice)
        assert r.status_code == 200, r.text
        second = host.next_task()
        assert second["id"] == task["id"] and second["turn"] == 2 and second["session_id"] == "thread-1"
        host.finish(second)
        wait_for(lambda: client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["status"] == "completed")

        changes = client.get(f"/api/work/sessions/{session['id']}/changes", headers=alice).json()
        assert changes["repos"][0]["files"][0]["path"] == "a.py"
        r = client.post(f"/api/work/sessions/{session['id']}/push", json={"repos": ["demo"]}, headers=alice)
        assert r.status_code == 200 and r.json()["pushed"][0]["pushed"]
        push = host.requests[-1]
        assert push["type"] == "work_push" and push["only"] == ["demo"] and push["work"]["token"] == "glpat-secret"
    finally:
        host.close()


def test_direct_push_after_each_completed_turn(client):
    admin, alice, host, server = setup_work(client)
    try:
        client.post("/api/work/connect", json={"connector_id": server["id"], "git_name": "张三",
                                              "git_email": "zs@example.com", "token": "t", "push_mode": "direct"}, headers=alice)
        d = client.post("/api/work/dirs", json={"name": "工具"}, headers=alice).json()
        client.post(f"/api/work/dirs/{d['id']}/repos", json={"url": "https://git.example.com/x/demo.git", "branch": "master"},
                    headers=alice)
        session = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"],
                                                          "prompt": "改一下"}, headers=alice).json()
        assert session["push_mode"] == "direct"
        host.finish(host.next_task())
        assert wait_for(lambda: any(r["type"] == "work_push" for r in host.requests))
        events = wait_for(lambda: [e for e in client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["events"]
                                   if "已自动推送" in e["content"]])
        assert events
    finally:
        host.close()


def test_session_needs_a_repo_and_a_connection(client):
    admin, alice, host, server = setup_work(client)
    try:
        r = client.post("/api/work/dirs", json={"name": "空"}, headers=alice)
        assert r.status_code == 409  # not connected yet
        client.post("/api/work/connect", json={"connector_id": server["id"], "git_name": "张三",
                                              "git_email": "zs@example.com", "token": ""}, headers=alice)
        d = client.post("/api/work/dirs", json={"name": "空"}, headers=alice).json()
        r = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"], "prompt": "x"},
                        headers=alice)
        assert r.status_code == 400 and "仓库" in r.json()["error"]
        r = client.post(f"/api/work/dirs/{d['id']}/repos", json={"url": "ssh://git@x/y.git", "branch": "main"}, headers=alice)
        assert r.status_code == 400
    finally:
        host.close()


def ready_session(client, alice, host, server, push_mode="confirm"):
    client.post("/api/work/connect", json={"connector_id": server["id"], "git_name": "张三",
                                          "git_email": "zs@example.com", "token": "t", "push_mode": push_mode}, headers=alice)
    d = client.post("/api/work/dirs", json={"name": "AI平台"}, headers=alice).json()
    client.post(f"/api/work/dirs/{d['id']}/repos", json={"url": "https://git.example.com/x/demo.git", "branch": "main"},
                headers=alice)
    return d


def test_turns_pick_accounts_by_quota_and_switch_when_one_runs_out(client):
    admin, alice, host, server = setup_work(client)
    hub = client.app.state.hub
    try:
        d = ready_session(client, alice, host, server)
        session = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"],
                                                          "prompt": "改代码"}, headers=alice).json()
        first = host.next_task()
        # the first pick is the account with the most left (c2), not the first listed
        assert first["work"]["account"] == "c2"
        # it runs out mid-turn: the hub marks it, switches to c1 and continues on its own
        host.accounts[1]["primary"]["used_percent"] = 100
        host.accounts[1]["usable"] = False
        host.finish(first, status="failed", result="", error="You've hit your usage limit. Try again later.")
        second = host.next_task()
        assert second["id"] == first["id"] and second["turn"] == 2 and second["work"]["account"] == "c1"
        assert "继续刚才的任务" in second["prompt"]
        events = client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["events"]
        notice = [e["content"] for e in events if "切换额度" in e["content"]]
        assert notice and "c1" not in notice[0] and "c2" not in notice[0]  # colleagues never see account names
        switches = hub.db.all("SELECT from_account, to_account, reason FROM work_switches ORDER BY id")
        assert switches[-1]["from_account"] == "c2" and switches[-1]["to_account"] == "c1"

        # both out now: no switch target, the colleague is told when it comes back
        host.accounts[0]["primary"]["used_percent"] = 100
        host.accounts[0]["usable"] = False
        host.finish(second, status="failed", result="", error="You've hit your usage limit.")
        assert wait_for(lambda: any("暂时没有可切换的额度" in e["content"] for e in
                                    client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["events"]))
        assert host.tasks.empty()
    finally:
        host.close()


def test_confirm_mode_waits_for_the_colleague_after_a_switch(client):
    admin, alice, host, server = setup_work(client)
    try:
        d = ready_session(client, alice, host, server)
        r = client.post(f"/api/work/admin/{server['id']}/settings",
                        json={"auto_switch": True, "threshold": 20, "on_limit": "confirm"}, headers=admin)
        assert r.status_code == 200 and r.json()["on_limit"] == "confirm"
        session = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"],
                                                          "prompt": "改代码"}, headers=alice).json()
        task = host.next_task()
        host.accounts[1]["usable"] = False
        host.finish(task, status="failed", result="", error="usage limit reached")
        assert wait_for(lambda: any("发送「继续」" in e["content"] for e in
                                    client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["events"]))
        time.sleep(0.2)
        assert host.tasks.empty()  # nothing sent until the colleague continues
        client.post(f"/api/work/sessions/{session['id']}/continue", json={"message": "继续"}, headers=alice)
        assert host.next_task()["work"]["account"] == "c1"
    finally:
        host.close()


def test_admin_page_accounts_settings_usage_and_permissions(client):
    admin, alice, host, server = setup_work(client)
    try:
        d = ready_session(client, alice, host, server)
        # only admins maintain work servers
        assert client.get(f"/api/work/admin?connector_id={server['id']}", headers=alice).status_code == 403
        assert client.post(f"/api/work/admin/{server['id']}/current", json={"account_id": "c1"}, headers=alice).status_code == 403
        session = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"],
                                                          "prompt": "改代码"}, headers=alice).json()
        host.finish(host.next_task(), usage={"input_tokens": 1200, "output_tokens": 300})
        status = wait_for(lambda: (lambda s: s if s["usage"] else None)(
            client.get(f"/api/work/admin?connector_id={server['id']}", headers=admin).json()))
        assert status["usage"][0]["total"] == 1500 and status["usage"][0]["today"] == 1500
        assert {a["account_id"] for a in status["accounts"]} == {"c1", "c2"}
        assert [a["account_id"] for a in status["accounts"] if a["current"]] == ["c2"]
        assert status["containers"][0]["username"] == "alice"
        assert status["host"]["cpus"] == 10
        # the admin pins c1: the next turn uses it although c2 has more left
        r = client.post(f"/api/work/admin/{server['id']}/current", json={"account_id": "c1"}, headers=admin)
        assert r.json()["current"]["codex"] == "c1"
        client.post(f"/api/work/admin/{server['id']}/settings", json={"auto_switch": False, "threshold": 20, "on_limit": "auto"},
                    headers=admin)
        client.post(f"/api/work/sessions/{session['id']}/continue", json={"message": "再来"}, headers=alice)
        assert host.next_task()["work"]["account"] == "c1"
        # container actions go to the connector
        assert client.post(f"/api/work/admin/{server['id']}/containers/u2/stop", headers=admin).status_code == 200
        assert host.requests[-1] == {**host.requests[-1], "type": "work_admin_action", "action": "stop", "user": "u2"}
        assert client.post(f"/api/work/admin/{server['id']}/containers/u2/rm", headers=admin).status_code == 400
    finally:
        host.close()


def test_delete_session_asks_before_dropping_unpushed_work(client):
    admin, alice, host, server = setup_work(client)
    try:
        d = ready_session(client, alice, host, server)
        session = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"],
                                                          "prompt": "改代码"}, headers=alice).json()
        r = client.delete(f"/api/work/sessions/{session['id']}", headers=alice)
        assert r.status_code == 409 and "运行" in r.json()["error"]
        host.finish(host.next_task())
        wait_for(lambda: client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["status"] == "completed")
        r = client.delete(f"/api/work/sessions/{session['id']}", headers=alice)
        assert r.status_code == 409 and "未推送" in r.json()["error"]
        assert client.delete(f"/api/work/sessions/{session['id']}?force=1", headers=alice).status_code == 200
        assert host.requests[-1]["type"] == "work_remove_session"
        assert client.get("/api/work", headers=alice).json()["sessions"] == []
        assert client.get("/api/tasks", headers=alice).json()["tasks"] == []  # still out of the ordinary list
    finally:
        host.close()


class FakeGitLab:
    """Answers the GitLab REST calls the 「工作」menu makes (agent_hub.gitlab._request)."""

    def __init__(self):
        self.calls = []
        self.mr_exists = False

    def __call__(self, base, token, method, path, data=None, query=None):
        self.calls.append((base, token, method, path, data, query))
        if token.get("value") == "bad":
            return 401, {"message": "401 Unauthorized"}
        if path == "/projects":
            return 200, [{"path": "demo", "path_with_namespace": "x/demo", "http_url_to_repo": "https://git.example.com/x/demo.git",
                          "default_branch": "main", "description": "演示"}]
        if path == "/projects/x%2Fdemo/merge_requests" and method == "POST":
            if self.mr_exists:
                return 409, {"message": ["Another open merge request already exists for this source branch: !7"]}
            return 201, {"iid": 7, "web_url": "https://git.example.com/x/demo/-/merge_requests/7", "state": "opened",
                         "detailed_merge_status": "checking", "head_pipeline": None}
        if path == "/projects/x%2Fdemo/merge_requests" and method == "GET":
            return 200, [{"iid": 7, "web_url": "https://git.example.com/x/demo/-/merge_requests/7", "state": "opened"}]
        if path == "/projects/x%2Fdemo/merge_requests/7":
            return 200, {"iid": 7, "web_url": "https://git.example.com/x/demo/-/merge_requests/7", "state": "opened",
                         "detailed_merge_status": "mergeable",
                         "head_pipeline": {"status": "success", "web_url": "https://git.example.com/x/demo/-/pipelines/9"}}
        if path == "/user":
            return 200, {"username": "alice.gl", "name": "Alice", "email": "alice@example.com"}
        return 404, {"message": "404 Not Found"}


def test_gitlab_projects_merge_requests_and_pipeline(client, monkeypatch):
    from agent_hub import gitlab
    fake = FakeGitLab()
    monkeypatch.setattr(gitlab, "_request", fake)
    monkeypatch.setenv("AGENT_HUB_GITLAB_URL", "https://git.example.com")
    admin, alice, host, server = setup_work(client)
    try:
        d = ready_session(client, alice, host, server)
        r = client.get("/api/work/gitlab/projects?q=demo", headers=alice)
        assert r.status_code == 200 and r.json()["projects"][0]["url"] == "https://git.example.com/x/demo.git"
        assert fake.calls[-1][1] == {"kind": "pat", "value": "t"}  # the colleague's own token, decrypted
        # Git settings after connecting: a token GitLab rejects is turned away, a blank one keeps the saved token
        settings = {"connector_id": server["id"], "git_name": "Alice 2", "git_email": "a2@example.com"}
        r = client.post("/api/work/connect", json={**settings, "token": "bad"}, headers=alice)
        assert r.status_code == 400 and "令牌" in r.text
        r = client.post("/api/work/connect", json={**settings, "token": ""}, headers=alice)
        assert r.status_code == 200 and r.json()["has_token"] and r.json()["git_name"] == "Alice 2"
        session = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"],
                                                          "prompt": "改代码"}, headers=alice).json()
        host.finish(host.next_task())
        wait_for(lambda: client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["status"] == "completed")
        # unpushed commits: push first
        r = client.post(f"/api/work/sessions/{session['id']}/mr", json={}, headers=alice)
        assert "先推送" in r.json()["results"][0]["skipped"]
        host.unpushed = 0
        r = client.post(f"/api/work/sessions/{session['id']}/mr", json={}, headers=alice)
        mr = r.json()["results"][0]
        assert mr["iid"] == 7 and mr["url"].endswith("/merge_requests/7")
        post = next(c for c in fake.calls if c[2] == "POST")
        assert post[4]["source_branch"] == f"agent/alice/s-{session['id']}" and post[4]["target_branch"] == "main"
        # an MR already open for the branch is reused
        fake.mr_exists = True
        assert client.post(f"/api/work/sessions/{session['id']}/mr", json={}, headers=alice).json()["results"][0]["iid"] == 7
        mrs = client.get(f"/api/work/sessions/{session['id']}/mrs?refresh=1", headers=alice).json()["mrs"]
        assert mrs[0]["pipeline"] == "success" and mrs[0]["merge_status"] == "mergeable"
    finally:
        host.close()


def test_gitlab_oauth_connect_and_auto_server(client, monkeypatch):
    from agent_hub import gitlab
    fake = FakeGitLab()
    monkeypatch.setattr(gitlab, "_request", fake)
    monkeypatch.setenv("AGENT_HUB_GITLAB_URL", "https://git.example.com")
    monkeypatch.setenv("AGENT_HUB_GITLAB_OAUTH_ID", "app")
    monkeypatch.setenv("AGENT_HUB_GITLAB_OAUTH_SECRET", "s3cret")
    monkeypatch.setenv("AGENT_HUB_PUBLIC_URL", "http://hub.local")
    monkeypatch.setattr(gitlab, "_token_call", lambda cfg, form: {
        "access_token": "oauth-access", "refresh_token": "oauth-refresh", "expires_in": 7200})
    admin, alice, host, server = setup_work(client)
    try:
        home = client.get("/api/work", headers=alice).json()
        assert home["oauth"] is True and home["gitlab_pending"] is None
        url = client.post("/api/work/gitlab/authorize", headers=alice).json()["url"]
        assert url.startswith("https://git.example.com/oauth/authorize?") and "redirect_uri=http%3A%2F%2Fhub.local" in url
        state = url.split("state=")[1].split("&")[0]
        # a forged state is refused
        r = client.get("/api/work/gitlab/callback?code=c&state=1.1.bad", follow_redirects=False)
        assert "gitlab_error" in r.headers["location"]
        r = client.get(f"/api/work/gitlab/callback?code=c&state={state}", follow_redirects=False)
        assert r.headers["location"].endswith("gitlab=ok")
        assert client.get("/api/work", headers=alice).json()["gitlab_pending"]["username"] == "alice.gl"
        # connecting without a token uses the authorization; connector 0 = automatic placement
        r = client.post("/api/work/connect", json={"connector_id": 0, "git_name": "Alice", "git_email": "alice@example.com"},
                        headers=alice)
        assert r.status_code == 200, r.text
        assert r.json()["token_kind"] == "oauth" and r.json()["connector_id"] == server["id"]
        client.get("/api/work/gitlab/projects", headers=alice)
        assert fake.calls[-1][1] == {"kind": "oauth", "value": "oauth-access"}
        # close to expiry: refreshed before use
        client.app.state.db.execute("UPDATE work_profiles SET token_expires = 0")
        monkeypatch.setattr(gitlab, "_token_call", lambda cfg, form: {
            "access_token": "oauth-access-2", "refresh_token": "oauth-refresh-2", "expires_in": 7200})
        client.get("/api/work/gitlab/projects", headers=alice)
        assert fake.calls[-1][1] == {"kind": "oauth", "value": "oauth-access-2"}
    finally:
        host.close()


def test_anyone_can_offer_their_machine_and_pick_agents_and_people(client):
    """A colleague (not an admin) registers their own machine: only the agents they pick serve the 「工作」menu,
    the others keep working as ordinary agents, and they decide who may use it."""
    admin, _ = register(client, "admin")
    erin, _ = register(client, "erin")
    frank, _ = register(client, "frank")
    gina, _ = register(client, "gina")
    hello = {**WORKHOST_HELLO, "connector": {**WORKHOST_HELLO["connector"], "name": "erin 的 Mac",
                                             "workhost": {"enabled": True, "docker": True, "image": "agent-hub-work:latest"}},
             "agents": [{"key": "codex", "name": "Codex", "kind": "codex", "isolation": "none", "workhost": True},
                        {"key": "claude", "name": "Claude Code", "kind": "claude", "isolation": "sandbox", "workhost": False}]}
    original = dict(WORKHOST_HELLO)
    WORKHOST_HELLO.clear(); WORKHOST_HELLO.update(hello)
    try:
        host = FakeWorkhost(client, erin)
    finally:
        WORKHOST_HELLO.clear(); WORKHOST_HELLO.update(original)
    try:
        hosts = client.get("/api/work/admin", headers=erin).json()["hosts"]
        mine = hosts[0]
        assert mine["mine"] and mine["workhost"] and mine["docker"] and mine["access"] == "all"
        assert {a["key"]: a["workhost"] for a in mine["agents"]} == {"codex": True, "claude": False}
        # both stay ordinary agents; only codex is also in the 「工作」menu
        keys = [a["key"] for a in client.get("/api/agents", headers=erin).json()["agents"]]
        assert "claude" in keys and "codex" in keys
        servers = client.get("/api/work", headers=frank).json()["servers"]
        assert servers[0]["owner"] == "erin" and [a["key"] for a in servers[0]["agents"]] == ["codex"]
        # the owner maintains it without being an admin; others cannot
        status = client.get(f"/api/work/admin?connector_id={mine['id']}", headers=erin)
        assert status.status_code == 200
        assert client.get(f"/api/work/admin?connector_id={mine['id']}", headers=frank).status_code == 403
        # picking agents goes to the connector
        r = client.post(f"/api/work/hosts/{mine['id']}/agents", json={"keys": ["codex", "claude", "ghost"]}, headers=erin)
        assert r.status_code == 200
        sent = wait_for(lambda: [m for m in host.requests if m.get("type") == "set_work_agents"])
        assert sent and sent[-1]["keys"] == ["codex", "claude"]  # unknown keys dropped
        assert client.post(f"/api/work/hosts/{mine['id']}/agents", json={"keys": ["codex"]}, headers=frank).status_code == 403
        # only frank may use it now
        r = client.post(f"/api/work/hosts/{mine['id']}/access", json={"access": "list", "users": ["frank"]}, headers=erin)
        assert r.status_code == 200 and [u["username"] for u in r.json()["allowed"]] == ["frank"]
        assert client.get("/api/work", headers=gina).json()["servers"] == []
        assert client.get("/api/work", headers=frank).json()["servers"][0]["id"] == mine["id"]
        r = client.post("/api/work/connect", json={"connector_id": mine["id"], "git_name": "G", "git_email": "g@x.com"},
                        headers=gina)
        assert r.status_code == 409
        r = client.post("/api/work/connect", json={"connector_id": 0, "git_name": "F", "git_email": "f@x.com"}, headers=frank)
        assert r.status_code == 200 and r.json()["connector_id"] == mine["id"]
        # access withdrawn later: frank can no longer start sessions there
        client.post(f"/api/work/hosts/{mine['id']}/access", json={"access": "list", "users": []}, headers=erin)
        d = client.post("/api/work/dirs", json={"name": "x"}, headers=frank).json()
        client.post(f"/api/work/dirs/{d['id']}/repos", json={"url": "file:///srv/git/demo.git", "branch": "main"}, headers=frank)
        r = client.post("/api/work/sessions", json={"agent_id": servers[0]["agents"][0]["id"], "dir_id": d["id"], "prompt": "x"},
                        headers=frank)
        assert r.status_code == 403
    finally:
        host.close()


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()


def test_images_attached_to_messages_reach_the_agent(client):
    admin, alice, host, server = setup_work(client)
    try:
        d = ready_session(client, alice, host, server)
        r = client.post("/api/work/sessions", json={"agent_id": server["agents"][0]["id"], "dir_id": d["id"], "prompt": "看图",
                                                    "images": [{"name": "shot", "data": "data:image/png;base64," + PNG}]},
                        headers=alice)
        assert r.status_code == 200, r.text
        session = r.json()
        first = host.next_task()
        assert first["images"] == [{"name": "shot.png", "data": PNG}]
        assert "保密要求" not in first["prompt"]  # the colleague's own container: no owner to keep things from
        # shown with the message, marked as the person's input, and downloadable by them
        events = client.get(f"/api/tasks/{session['task_id']}", headers=alice).json()["events"]
        image = next(e for e in events if e["kind"] == "image")
        assert json.loads(image["content"])["input"] is True
        assert client.get(f"/api/tasks/{session['task_id']}/files/{image['id']}", headers=alice).content.startswith(b"\x89PNG")
        host.finish(first)
        wait_for(lambda: client.get(f"/api/work/sessions/{session['id']}", headers=alice).json()["status"] == "completed")
        # a picture alone is a message; the next turn carries only its own images
        r = client.post(f"/api/work/sessions/{session['id']}/continue", json={"message": "", "images": [{"name": "b.png", "data": PNG}]},
                        headers=alice)
        assert r.status_code == 200, r.text
        second = host.next_task()
        assert second["prompt"] == "（见附图）" and [i["name"] for i in second["images"]] == ["b.png"]
        host.finish(second)
        # not an image, too many: refused
        bad = client.post(f"/api/work/sessions/{session['id']}/continue",
                          json={"message": "x", "images": [{"name": "a.png", "data": base64.b64encode(b"hello").decode()}]}, headers=alice)
        assert bad.status_code == 400 and "不是支持的图片" in bad.text
        many = client.post(f"/api/work/sessions/{session['id']}/continue",
                           json={"message": "x", "images": [{"name": "a.png", "data": PNG}] * 7}, headers=alice)
        assert many.status_code == 400 and "最多" in many.text
    finally:
        host.close()
