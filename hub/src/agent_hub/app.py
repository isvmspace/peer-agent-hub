"""HTTP host: console API, connector WebSocket, MCP endpoint and the built console."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import logging
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__
from .auth import (
    bearer,
    create_api_token,
    decode_session,
    hash_password,
    issue_session,
    resolve_api_token,
    verify_password,
)
from .db import Database
from .hub import ACTIVE, FEATURES, IMAGE_SUFFIXES, Connection, Hub, HubError, public_code_ref
from .mcp_tools import McpAuthGate, build_mcp
from .social import Social
from .releases import MAX_ARTIFACT_BYTES, PLATFORMS, Releases, filename_for
from .storage import LocalStorage, build_storage
from .workflows import Workflows, templates as workflow_templates
from .autoreply import AutoReply
from .work import Work

LOGGER = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


class Settings:
    def __init__(self):
        self.data_dir = Path(os.getenv("AGENT_HUB_DATA_DIR", "var"))
        self.allow_register = os.getenv("AGENT_HUB_ALLOW_REGISTER", "true").lower() != "false"
        self.download_dir = Path(os.getenv("AGENT_HUB_DOWNLOAD_DIR", str(self.data_dir / "downloads")))
        self.secret = os.getenv("AGENT_HUB_SECRET") or self._persistent_secret()

    def _persistent_secret(self) -> str:
        path = self.data_dir / "secret.key"
        if path.exists():
            return path.read_text().strip()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48)
        path.write_text(value)
        path.chmod(0o600)
        return value


def text(data: dict, key: str) -> str:
    """A string field; a missing or null value is empty, any other non-string is rejected."""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HubError(f"{key} 必须是字符串")
    return value


def flag(data: dict, key: str) -> bool:
    """A JSON boolean field; a missing or null value is false. Strings like "false" are rejected, not read as true."""
    value = data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise HubError(f"{key} 必须是 true 或 false")
    return value


def number(value, name: str, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HubError(f"{name} 必须是整数") from None


def ok(data=None, status: int = 200) -> JSONResponse:
    return JSONResponse(data if data is not None else {"ok": True}, status_code=status)


def fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def create_app(settings: Settings | None = None, db: Database | None = None) -> Starlette:
    settings = settings or Settings()
    db = db or Database(settings.data_dir / "agent-hub.db")
    hub = Hub(db)
    hub.attachments_dir = settings.data_dir / "attachments"
    social = Social(db, hub)
    workflows = Workflows(db, hub)
    autoreply = AutoReply(db, hub, social)
    work = Work(db, hub, settings.secret)
    storage =build_storage(settings.data_dir, settings.secret)
    releases = Releases(db, storage)
    hub.update_check = releases.check
    mcp = build_mcp(hub, social)
    mcp_app = mcp.http_app(path="/", stateless_http=True)

    def session_user(request: Request) -> dict:
        user_id = decode_session(settings.secret, bearer(request.headers))
        user = hub.get_user(user_id) if user_id else None
        if not user or user["disabled"]:
            raise HubError("登录已失效，请重新登录", 401)
        return user

    def endpoint(fn, public: bool = False):
        async def handler(request: Request):
            try:
                if public:
                    return await fn(request)
                return await fn(request, session_user(request))
            except HubError as exc:
                return fail(str(exc), exc.status)

        return handler

    # ---- auth ---------------------------------------------------------

    async def register(request: Request):
        data = await body(request)
        first = db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0
        if not settings.allow_register and not first:
            raise HubError("站点已关闭自助注册，请联系管理员", 403)
        username = text(data, "username").strip()
        password = text(data, "password")
        if not username.replace("-", "").replace("_", "").replace(".", "").isalnum() or len(username) > 32:
            raise HubError("用户名只能包含字母、数字、-、_、.，且不超过 32 位")
        if len(password) < 6:
            raise HubError("密码至少 6 位")
        if db.one("SELECT id FROM users WHERE username = ?", (username,)):
            raise HubError("用户名已存在")
        is_admin = db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0
        user_id = db.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, str(data.get("display_name") or username)[:40], hash_password(password), int(is_admin), time.time()),
        )
        db.audit(user_id, "user.register")
        return ok({"token": issue_session(settings.secret, user_id), "user": hub.get_user(user_id)})

    async def login(request: Request):
        data = await body(request)
        row = db.one("SELECT * FROM users WHERE username = ?", (text(data, "username").strip(),))
        if not row or not verify_password(text(data, "password"), row["password_hash"]):
            raise HubError("用户名或密码错误", 401)
        if row["disabled"]:
            raise HubError("账号已被管理员停用", 403)
        db.audit(row["id"], "user.login")
        return ok({"token": issue_session(settings.secret, row["id"]), "user": hub.get_user(row["id"])})

    async def me(request: Request, user: dict):
        return ok({"user": user, "allow_register": settings.allow_register})

    async def update_profile(request: Request, user: dict):
        data = await body(request)
        name = text(data, "display_name").strip()
        if not name:
            raise HubError("显示名称不能为空")
        if len(name) > 40:
            raise HubError("显示名称最多 40 个字")
        db.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, user["id"]))
        db.audit(user["id"], "user.profile", display_name=name)
        return ok({"user": hub.get_user(user["id"])})

    async def change_password(request: Request, user: dict):
        data = await body(request)
        row = db.one("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
        if not verify_password(text(data, "old_password"), row["password_hash"]):
            raise HubError("原密码错误")
        if len(text(data, "new_password")) < 6:
            raise HubError("新密码至少 6 位")
        if data["new_password"] == data.get("old_password"):
            raise HubError("新密码不能和原密码相同")
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(data["new_password"]), user["id"]))
        db.audit(user["id"], "user.password")
        return ok()

    async def auth_options(request: Request):
        needs_setup = db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0
        return ok({"allow_register": settings.allow_register or needs_setup, "needs_setup": needs_setup, "version": __version__})

    async def users(request: Request, user: dict):
        return ok({"users": db.all(
            "SELECT id, username, display_name, is_admin FROM users WHERE disabled = 0 ORDER BY username"
        )})

    # ---- admin ---------------------------------------------------------

    def require_superuser(user: dict) -> None:
        if not user["is_admin"]:
            raise HubError("仅管理员可以操作", 403)

    async def admin_hosts(request: Request, user: dict):
        """Every registered computer with its owner, addresses and agents."""
        require_superuser(user)
        rows = db.all(
            "SELECT c.id, c.name, c.hostname, c.os, c.version, c.online, c.last_seen_at, c.connected_at, c.remote_ip, "
            "c.local_ips, u.id AS user_id, u.username, u.display_name, t.name AS token_name, t.revoked "
            "FROM connectors c JOIN users u ON u.id = c.user_id JOIN api_tokens t ON t.id = c.token_id "
            "ORDER BY c.online DESC, c.last_seen_at DESC"
        )
        for row in rows:
            row["local_ips"] = json.loads(row["local_ips"] or "[]")
            row["agents"] = db.all(
                "SELECT key, name, kind, online, isolation FROM agents WHERE connector_id = ? ORDER BY key", (row["id"],)
            )
        return ok({"hosts": rows})

    async def admin_users(request: Request, user: dict):
        require_superuser(user)
        rows = db.all(
            "SELECT u.id, u.username, u.display_name, u.is_admin, u.disabled, u.created_at, "
            "(SELECT COUNT(*) FROM agents a WHERE a.user_id = u.id) AS agents, "
            "(SELECT COUNT(*) FROM connectors c WHERE c.user_id = u.id AND c.online = 1) AS connectors_online, "
            "(SELECT COUNT(*) FROM tasks t WHERE t.from_user_id = u.id) AS tasks_sent, "
            "(SELECT MAX(created_at) FROM audit_logs l WHERE l.user_id = u.id AND l.action = 'user.login') AS last_login "
            "FROM users u ORDER BY u.id"
        )
        return ok({"users": rows})

    async def admin_user_item(request: Request, user: dict):
        require_superuser(user)
        target_id = int(request.path_params["user_id"])
        target = hub.get_user(target_id)
        if not target:
            raise HubError("用户不存在", 404)
        data = await body(request)
        fields = {k: int(flag(data, k)) for k in ("is_admin", "disabled") if k in data}
        if target_id == user["id"] and (fields.get("is_admin") == 0 or fields.get("disabled") == 1):
            raise HubError("不能取消自己的管理员身份或停用自己")
        for key, value in fields.items():
            db.execute(f"UPDATE users SET {key} = ? WHERE id = ?", (value, target_id))
        db.audit(user["id"], "admin.user", target=target["username"], **fields)
        if fields.get("disabled"):
            hub.fail_waiting_tasks_of(target_id)
            # a disabled account's open connector sessions end now; reconnecting fails at the handshake
            for row in db.all("SELECT id FROM connectors WHERE user_id = ?", (target_id,)):
                conn = hub.connections.get(row["id"])
                if conn:
                    hub.connector_offline(row["id"])
                    try:
                        await conn.close()
                    except Exception:  # noqa: BLE001
                        pass
        return ok({"user": hub.get_user(target_id)})

    # ---- overview / agents / connectors ------------------------------

    def overview_data(user: dict) -> dict:
        agents = hub.list_agents()
        mine = hub.list_tasks(user, "all", limit=10, recent=True)
        # asked for on their own: an old task still running must not fall out of a window of recent ones
        active = hub.list_tasks(user, "all", status="active", limit=500)
        return (
            {
                "active_tasks": active,
                "agents_total": len(agents),
                "agents_online": sum(1 for a in agents if a["online"]),
                "my_agents": sum(1 for a in agents if a["user_id"] == user["id"]),
                "pending_approval": len(hub.list_tasks(user, "approval")),
                "running": sum(1 for t in active if t["status"] in ("queued", "dispatched", "running")),
                "recent_tasks": mine[:10],
            }
        )

    async def overview(request: Request, user: dict):
        return ok(overview_data(user))

    async def overview_stream(request: Request, user: dict):
        def snapshot():
            yield "overview", {**overview_data(user), "agents": hub.list_agents(viewer=user)}

        # many task events can arrive at once; one picture per second is plenty for the dashboard
        return sse(request, "tasks", snapshot, user, min_interval=1.0)

    async def agents(request: Request, user: dict):
        return ok({"agents": hub.list_agents(request.query_params.get("q", ""), viewer=user)})

    async def connectors(request: Request, user: dict):
        rows = db.all(
            "SELECT c.*, t.prefix, t.revoked FROM connectors c JOIN api_tokens t ON t.id = c.token_id "
            "WHERE c.user_id = ? ORDER BY c.id DESC",
            (user["id"],),
        )
        for row in rows:
            row["agents"] = db.all("SELECT key, name, kind, online FROM agents WHERE connector_id = ?", (row["id"],))
        return ok({"connectors": rows})

    async def delete_agent(request: Request, user: dict):
        agent_id = int(request.path_params["agent_id"])
        agent = db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if not agent or (agent["user_id"] != user["id"] and not user["is_admin"]):
            raise HubError("智能体不存在或无权操作", 404)
        if db.one("SELECT id FROM tasks WHERE agent_id = ? LIMIT 1", (agent_id,)):
            raise HubError("该智能体已有任务记录，只能从连接器配置中移除（会显示为离线）")
        db.execute("DELETE FROM approval_rules WHERE agent_id = ?", (agent_id,))
        db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        db.audit(user["id"], "agent.delete", agent_id=agent_id)
        return ok()

    # ---- tokens --------------------------------------------------------

    async def tokens(request: Request, user: dict):
        if request.method == "POST":
            data = await body(request)
            kind = data.get("kind")
            if kind not in ("personal", "connector"):
                raise HubError("kind 必须是 personal 或 connector")
            name = str(data.get("name") or kind).strip()[:60]
            token_id, token = create_api_token(db, user["id"], kind, name)
            db.audit(user["id"], "token.create", token_id=token_id, kind=kind)
            return ok({"id": token_id, "token": token})
        rows = db.all(
            "SELECT id, kind, name, prefix, revoked, created_at, last_used_at FROM api_tokens "
            "WHERE user_id = ? AND kind != 'task' ORDER BY id DESC",
            (user["id"],),
        )
        return ok({"tokens": rows})

    async def revoke_token(request: Request, user: dict):
        token_id = int(request.path_params["token_id"])
        row = db.one("SELECT * FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user["id"]))
        if not row:
            raise HubError("令牌不存在", 404)
        db.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ?", (token_id,))
        connector = db.one("SELECT id FROM connectors WHERE token_id = ?", (token_id,))
        if connector and connector["id"] in hub.connections:
            conn = hub.connections[connector["id"]]
            try:
                await conn.send({"type": "revoked"})
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
            hub.connector_offline(connector["id"])
        db.audit(user["id"], "token.revoke", token_id=token_id)
        return ok()

    # ---- tasks ---------------------------------------------------------

    async def tasks(request: Request, user: dict):
        if request.method == "POST":
            data = await body(request)
            task = await hub.create_task(
                user, data.get("agent") or data.get("agent_id") or "", text(data, "prompt"), text(data, "title"),
                workspace=text(data, "workspace"), images=data.get("images"),
            )
            return ok({"task": hub.present_task(user, task)})
        q = request.query_params
        return ok({"tasks": hub.list_tasks(user, q.get("box", "all"), q.get("status", ""), number(q.get("limit"), "limit", 100), q.get("q", ""))})

    async def task_precheck(request: Request, user: dict):
        data = await body(request)
        return ok(hub.precheck(user, data.get("agent") or data.get("agent_id") or "", text(data, "workspace")))

    async def task_tree(request: Request, user: dict):
        return ok(hub.task_tree(user, int(request.path_params["task_id"])))

    async def task_artifacts(request: Request, user: dict):
        return ok({"artifacts": hub.artifacts(user, int(request.path_params["task_id"]))})

    async def artifact_content(request: Request, user: dict):
        artifact = hub.visible_artifact(user, int(request.path_params["artifact_id"]))
        data = hub.artifact_bytes(artifact)
        media = artifact["mime"] or mimetypes.guess_type(artifact["name"])[0] or "application/octet-stream"
        disposition = "inline" if artifact["kind"] == "image" else "attachment"
        return Response(data, media_type=media, headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(artifact['name'])}",
            "X-Content-SHA256": artifact["sha256"], "Cache-Control": "private, max-age=3600",
        })

    async def artifact_review(request: Request, user: dict):
        data = await body(request)
        artifact = await hub.review_artifact(user, int(request.path_params["artifact_id"]), text(data, "verdict"), text(data, "note"))
        return ok({"artifact": artifact})

    async def workflow_list(request: Request, user: dict):
        if request.method == "POST":
            data = await body(request)
            run = await workflows.start(user, text(data, "template"), text(data, "title"),
                                        text(data, "input"), data.get("steps") or [])
            return ok({"run": run})
        return ok({"runs": workflows.runs(user), "templates": workflow_templates()})

    async def workflow_item(request: Request, user: dict):
        return ok({"run": workflows.run(user, int(request.path_params["run_id"]))})

    async def workflow_action(request: Request, user: dict):
        run_id = int(request.path_params["run_id"])
        action = request.path_params["action"]
        data = await body(request)
        if action == "continue":
            run = await workflows.continue_step(user, run_id, int(data.get("step", -1)))
        elif action == "cancel":
            run = await workflows.cancel(user, run_id)
        else:
            raise HubError("未知操作", 404)
        return ok({"run": run})

    def with_request(task: dict) -> dict:
        """The first prompt of a group task carries the whole group context; the console shows the sender's own message."""
        if task.get("group_message_id"):
            task["request"] = db.one(
                "SELECT m.content, m.created_at, g.name AS group_name FROM group_messages m "
                "JOIN chat_groups g ON g.id = m.group_id WHERE m.id = ?",
                (task["group_message_id"],),
            )
        return task

    def with_rights(user: dict, task: dict) -> dict:
        party = hub.is_party(user, task)
        share = None if party else hub.share_of(user, task["id"])
        task["can_approve"] = task["status"] == "pending_approval" and user["id"] == task["owner_user_id"]
        task["can_share"] = party and not task.get("group_id")
        task["can_continue"] = party or bool(share and share["can_continue"])
        task["shared_by"] = hub.get_user(share["shared_by"])["username"] if share else ""
        return task

    async def task_detail(request: Request, user: dict):
        task = hub.visible_task(user, int(request.path_params["task_id"]))
        after = number(request.query_params.get("after"), "after")
        with_rights(user, task)
        children = [
            c if hub.can_view(user, hub.get_task(c["id"])) else {**c, "title": ""}
            for c in db.all("SELECT id, title, status FROM tasks WHERE parent_task_id = ? ORDER BY id", (task["id"],))
        ]
        task["wait"] = hub.wait_reason(task)
        task["code_ref"] = json.loads(task.get("code_ref") or "{}")
        if not hub.sees_process(user, task):
            task["code_ref"] = public_code_ref(task["code_ref"])
        with_request(task)
        turn_from = number(request.query_params.get("turn_from"), "turn_from")
        turn_to = number(request.query_params.get("turn_to"), "turn_to")
        if turn_to:  # an earlier slice of turns for a chat that loads its history backwards
            return ok({"events": hub.events(task["id"], 0, 100_000, viewer=user, turn_from=turn_from, turn_to=turn_to)})
        return ok({"task": hub.present_task(user, task), "events": hub.events(task["id"], after, viewer=user), "children": children})

    async def task_action(request: Request, user: dict):
        task_id = int(request.path_params["task_id"])
        action = request.path_params["action"]
        data = await body(request)
        if action == "approve":
            task = await hub.approve(user, task_id, True)
        elif action == "reject":
            task = await hub.approve(user, task_id, False)
        elif action == "cancel":
            task = await hub.cancel(user, task_id, tree=flag(data, "tree"))
        elif action == "follow-up":
            task = await hub.follow_up(user, task_id, text(data, "message"), data.get("images"))
        else:
            raise HubError("未知操作", 404)
        return ok({"task": hub.present_task(user, task)})

    async def task_shares(request: Request, user: dict):
        task_id = int(request.path_params["task_id"])
        if request.method == "POST":
            data = await body(request)
            hub.share_task(user, task_id, data.get("user") or "", flag(data, "can_continue"), flag(data, "auto_approve"))
        elif request.method == "DELETE":
            hub.unshare_task(user, task_id, int(request.path_params["user_id"]))
            if not hub.can_view(user, hub.get_task(task_id)):
                return ok({"shares": []})  # someone leaving a share no longer sees it
        return ok({"shares": hub.shares(user, task_id)})

    async def local_sessions(request: Request, user: dict):
        agent_id = int(request.path_params["agent_id"])
        if request.method == "POST":
            data = await body(request)
            task = await hub.import_local_session(user, agent_id, text(data, "session_id"), text(data, "title"))
            return ok({"task": task})
        return ok({"sessions": await hub.local_sessions(user, agent_id)})

    async def task_file(request: Request, user: dict):
        task_id = int(request.path_params["task_id"])
        hub.visible_task(user, task_id)
        row = db.one(
            "SELECT content FROM task_events WHERE id = ? AND task_id = ? AND kind IN ('image', 'file')",
            (int(request.path_params["event_id"]), task_id),
        )
        meta = json.loads(row["content"]) if row else {}
        path = hub.attachment_path(task_id, str(meta.get("file", ""))) if meta else None
        if not path:
            raise HubError("文件不存在", 404)
        return FileResponse(path, filename=meta.get("name") or path.name,
                            content_disposition_type="inline" if row and path.suffix in IMAGE_SUFFIXES else "attachment",
                            headers={"Cache-Control": "private, max-age=86400"})

    # ---- live streams (SSE) -------------------------------------------

    def sse(request: Request, channel: str, snapshot, user: dict, allowed=lambda: None, min_interval: float = 0.0):
        """Push `snapshot()` whenever the channel fires; heartbeat keeps proxies from closing the stream.

        Access is checked again before every push (`allowed` raises HubError once it is gone, e.g. a share
        was revoked or the user left a group), so an open stream never outlives the permission it started with.
        """

        def still_allowed() -> bool:
            current = hub.get_user(user["id"])
            if not current or current["disabled"] or decode_session(settings.secret, bearer(request.headers)) != user["id"]:
                return False  # also ends the stream once the login itself expires
            user.update(current)  # a role change (e.g. admin revoked) applies to the open stream too
            try:
                allowed()
            except HubError:
                return False
            return True

        async def stream():
            wake = hub.subscribe(channel)
            last = 0.0
            try:
                while True:
                    # at most one push per min_interval, but the latest state always goes out at the end of the window
                    pause = last + min_interval - time.monotonic()
                    if pause > 0:
                        await asyncio.sleep(pause)
                    last = time.monotonic()
                    wake.clear()
                    if not still_allowed():
                        yield f"event: denied\ndata: {json.dumps({'error': '你已没有权限查看此内容'}, ensure_ascii=False)}\n\n"
                        return
                    for name, payload in snapshot():
                        yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": ping\n\n"
            finally:
                hub.unsubscribe(channel, wake)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- 「工作」menu -----------------------------------------------------

    def whole(data: dict, key: str) -> int:
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise HubError(f"{key} 必须是整数")
        return value

    async def work_home(request: Request, user: dict):
        return ok({"servers": work.servers(user), "profile": work.profile(user), "dirs": work.dirs(user) if work.profile(user) else [],
                   "sessions": work.sessions(user) if work.profile(user) else [], "oauth": work.oauth_enabled(),
                   "gitlab_pending": work.gitlab_pending(user)})

    async def work_connect(request: Request, user: dict):
        data = await body(request)
        return ok(await work.connect(user, number(data.get("connector_id"), "connector_id"), text(data, "git_name"), text(data, "git_email"),
                                     text(data, "token"), text(data, "push_mode") or "confirm"))

    async def work_dirs(request: Request, user: dict):
        data = await body(request)
        return ok(work.create_dir(user, text(data, "name"), text(data, "description")))

    async def work_dir_item(request: Request, user: dict):
        data = await body(request)
        return ok(work.update_dir(user, int(request.path_params["dir_id"]), text(data, "name"), text(data, "description")))

    async def work_repos(request: Request, user: dict):
        data = await body(request)
        return ok(await work.add_repo(user, int(request.path_params["dir_id"]), text(data, "url"), text(data, "branch"),
                                      text(data, "name")))

    async def collab_machines(request: Request, user: dict):
        return ok({"machines": hub.collab_machines(user)})

    async def collab_machine(request: Request, user: dict):
        data = await body(request)
        hub.set_collab(user, int(request.path_params["cid"]), bool(data.get("enabled")))
        return ok()

    def _days(q) -> float:
        try:
            return min(max(float(q.get("days") or 3), 0.04), 90)
        except ValueError:
            raise HubError("days 需要是数字") from None

    async def collab_overview(request: Request, user: dict):
        q = request.query_params
        return ok(await hub.collab_query(user, int(request.path_params["cid"]), {"type": "collab_overview", "days": _days(q)}))

    async def collab_discussion(request: Request, user: dict):
        q = request.query_params
        message = {"type": "collab_discussion", "key": q.get("key", "")[:300], "days": _days(q)}
        return ok(await hub.collab_query(user, int(request.path_params["cid"]), message))

    async def collab_session(request: Request, user: dict):
        q = request.query_params
        message = {"type": "collab_session", "key": q.get("key", "")[:300], "days": _days(q)}
        if q.get("start", "").isdigit():
            message["start"] = int(q["start"])
        return ok(await hub.collab_query(user, int(request.path_params["cid"]), message))

    async def work_repo_item(request: Request, user: dict):
        if request.method == "PUT":
            data = await body(request)
            await work.set_repo_branch(user, int(request.path_params["repo_id"]), text(data, "branch"))
            return ok()
        work.remove_repo(user, int(request.path_params["repo_id"]))
        return ok()

    async def work_sessions(request: Request, user: dict):
        if request.method == "GET":
            return ok({"sessions": work.sessions(user)})
        data = await body(request)
        return ok(await work.create_session(user, whole(data, "agent_id"), whole(data, "dir_id"), text(data, "prompt"),
                                            text(data, "push_mode"), data.get("images")))

    async def work_session_item(request: Request, user: dict):
        if request.method == "DELETE":
            await work.delete_session(user, int(request.path_params["session_id"]),
                                      request.query_params.get("force") == "1")
            return ok()
        return ok(work.session(user, int(request.path_params["session_id"]),
                               number(request.query_params.get("after"), "after")))

    async def work_session_mrs(request: Request, user: dict):
        return ok({"mrs": await work.mrs(user, int(request.path_params["session_id"]),
                                         request.query_params.get("refresh") == "1")})

    async def work_gitlab_projects(request: Request, user: dict):
        return ok({"projects": await work.projects(user, request.query_params.get("q", ""))})

    async def work_gitlab_authorize(request: Request, user: dict):
        return ok({"url": work.oauth_start(user, settings.secret)})

    async def work_gitlab_callback(request: Request):
        q = request.query_params
        try:
            if q.get("error"):
                raise HubError(q.get("error_description") or q.get("error"))
            await work.oauth_callback(q.get("code", ""), q.get("state", ""), settings.secret)
            return RedirectResponse("/console/#/work?gitlab=ok", status_code=302)
        except HubError as exc:
            from urllib.parse import quote
            return RedirectResponse(f"/console/#/work?gitlab_error={quote(str(exc))}", status_code=302)

    async def work_admin(request: Request, user: dict):
        connector_id = number(request.query_params.get("connector_id"), "connector_id")
        if not connector_id:
            return ok({"servers": [s for s in work.servers() if user.get("is_admin") or s["owner_id"] == user["id"]],
                       "hosts": work.hosts(user)})
        return ok(await work.admin_status(user, connector_id))

    async def work_host_agents(request: Request, user: dict):
        data = await body(request)
        keys = data.get("keys") or []
        if not isinstance(keys, list):
            raise HubError("keys 必须是智能体标识列表")
        await work.set_work_agents(user, int(request.path_params["connector_id"]), keys)
        return ok()

    async def work_host_access(request: Request, user: dict):
        data = await body(request)
        users = data.get("users") or []
        if not isinstance(users, list):
            raise HubError("users 必须是用户名列表")
        return ok(work.set_access(user, int(request.path_params["connector_id"]), text(data, "access"), users))

    async def work_admin_settings(request: Request, user: dict):
        data = await body(request)
        return ok(work.update_settings(user, int(request.path_params["connector_id"]), flag(data, "auto_switch"),
                                       whole(data, "threshold"), text(data, "on_limit")))

    async def work_admin_current(request: Request, user: dict):
        data = await body(request)
        return ok(work.set_current(user, int(request.path_params["connector_id"]), text(data, "account_id")))

    async def work_admin_container(request: Request, user: dict):
        await work.container_action(user, int(request.path_params["connector_id"]), request.path_params["user"],
                                    request.path_params["action"])
        return ok()

    async def work_session_changes(request: Request, user: dict):
        return ok(await work.changes(user, int(request.path_params["session_id"])))

    async def work_session_action(request: Request, user: dict):
        session_id, action = int(request.path_params["session_id"]), request.path_params["action"]
        data = await body(request)
        if action == "continue":
            return ok(await work.continue_session(user, session_id, text(data, "message"), data.get("images")))
        if action == "cancel":
            return ok(await work.cancel_session(user, session_id))
        if action == "push":
            repos = data.get("repos") or []
            if not isinstance(repos, list):
                raise HubError("repos 必须是仓库名列表")
            return ok(await work.push(user, session_id, repos))
        if action == "mode":
            return ok(work.set_push_mode(user, session_id, text(data, "push_mode")))
        if action == "mr":
            repos = data.get("repos") or []
            if not isinstance(repos, list):
                raise HubError("repos 必须是仓库名列表")
            return ok({"results": await work.create_mrs(user, session_id, repos)})
        raise HubError(f"未知操作 {action}", 404)

    async def work_session_stream(request: Request, user: dict):
        session_id = int(request.path_params["session_id"])
        task_id = work.session(user, session_id, 1 << 62)["task_id"]
        cursor = {"after": number(request.query_params.get("after"), "after")}

        def snapshot():
            events = []
            while True:
                page = hub.events(task_id, cursor["after"], viewer=user)
                events += page
                if page:
                    cursor["after"] = page[-1]["id"]
                if len(page) < 1000:
                    break
            row = work.session(user, session_id, 1 << 62)
            row.pop("events", None)
            yield "session", {"session": row, "events": events}

        return sse(request, f"task:{task_id}", snapshot, user, lambda: work.session(user, session_id, 1 << 62))

    async def task_stream(request: Request, user: dict):
        task_id = int(request.path_params["task_id"])
        hub.visible_task(user, task_id)
        cursor = {"after": number(request.query_params.get("after"), "after")}
        # ?turns=n: a fresh chat view starts with the latest n turns and loads older ones on demand
        recent = number(request.query_params.get("turns"), "turns")
        first_turn = max(1, hub.get_task(task_id)["turn"] - recent + 1) if recent and not cursor["after"] else 0
        first_turn = number(request.query_params.get("from_turn"), "from_turn", first_turn)

        def snapshot():
            task = with_rights(user, with_request(hub.get_task(task_id)))
            task["wait"] = hub.wait_reason(task)
            task["code_ref"] = json.loads(task.get("code_ref") or "{}")
            if not hub.sees_process(user, task):
                task["code_ref"] = public_code_ref(task["code_ref"])
            events = []
            while True:  # catch up fully, so a long conversation never stops at its first page
                page = hub.events(task_id, cursor["after"], viewer=user, turn_from=first_turn)
                events += page
                if page:
                    cursor["after"] = page[-1]["id"]
                if len(page) < 1000:
                    break
            yield "task", {"task": hub.present_task(user, task), "events": events, "first_turn": first_turn or 1}

        return sse(request, f"task:{task_id}", snapshot, user, lambda: hub.visible_task(user, task_id))

    # ---- workspaces / contacts / groups --------------------------------

    async def agent_visibility(request: Request, user: dict):
        data = await body(request)
        hub.set_hidden(user, int(request.path_params["agent_id"]), flag(data, "hidden"))
        return ok()

    async def agent_workspaces(request: Request, user: dict):
        data = await body(request)
        await hub.set_workspaces(user, int(request.path_params["agent_id"]), data.get("workspaces") or [])
        # the connector validates the folders and answers with its updated agent list
        for _ in range(20):
            await asyncio.sleep(0.15)
            agent = next((a for a in hub.list_agents() if a["id"] == int(request.path_params["agent_id"])), None)
            if agent and [w["path"] for w in agent["workspaces"]] == [
                str(w.get("path", "")).strip() for w in data.get("workspaces") or [] if str(w.get("path", "")).strip()
            ]:
                return ok({"agent": agent})
        agent = next((a for a in hub.list_agents() if a["id"] == int(request.path_params["agent_id"])), None)
        return ok({"agent": agent, "warning": "连接器未接受全部路径（目录不存在或不是绝对路径），请查看当前生效的工作区"})

    async def friends(request: Request, user: dict):
        if request.method == "POST":
            data = await body(request)
            return ok({"friendship": social.request_friend(user, data.get("user") or "", text(data, "note"))})
        return ok(social.friends(user))

    async def friend_action(request: Request, user: dict):
        friendship_id = int(request.path_params["friendship_id"])
        if request.method == "DELETE":
            social.remove_friend(user, friendship_id)
            return ok()
        action = request.path_params.get("action")
        if action not in ("accept", "reject"):
            raise HubError("未知操作", 404)
        social.answer_friend(user, friendship_id, action == "accept")
        return ok()

    async def direct_messages(request: Request, user: dict):
        target = int(request.path_params["user_id"])
        if request.method == "POST":
            data = await body(request)
            return ok(social.send_direct(user, target, text(data, "content")))
        return ok({"messages": social.direct_messages(user, target, number(request.query_params.get("after"), "after"),
                                                      number(request.query_params.get("before"), "before"))})

    async def auto_replies(request: Request, user: dict):
        if request.method == "PUT":
            try:
                data = await request.json()
            except (json.JSONDecodeError, ValueError):
                data = None
            if not isinstance(data, dict):
                raise HubError("请求体必须是 JSON 对象")
            whole = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
            friend_id, agent_id, workspace = data.get("friend_id", 0), data.get("agent_id"), data.get("workspace", "")
            if not whole(friend_id) or friend_id < 0:
                raise HubError("friend_id 必须是整数（0 表示默认设置）")
            if agent_id is not None and not whole(agent_id):
                raise HubError("agent_id 必须是整数或 null")
            if not isinstance(workspace, str):
                raise HubError("workspace 必须是工作区 ID 字符串")
            if not isinstance(data.get("inherit", False), bool):
                raise HubError("inherit 必须是 true 或 false")
            return ok(autoreply.configure(user, friend_id, agent_id, workspace, data.get("inherit", False)))
        return ok(autoreply.settings(user))

    async def direct_stream(request: Request, user: dict):
        target = int(request.path_params["user_id"])
        social.direct_messages(user, target, 1 << 62)  # permission check
        cursor = {"after": number(request.query_params.get("after"), "after")}

        def snapshot():
            while True:  # a long gap arrives as several full pages, in order
                rows = social.direct_messages(user, target, cursor["after"])
                if rows:
                    cursor["after"] = rows[-1]["id"]
                yield "messages", {"messages": rows}
                if len(rows) < 300:
                    return

        return sse(request, social.dm_channel(user["id"], target), snapshot, user, lambda: social.direct_messages(user, target, 1 << 62))

    async def inbox_stream(request: Request, user: dict):
        """Ping the console when friend requests, access requests, direct or group messages arrive."""

        def snapshot():
            data = social.friends(user)
            contacts = social.contacts(user)
            yield "inbox", {
                "friend_requests": len(data["incoming"]),
                "access_requests": len(contacts["incoming"]),
                "unread": {f["user_id"]: f["unread"] for f in data["friends"] if f["unread"]},
                "group_unread": {g["id"]: g["unread"] for g in social.groups(user) if g["unread"]},
                "at": time.time(),
            }

        return sse(request, f"user:{user['id']}", snapshot, user)

    async def contacts(request: Request, user: dict):
        if request.method == "POST":
            data = await body(request)
            return ok({"contact": social.request_contact(user, data.get("agent") or "", text(data, "note"))})
        return ok(social.contacts(user))

    async def contact_action(request: Request, user: dict):
        contact_id = int(request.path_params["contact_id"])
        if request.method == "DELETE":
            social.remove_contact(user, contact_id)
            return ok()
        action = request.path_params.get("action")
        if action not in ("accept", "reject", "workspaces", "full"):
            raise HubError("未知操作", 404)
        data = await body(request)
        if action == "full":
            if not isinstance(data.get("allow"), bool):
                raise HubError("allow 必须是 true 或 false")
            social.set_contact_full(user, contact_id, data["allow"])
            return ok()
        if action == "workspaces":
            social.set_contact_workspaces(user, contact_id, data.get("workspaces") or [])
            return ok()
        social.answer_contact(user, contact_id, action == "accept", flag(data, "trust"), data.get("workspaces"))
        return ok()

    async def agent_conversations(request: Request, user: dict):
        return ok({"tasks": social.conversations(user, int(request.path_params["agent_id"]))})

    async def groups(request: Request, user: dict):
        if request.method == "POST":
            data = await body(request)
            return ok({"group": social.create_group(
                user, text(data, "name"), data.get("members") or [], data.get("people") or [])})
        return ok({"groups": social.groups(user)})

    async def group_item(request: Request, user: dict):
        group_id = int(request.path_params["group_id"])
        if request.method == "DELETE":
            social.delete_group(user, group_id)
            return ok()
        if request.method == "PUT":
            data = await body(request)
            if "name" in data and not isinstance(data["name"], str):
                raise HubError("群名必须是文字")  # an explicit null too, before anything is written
            social.update_group(user, group_id, data.get("name"), data.get("members"), data.get("people"))
        group = social.visible_group(user, group_id)
        before = number(request.query_params.get("before"), "before")
        if before:
            return ok({"timeline": social.timeline(group_id, user, before)})
        social.mark_group_read(user, group_id)
        return ok({"group": group, "members": social.members(group_id, user), "people": social.people(group_id),
                   "timeline": social.timeline(group_id, user)})

    async def group_leave(request: Request, user: dict):
        social.leave_group(user, int(request.path_params["group_id"]))
        return ok()

    async def group_messages(request: Request, user: dict):
        data = await body(request)
        return ok(await social.post(user, int(request.path_params["group_id"]), text(data, "content"), data.get("agents")))

    async def group_stream(request: Request, user: dict):
        group_id = int(request.path_params["group_id"])
        social.visible_group(user, group_id)
        # the view's oldest message stays fixed for the stream, so new messages never push shown ones out
        cursor = {"since": number(request.query_params.get("since"), "since")}

        def snapshot():
            social.mark_group_read(user, group_id)
            timeline = social.timeline(group_id, user, since=cursor["since"])
            if not cursor["since"] and timeline:
                cursor["since"] = timeline[0]["id"]
            yield "timeline", {"members": social.members(group_id, user), "people": social.people(group_id),
                               "timeline": timeline, "since": cursor["since"]}

        return sse(request, f"group:{group_id}", snapshot, user, lambda: social.visible_group(user, group_id))

    # ---- approval rules ---------------------------------------------

    async def rules(request: Request, user: dict):
        if request.method == "POST":
            data = await body(request)
            try:
                from_user_id = int(data["from_user_id"]) if data.get("from_user_id") else None
                agent_id = int(data["agent_id"]) if data.get("agent_id") else None
            except (TypeError, ValueError):
                raise HubError("from_user_id / agent_id 必须是数字")
            if agent_id:
                agent = db.one("SELECT user_id FROM agents WHERE id = ?", (agent_id,))
                if not agent or agent["user_id"] != user["id"]:
                    raise HubError("只能为自己的智能体设置免审批")
            if from_user_id and not hub.get_user(int(from_user_id)):
                raise HubError("用户不存在")
            rule_id = db.execute(
                "INSERT INTO approval_rules (owner_user_id, from_user_id, agent_id, note, enabled, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (user["id"], from_user_id, agent_id, text(data, "note")[:200], time.time()),
            )
            db.audit(user["id"], "rule.create", rule_id=rule_id, from_user_id=from_user_id, agent_id=agent_id)
            return ok({"id": rule_id})
        rows = db.all(
            "SELECT r.*, u.username AS from_username, u.display_name AS from_display_name, a.key AS agent_key, "
            "a.name AS agent_name FROM approval_rules r LEFT JOIN users u ON u.id = r.from_user_id "
            "LEFT JOIN agents a ON a.id = r.agent_id WHERE r.owner_user_id = ? ORDER BY r.id DESC",
            (user["id"],),
        )
        return ok({"rules": rows})

    async def rule_item(request: Request, user: dict):
        rule_id = int(request.path_params["rule_id"])
        if not db.one("SELECT id FROM approval_rules WHERE id = ? AND owner_user_id = ?", (rule_id, user["id"])):
            raise HubError("规则不存在", 404)
        if request.method == "DELETE":
            db.execute("DELETE FROM approval_rules WHERE id = ?", (rule_id,))
            db.audit(user["id"], "rule.delete", rule_id=rule_id)
        else:
            enabled = int(bool((await body(request)).get("enabled")))
            db.execute("UPDATE approval_rules SET enabled = ? WHERE id = ?", (enabled, rule_id))
            db.audit(user["id"], "rule.toggle", rule_id=rule_id, enabled=enabled)
        return ok()

    async def audit(request: Request, user: dict):
        if user["is_admin"] and request.query_params.get("scope") == "all":
            rows = db.all(
                "SELECT l.*, u.username FROM audit_logs l LEFT JOIN users u ON u.id = l.user_id ORDER BY l.id DESC LIMIT 300"
            )
        else:
            rows = db.all(
                "SELECT l.*, u.username FROM audit_logs l LEFT JOIN users u ON u.id = l.user_id "
                "WHERE l.user_id = ? ORDER BY l.id DESC LIMIT 300",
                (user["id"],),
            )
        return ok({"logs": rows})

    # ---- connector websocket ----------------------------------------

    async def connector_ws(websocket: WebSocket):
        token_row = resolve_api_token(db, bearer(websocket.headers), "connector")
        if not token_row:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        connector_id = None
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            if hello.get("type") != "hello":
                await websocket.close(code=4400)
                return
            connector_id = hub.register_connector(token_row, hello, websocket.client.host if websocket.client else "")
            old = hub.connections.get(connector_id)
            hub.connections[connector_id] = Connection(connector_id, websocket.send_json, websocket.close)
            if old:
                LOGGER.info("connector %s replaced an existing session", connector_id)
                try:
                    await old.close()
                except Exception:  # noqa: BLE001
                    pass
            await hub.connections[connector_id].send({"type": "welcome", "connector_id": connector_id, "features": list(FEATURES)})
            notice = hub.update_notice(connector_id)
            if notice:
                await hub.connections[connector_id].send(notice)
            await hub.resend_cancels(connector_id, {int(i) for i in hello.get("running") or [] if str(i).isdigit()})
            await hub.dispatch(connector_id)
            while True:
                message = await websocket.receive_json()
                if hub.connections.get(connector_id) is None or hub.connections[connector_id].send_json != websocket.send_json:
                    break  # revoked or superseded session
                if isinstance(message, dict):
                    await hub.handle_connector_message(connector_id, message)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            LOGGER.exception("connector socket error")
            try:
                # unacknowledged reports stay in the connector's outbox and are replayed on reconnect
                await websocket.close(code=1011)
            except Exception:  # noqa: BLE001 - already gone
                pass
        finally:
            current = hub.connections.get(connector_id) if connector_id else None
            if current and current.send_json == websocket.send_json:
                hub.connector_offline(connector_id)

    def base_url(request: Request) -> str:
        return os.getenv("AGENT_HUB_PUBLIC_URL") or str(request.base_url).rstrip("/")

    def require_admin(user: dict) -> None:
        if not user["is_admin"]:
            raise HubError("仅管理员可以管理连接器版本", 403)

    async def downloads(request: Request, user: dict):
        files = []
        for platform in PLATFORMS:
            latest = releases.latest(platform)
            if latest:
                files.append({"platform": platform, "version": latest["version"], "size": latest["size"],
                              "sha256": latest["sha256"], "notes": latest["notes"], "created_at": latest["created_at"],
                              "url": f"/downloads/latest/{platform.replace('/', '-')}"})
        return ok({"files": files, "storage": storage.kind})

    async def download_latest(request: Request):
        platform = request.path_params["platform"].replace("-", "/", 1)
        latest = releases.latest(platform) if platform in PLATFORMS else None
        if not latest:
            return fail("该平台暂无已发布的连接器", 404)
        return RedirectResponse(releases.ticket(latest["id"], base_url(request))["download_url"], status_code=302)

    async def artifacts(request: Request):
        if not isinstance(storage, LocalStorage):
            return fail("not found", 404)
        q = request.query_params
        try:
            key, expires_at = q["key"], int(q["exp"])
        except (KeyError, ValueError):
            return fail("bad request", 400)
        if not storage.verify(key, expires_at, q.get("sig", "")):
            return fail("下载链接无效或已过期", 403)
        path = storage.path(key)
        if not path.exists():
            return fail("not found", 404)
        return FileResponse(path, filename=q.get("name") or path.name, media_type="application/octet-stream")

    async def release_list(request: Request, user: dict):
        if request.method == "GET":
            return ok({"releases": releases.list(), "platforms": list(PLATFORMS), "storage": storage.kind})
        require_admin(user)
        q = request.query_params
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "upload.bin"
            size = 0
            with open(target, "wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        raise HubError("安装包超过 200MB 上限")
                    fh.write(chunk)
            row = releases.publish(
                user["id"], q.get("version", ""), q.get("platform", ""), target,
                q.get("notes", ""), q.get("policy", "OPTIONAL"), q.get("min_supported", ""),
            )
        await hub.notify_updates(row["platform"])
        return ok({"release": row})

    async def release_withdraw(request: Request, user: dict):
        require_admin(user)
        releases.withdraw(user["id"], int(request.path_params["release_id"]))
        return ok()

    def connector_auth(request: Request) -> dict:
        row = resolve_api_token(db, bearer(request.headers), "connector")
        if not row:
            raise HubError("连接器令牌无效", 401)
        return row

    async def connector_latest(request: Request):
        connector_auth(request)
        q = request.query_params
        return ok(releases.check(q.get("platform", ""), q.get("current", "")))

    async def connector_ticket(request: Request):
        connector_auth(request)
        data = await body(request)
        try:
            release_id = int(data.get("release_id"))
        except (TypeError, ValueError):
            raise HubError("release_id 必须是数字")
        return ok(releases.ticket(release_id, base_url(request)))

    async def healthz(request: Request):
        return ok({"status": "ok", "version": __version__, "connectors_online": len(hub.connections)})

    async def public_doc(request: Request):
        """Public guides (connecting a machine, running a work server), written for people and for the agents they hand the link to."""
        hub_url = base_url(request)
        name = request.url.path.rsplit("/", 1)[-1]
        text = (Path(__file__).parent / "docs" / name).read_text(encoding="utf-8")
        text = text.replace("{{HUB}}", hub_url).replace("{{HOST}}", urlsplit(hub_url).hostname or "")
        return Response(text, media_type="text/markdown; charset=utf-8")

    async def console_index(request: Request):
        index = WEB_DIR / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})  # always check for a new build
        return Response("console not built: run `npm run build` in frontend/", status_code=503)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        db.execute("UPDATE connectors SET online = 0")
        db.execute("UPDATE agents SET online = 0")
        releases.bootstrap(settings.download_dir)
        await workflows.recover()
        await hub.recover_rework()
        async with mcp_app.lifespan(app):
            yield

    api = [
        Route("/api/auth/options", endpoint(auth_options, public=True)),
        Route("/api/auth/register", endpoint(register, public=True), methods=["POST"]),
        Route("/api/auth/login", endpoint(login, public=True), methods=["POST"]),
        Route("/api/auth/me", endpoint(me)),
        Route("/api/auth/password", endpoint(change_password), methods=["POST"]),
        Route("/api/auth/profile", endpoint(update_profile), methods=["PUT"]),
        Route("/api/users", endpoint(users)),
        Route("/api/admin/users", endpoint(admin_users)),
        Route("/api/admin/hosts", endpoint(admin_hosts)),
        Route("/api/admin/users/{user_id:int}", endpoint(admin_user_item), methods=["PUT"]),
        Route("/api/friends", endpoint(friends), methods=["GET", "POST"]),
        Route("/api/friends/{friendship_id:int}", endpoint(friend_action), methods=["DELETE"]),
        Route("/api/friends/{friendship_id:int}/{action}", endpoint(friend_action), methods=["POST"]),
        Route("/api/dm/{user_id:int}", endpoint(direct_messages), methods=["GET", "POST"]),
        Route("/api/auto-replies", endpoint(auto_replies), methods=["GET", "PUT"]),
        Route("/api/dm/{user_id:int}/stream", endpoint(direct_stream)),
        Route("/api/inbox/stream", endpoint(inbox_stream)),
        Route("/api/overview", endpoint(overview)),
        Route("/api/overview/stream", endpoint(overview_stream)),
        Route("/api/tasks/precheck", endpoint(task_precheck), methods=["POST"]),
        Route("/api/tasks/{task_id:int}/tree", endpoint(task_tree)),
        Route("/api/tasks/{task_id:int}/artifacts", endpoint(task_artifacts)),
        Route("/api/artifacts/{artifact_id:int}", endpoint(artifact_content)),
        Route("/api/artifacts/{artifact_id:int}/review", endpoint(artifact_review), methods=["POST"]),
        Route("/api/workflows", endpoint(workflow_list), methods=["GET", "POST"]),
        Route("/api/workflows/{run_id:int}", endpoint(workflow_item)),
        Route("/api/workflows/{run_id:int}/{action}", endpoint(workflow_action), methods=["POST"]),
        Route("/api/agents", endpoint(agents)),
        Route("/api/agents/{agent_id:int}", endpoint(delete_agent), methods=["DELETE"]),
        Route("/api/connectors", endpoint(connectors)),
        Route("/api/tokens", endpoint(tokens), methods=["GET", "POST"]),
        Route("/api/tokens/{token_id:int}", endpoint(revoke_token), methods=["DELETE"]),
        Route("/api/tasks", endpoint(tasks), methods=["GET", "POST"]),
        Route("/api/tasks/{task_id:int}", endpoint(task_detail)),
        Route("/api/tasks/{task_id:int}/shares", endpoint(task_shares), methods=["GET", "POST"]),
        Route("/api/tasks/{task_id:int}/shares/{user_id:int}", endpoint(task_shares), methods=["DELETE"]),
        Route("/api/tasks/{task_id:int}/{action}", endpoint(task_action), methods=["POST"]),
        Route("/api/tasks/{task_id:int}/stream", endpoint(task_stream)),
        Route("/api/tasks/{task_id:int}/files/{event_id:int}", endpoint(task_file)),
        Route("/api/agents/{agent_id:int}/local-sessions", endpoint(local_sessions), methods=["GET", "POST"]),
        Route("/api/agents/{agent_id:int}/workspaces", endpoint(agent_workspaces), methods=["PUT"]),
        Route("/api/agents/{agent_id:int}/visibility", endpoint(agent_visibility), methods=["PUT"]),
        Route("/api/agents/{agent_id:int}/conversations", endpoint(agent_conversations)),
        Route("/api/contacts", endpoint(contacts), methods=["GET", "POST"]),
        Route("/api/contacts/{contact_id:int}", endpoint(contact_action), methods=["DELETE"]),
        Route("/api/contacts/{contact_id:int}/{action}", endpoint(contact_action), methods=["POST"]),
        Route("/api/groups", endpoint(groups), methods=["GET", "POST"]),
        Route("/api/groups/{group_id:int}", endpoint(group_item), methods=["GET", "PUT", "DELETE"]),
        Route("/api/groups/{group_id:int}/messages", endpoint(group_messages), methods=["POST"]),
        Route("/api/groups/{group_id:int}/stream", endpoint(group_stream)),
        Route("/api/groups/{group_id:int}/leave", endpoint(group_leave), methods=["POST"]),
        Route("/api/rules", endpoint(rules), methods=["GET", "POST"]),
        Route("/api/rules/{rule_id:int}", endpoint(rule_item), methods=["PUT", "DELETE"]),
        Route("/api/audit", endpoint(audit)),
        Route("/api/downloads", endpoint(downloads)),
        Route("/api/releases", endpoint(release_list), methods=["GET", "POST"]),
        Route("/api/releases/{release_id:int}/withdraw", endpoint(release_withdraw), methods=["POST"]),
        Route("/api/work", endpoint(work_home)),
        Route("/api/work/connect", endpoint(work_connect), methods=["POST"]),
        Route("/api/work/dirs", endpoint(work_dirs), methods=["POST"]),
        Route("/api/work/dirs/{dir_id:int}", endpoint(work_dir_item), methods=["PUT"]),
        Route("/api/work/dirs/{dir_id:int}/repos", endpoint(work_repos), methods=["POST"]),
        Route("/api/work/repos/{repo_id:int}", endpoint(work_repo_item), methods=["PUT", "DELETE"]),
        Route("/api/collab/machines", endpoint(collab_machines)),
        Route("/api/collab/{cid:int}", endpoint(collab_machine), methods=["PUT"]),
        Route("/api/collab/{cid:int}/overview", endpoint(collab_overview)),
        Route("/api/collab/{cid:int}/session", endpoint(collab_session)),
        Route("/api/collab/{cid:int}/discussion", endpoint(collab_discussion)),
        Route("/api/work/sessions", endpoint(work_sessions), methods=["GET", "POST"]),
        Route("/api/work/sessions/{session_id:int}", endpoint(work_session_item), methods=["GET", "DELETE"]),
        Route("/api/work/admin", endpoint(work_admin)),
        Route("/api/work/hosts/{connector_id:int}/agents", endpoint(work_host_agents), methods=["POST"]),
        Route("/api/work/hosts/{connector_id:int}/access", endpoint(work_host_access), methods=["POST"]),
        Route("/api/work/sessions/{session_id:int}/mrs", endpoint(work_session_mrs)),
        Route("/api/work/gitlab/projects", endpoint(work_gitlab_projects)),
        Route("/api/work/gitlab/authorize", endpoint(work_gitlab_authorize), methods=["POST"]),
        Route("/api/work/gitlab/callback", work_gitlab_callback),
        Route("/api/work/admin/{connector_id:int}/settings", endpoint(work_admin_settings), methods=["POST"]),
        Route("/api/work/admin/{connector_id:int}/current", endpoint(work_admin_current), methods=["POST"]),
        Route("/api/work/admin/{connector_id:int}/containers/{user}/{action}", endpoint(work_admin_container), methods=["POST"]),
        Route("/api/work/sessions/{session_id:int}/changes", endpoint(work_session_changes)),
        Route("/api/work/sessions/{session_id:int}/stream", endpoint(work_session_stream)),
        Route("/api/work/sessions/{session_id:int}/{action}", endpoint(work_session_action), methods=["POST"]),
        Route("/api/connector/latest", endpoint(connector_latest, public=True)),
        Route("/api/connector/download-ticket", endpoint(connector_ticket, public=True), methods=["POST"]),
    ]
    routes = [
        *api,
        Route("/healthz", healthz),
        WebSocketRoute("/ws/connector", connector_ws),
        Mount("/mcp", app=McpAuthGate(mcp_app, hub)),
        Route("/", lambda r: RedirectResponse("/console/")),
        Route("/favicon.ico", lambda r: RedirectResponse("/console/favicon.ico", status_code=301)),
    ]
    routes.append(Route("/downloads/latest/{platform}", download_latest))
    routes.append(Route("/docs/onboarding.md", public_doc))
    routes.append(Route("/docs/work-servers.md", public_doc))
    routes.append(Route("/artifacts", artifacts))
    if (WEB_DIR / "assets").exists():
        routes.append(Mount("/console/assets", app=StaticFiles(directory=WEB_DIR / "assets")))
    routes.append(Route("/console/{path:path}", console_index))

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.hub = hub
    app.state.db = db
    return app
