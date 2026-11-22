"""Task lifecycle, approval policy and dispatch to connected connectors."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import redact
from .auth import create_api_token
from .db import Database

LOGGER = logging.getLogger(__name__)

MAX_DEPTH = 5
MCP_KINDS = ("claude", "codex")  # CLIs the connector can hand a task-scoped MCP token to
MAX_EVENT_CHARS = 20_000
ACTIVE = ("pending_approval", "queued", "dispatched", "running")
FINISHED = ("completed", "failed", "canceled", "rejected")
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
ARTIFACT_KINDS = ("report", "diff", "test", "image", "file")
EVENT_SCHEMA = 1
# what the hub understands; sent in `welcome` so newer connectors can fall back for older hubs
FEATURES = ("events_v2", "ack", "telemetry", "execution_id")
QUIET_SECONDS = 30
TELEMETRY_TTL = 75  # connectors report every 20 s; older figures are not shown as live  # a running turn with no events for this long is flagged (never declared stuck)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# images a person attaches to a message: they travel to the agent inside the dispatch message
MAX_INPUT_IMAGES = 6
MAX_INPUT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_INPUT_IMAGES_BYTES = 20 * 1024 * 1024
IMAGE_EMPTY_PROMPT = "（见附图）"
COLLAB_SINCE = (0, 9, 11)  # first connector that answers 本机协作 requests


# How a task ran (commands, tool calls, raw CLI output) stays with the agent's owner.
PROCESS_KINDS = ("output", "tool")
CONFIDENTIAL_NOTE = (
    "【保密要求】本任务的发起人或可见者不是本机归属人，他们只能看到你的最终回复。回复里只给结论和必要结果，"
    "不要透露本机路径、密钥或凭据文件位置、服务器登录方式与账号、执行过的具体命令、知识库或记忆里的内部记录；"
    "做不了的事只说明原因类别（如“需要归属人在本机授权”），不写具体操作方法。\n\n"
)

def public_activity(activity: dict | None) -> dict | None:
    """The latest activity as people other than the agent's owner see it: what is running, with
    credentials masked and calls that touch secrets hidden."""
    if not activity:
        return activity
    out = {k: activity[k] for k in ("kind", "summary", "at")}
    out["summary"] = redact.HIDDEN if activity.get("hidden") else activity.get("public_summary", "")
    for k in ("tool", "open_tool"):
        if k in activity:
            out[k] = activity[k]
    return out


def public_code_ref(code_ref: dict) -> dict:
    """Commit and branch are shared with collaborators; the local repository path stays with the owner."""
    return {k: v for k, v in (code_ref or {}).items() if k != "repo"}


class HubError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Connection:
    connector_id: int
    send_json: Callable[[dict], Awaitable[None]]
    close: Callable[[], Awaitable[None]]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict) -> None:
        async with self.lock:
            await self.send_json(message)


ISOLATIONS = ("sandbox", "limited", "none")


def workspace_id(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def _workspaces(item: dict) -> list[dict]:
    result = []
    for w in item.get("workspaces") or []:
        if isinstance(w, dict) and str(w.get("path", "")).strip():
            result.append({"name": str(w.get("name") or w["path"])[:60], "path": str(w["path"]).strip()[:500]})
    return result[:50]


def agent_address(agent: dict) -> str:
    return f"{agent['username']}/{agent['key']}"


def _json_or_none(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


TOOL_PHASES = ("started", "completed", "failed")


def clean_tool_payload(raw: Any) -> dict | None:
    """Validate a structured tool event from a connector; anything malformed is dropped, not guessed."""
    if not isinstance(raw, dict) or raw.get("phase") not in TOOL_PHASES:
        return None
    name = str(raw.get("name") or "").strip()[:80]
    call_id = str(raw.get("call_id") or "").strip()[:120]
    if not name or not call_id:
        return None
    payload = {
        "schema_version": EVENT_SCHEMA,
        "call_id": call_id,
        "name": name,
        "phase": raw["phase"],
        "summary": str(raw.get("summary") or "")[:600],
    }
    # judged on the full text: trimming a long command must not drop the part that names a secret
    if raw.get("sensitive") is True or redact.sensitive(str(raw.get("summary") or "")):
        payload["sensitive"] = True
    observed = raw.get("observed_at")
    if isinstance(observed, (int, float)) and observed > 0:
        payload["observed_at"] = float(observed)
    if isinstance(raw.get("exit_code"), int):
        payload["exit_code"] = raw["exit_code"]
    return payload


def _usage_text(usage: dict) -> str:
    parts = []
    if "input_tokens" in usage or "output_tokens" in usage:
        parts.append(f"输入 {usage.get('input_tokens', 0)} / 输出 {usage.get('output_tokens', 0)} tokens")
    if "cost_usd" in usage:
        parts.append(f"约 ${usage['cost_usd']}（CLI 报告）")
    if "model" in usage:
        parts.append(usage["model"])
    return "，".join(parts) or "用量已记录"


def clean_usage(raw: Any) -> dict | None:
    """Token/cost numbers exactly as the CLI reported them; `source` says they are vendor-reported."""
    if not isinstance(raw, dict):
        return None
    usage = {}
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "duration_ms"):
        if isinstance(raw.get(key), (int, float)) and raw[key] >= 0:
            usage[key] = int(raw[key])
    if isinstance(raw.get("cost_usd"), (int, float)) and raw["cost_usd"] >= 0:
        usage["cost_usd"] = round(float(raw["cost_usd"]), 6)
    if isinstance(raw.get("model"), str):
        usage["model"] = raw["model"][:80]
    if not usage:
        return None
    usage["source"] = "reported"
    return usage


CAPABILITY_VALUES = {"tool_trace": ("none", "summary", "lifecycle"), "usage": ("none", "final")}


def clean_capabilities(raw: Any) -> dict:
    """Unknown or missing capabilities stay `unknown` (an older connector), never assumed."""
    raw = raw if isinstance(raw, dict) else {}
    return {k: (raw.get(k) if raw.get(k) in allowed else "unknown") for k, allowed in CAPABILITY_VALUES.items()}


class Hub:
    def __init__(self, db: Database):
        self.db = db
        self.connections: dict[int, Connection] = {}
        self._subscribers: dict[str, set[asyncio.Event]] = {}
        self.update_check: Callable[[str, str], dict] | None = None
        self.attachments_dir: Path | None = None
        self._requests: dict[str, tuple[int, asyncio.Future]] = {}
        # live per-agent load reported by connectors: agent_id -> {running, queued, oldest_wait_s, lock_wait, at}
        self.telemetry: dict[int, dict] = {}
        # blocking waits in progress: waiter task id -> set of task ids it is waiting for (MCP wait_seconds)
        self.waits: dict[int, set[int]] = {}
        # tasks blocked by a workspace lock: task id -> task id holding the lock (reported by the connector)
        self.lock_waits: dict[int, tuple[int, float]] = {}  # task -> (holder task, when reported)
        # called with a task id whenever a task reaches a final status (workflows advance on it)
        self.on_finished: list[Callable[[int], Awaitable[None]]] = [self._send_queued_rework]
        # extra checks right before a queued task is sent; a non-empty reason fails it instead
        self.dispatch_guards: list[Callable[[dict], str]] = []
        # add fields to a task's payload right before it is sent (the 「工作」menu adds its container context)
        self.payload_hooks: list[Callable[[dict, dict], None]] = []

    # ---- live notifications (SSE) -------------------------------------

    def subscribe(self, channel: str) -> asyncio.Event:
        event = asyncio.Event()
        self._subscribers.setdefault(channel, set()).add(event)
        return event

    def unsubscribe(self, channel: str, event: asyncio.Event) -> None:
        subs = self._subscribers.get(channel)
        if subs:
            subs.discard(event)
            if not subs:
                self._subscribers.pop(channel, None)

    def publish(self, channel: str) -> None:
        for event in self._subscribers.get(channel, ()):
            event.set()

    def _publish_task(self, task_id: int) -> None:
        self.publish(f"task:{task_id}")
        self.publish("tasks")
        row = self.db.one("SELECT group_id FROM tasks WHERE id = ?", (task_id,))
        if row and row["group_id"]:
            self.publish(f"group:{row['group_id']}")
            # agent replies count as group unread, so refresh every member's inbox
            for member in self.db.all(
                "SELECT owner_user_id AS id FROM chat_groups WHERE id = ? UNION SELECT user_id FROM group_users "
                "WHERE group_id = ? UNION SELECT a.user_id FROM group_agents ga JOIN agents a ON a.id = ga.agent_id "
                "WHERE ga.group_id = ?",
                (row["group_id"],) * 3,
            ):
                self.publish(f"user:{member['id']}")

    # ---- lookups -------------------------------------------------------

    def get_user(self, user_id: int) -> dict | None:
        return self.db.one(
            "SELECT id, username, display_name, is_admin, disabled, created_at FROM users WHERE id = ?", (user_id,)
        )

    def list_agents(self, query: str = "", online_only: bool = False, viewer: dict | None = None) -> list[dict]:
        """With a viewer, other people's agents only expose the workspaces granted to that viewer, by name.

        Admins see everything. `access` is the viewer's permission state: owner | accepted | pending | none.
        Agents their owner hid are left out for everyone who has no access to them yet.
        """
        rows = self._agents(query, online_only)
        if viewer is None:
            return rows
        access = {
            r["agent_id"]: r
            for r in self.db.all("SELECT agent_id, status, workspaces, allow_full FROM agent_contacts WHERE user_id = ?", (viewer["id"],))
        }
        friends = {
            r["other"]
            for r in self.db.all(
                "SELECT CASE WHEN user_id = ? THEN friend_id ELSE user_id END AS other FROM friendships "
                "WHERE status = 'accepted' AND (user_id = ? OR friend_id = ?)",
                (viewer["id"],) * 3,
            )
        }
        shown = []
        for row in rows:
            if row["user_id"] == viewer["id"]:
                row["access"] = "owner"
                shown.append(row)
                continue
            contact = access.get(row["id"])
            row["access"] = contact["status"] if contact else "none"
            row["allow_full"] = bool(contact and contact["allow_full"])
            if row["hidden"] and row["access"] != "accepted" and not viewer.get("is_admin"):
                continue
            shown.append(row)
            row["is_friend"] = row["user_id"] in friends
            if viewer.get("is_admin"):
                continue
            granted = json.loads(contact["workspaces"] or "[]") if row["access"] == "accepted" else []
            row["workspaces"] = [{"id": w["id"], "name": w["name"]} for w in row["workspaces"] if w["path"] in granted]
            row["workdir"] = ""
            if row["load"].get("lock_wait"):
                row["load"] = {**row["load"], "lock_wait": {k: v for k, v in row["load"]["lock_wait"].items() if k != "path"}}
        return shown

    def _agents(self, query: str = "", online_only: bool = False) -> list[dict]:
        rows = self.db.all(
            "SELECT a.*, u.username, u.display_name AS owner_name, c.hostname, c.os, c.name AS connector_name "
            "FROM agents a JOIN users u ON u.id = a.user_id JOIN connectors c ON c.id = a.connector_id "
            "ORDER BY a.online DESC, u.username, a.key"
        )
        result = []
        needle = query.strip().lower()
        for row in rows:
            row["tags"] = json.loads(row["tags"] or "[]")
            row["capabilities"] = clean_capabilities(_json_or_none(row.get("capabilities")))
            row["load"] = self.agent_load(row["id"], bool(row["online"]))
            row["workspaces"] = [{**w, "id": workspace_id(w["path"])} for w in json.loads(row["workspaces"] or "[]")]
            row["address"] = agent_address(row)
            row["online"] = bool(row["online"])
            if online_only and not row["online"]:
                continue
            haystack = " ".join([row["address"], row["name"], row["description"], row["owner_name"], *row["tags"]])
            if needle and needle not in haystack.lower():
                continue
            result.append(row)
        return result

    def resolve_agent(self, target: str | int) -> dict:
        if isinstance(target, int) or str(target).isdigit():
            row = self.db.one(
                "SELECT a.*, u.username FROM agents a JOIN users u ON u.id = a.user_id WHERE a.id = ?", (int(target),)
            )
        else:
            username, _, key = str(target).partition("/")
            row = self.db.one(
                "SELECT a.*, u.username FROM agents a JOIN users u ON u.id = a.user_id "
                "WHERE u.username = ? AND a.key = ?",
                (username.strip(), key.strip()),
            )
        if not row:
            raise HubError(f"智能体不存在: {target}（地址格式为 用户名/智能体标识，可先调用 list_agents）", 404)
        return row

    def get_task(self, task_id: int) -> dict:
        row = self.db.one(
            "SELECT t.*, a.key AS agent_key, a.name AS agent_name, a.kind AS agent_kind, a.user_id AS owner_user_id, "
            "a.connector_id AS agent_connector_id, t.connector_id AS task_connector_id, ou.username AS owner_username, fu.username AS from_username "
            "FROM tasks t JOIN agents a ON a.id = t.agent_id JOIN users ou ON ou.id = a.user_id "
            "JOIN users fu ON fu.id = t.from_user_id WHERE t.id = ?",
            (task_id,),
        )
        if not row:
            raise HubError(f"任务不存在: {task_id}", 404)
        row["agent_address"] = f"{row['owner_username']}/{row['agent_key']}"
        row["auto_approved"] = bool(row["auto_approved"])
        # the connector a task was dispatched to owns its reports, even if the agent moved since
        row["connector_id"] = row.pop("task_connector_id") or row["agent_connector_id"]
        return row

    def is_party(self, user: dict, task: dict) -> bool:
        return user["id"] in (task["from_user_id"], task["owner_user_id"])

    def share_of(self, user: dict, task_id: int) -> dict | None:
        return self.db.one("SELECT * FROM task_shares WHERE task_id = ? AND user_id = ?", (task_id, user["id"]))

    def can_view(self, user: dict, task: dict) -> bool:
        if user["is_admin"] or user["id"] in (task["from_user_id"], task["owner_user_id"]):
            return True
        if self.share_of(user, task["id"]):
            return True
        # group members read the replies posted in their group
        return bool(task.get("group_id") and self.db.one(
            "SELECT 1 FROM chat_groups g WHERE g.id = ? AND (g.owner_user_id = ? OR EXISTS "
            "(SELECT 1 FROM group_users gu WHERE gu.group_id = g.id AND gu.user_id = ?) OR EXISTS "
            "(SELECT 1 FROM group_agents ga JOIN agents a ON a.id = ga.agent_id WHERE ga.group_id = g.id AND a.user_id = ?))",
            (task["group_id"], user["id"], user["id"], user["id"]),
        ))

    def visible_task(self, user: dict, task_id: int) -> dict:
        task = self.get_task(task_id)
        if not self.can_view(user, task):
            raise HubError("无权查看该任务", 403)
        return task

    def list_tasks(self, user: dict, box: str = "all", status: str = "", limit: int = 100, q: str = "",
                   recent: bool = False) -> list[dict]:
        """recent: latest activity first (a continued old conversation counts as recent), not newest created."""
        where, params = ["t.id NOT IN (SELECT task_id FROM work_sessions)"], []  # 「工作」sessions have their own page
        if box == "sent":
            where.append("t.from_user_id = ?")
            params.append(user["id"])
        elif box == "received":
            where.append("a.user_id = ?")
            params.append(user["id"])
        elif box == "approval":
            where.append("a.user_id = ? AND t.status = 'pending_approval'")
            params.append(user["id"])
        elif box == "shared":
            where.append("t.id IN (SELECT task_id FROM task_shares WHERE user_id = ?)")
            params.append(user["id"])
        elif not (box == "everyone" and user["is_admin"]):
            where.append("(t.from_user_id = ? OR a.user_id = ? OR t.id IN (SELECT task_id FROM task_shares WHERE user_id = ?))")
            params += [user["id"], user["id"], user["id"]]
        if status == "active":
            where.append("t.status IN ('pending_approval', 'queued', 'dispatched', 'running')")
        elif status:
            where.append("t.status = ?")
            params.append(status)
        if q.strip():
            needle = q.strip()
            if needle.lstrip("#").isdigit():
                where.append("t.id = ?")
                params.append(int(needle.lstrip("#")))
            else:
                where.append("(t.title LIKE ? OR a.name LIKE ? OR a.key LIKE ? OR ou.username LIKE ?)")
                params += [f"%{needle}%"] * 4
        sql = (
            "SELECT t.id, t.title, t.status, t.turn, t.depth, t.parent_task_id, t.from_agent, t.auto_approved, t.source, "
            "t.created_at, t.updated_at, t.finished_at, t.agent_id, t.stop_pending, a.key AS agent_key, a.name AS agent_name, "
            "a.kind AS agent_kind, a.connector_id AS agent_connector_id, a.user_id AS owner_user_id, ou.username AS owner_username, "
            "fu.username AS from_username, CASE WHEN t.turn > 1 THEN tu.username END AS turn_username "
            "FROM tasks t JOIN agents a ON a.id = t.agent_id JOIN users ou ON ou.id = a.user_id "
            "JOIN users fu ON fu.id = t.from_user_id LEFT JOIN users tu ON tu.id = t.turn_user_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {'t.updated_at DESC, ' if recent else ''}t.id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = self.db.all(sql, params)
        for row in rows:
            row["agent_address"] = f"{row['owner_username']}/{row['agent_key']}"
            row["auto_approved"] = bool(row["auto_approved"])
            if row["status"] in ACTIVE:
                activity = self.last_activity(row["id"])
                row["wait"] = self.wait_reason(row, activity)
                row["activity"] = activity if self.sees_process(user, row) else public_activity(activity)
        return rows

    # ---- execution flow: the tree around a task ----------------------------

    def task_tree(self, user: dict, task_id: int) -> dict:
        """The whole delegation tree containing this task, with a timeline per node.

        Nodes the viewer may not see keep their place in the tree (status, timing) but no title or content.
        """
        self.visible_task(user, task_id)
        root = self.get_task(task_id)
        while root["parent_task_id"]:
            parent = self.get_task(root["parent_task_id"])
            root = parent
        nodes = [root] + [self.get_task(r["id"]) for r in self.descendants(root["id"])]
        out = []
        for task in nodes:
            visible = self.can_view(user, task)
            # a task the viewer may not see keeps its place in the tree, but only status and timing
            node = {
                "id": task["id"], "parent_task_id": task["parent_task_id"], "status": task["status"],
                "depth": task["depth"], "created_at": task["created_at"], "updated_at": task["updated_at"],
                "finished_at": task["finished_at"], "visible": visible, "stop_pending": bool(task["stop_pending"]),
            }
            if visible:
                activity = self.last_activity(task["id"])
                node.update(
                    title=task["title"], turn=task["turn"], agent_address=task["agent_address"], agent_kind=task["agent_kind"],
                    from_agent=task["from_agent"], from_username=task["from_username"], attempts=task["attempts"],
                    wait=self.wait_reason(task, activity),
                    activity=activity if self.sees_process(user, task) else public_activity(activity),
                    timeline=self.timeline(task["id"]) if self.sees_process(user, task) else self.public_timeline(task["id"]),
                )
            out.append(node)
        return {"root_id": root["id"], "focus_id": task_id, "nodes": out}

    def timeline(self, task_id: int) -> list[dict]:
        """Status changes and tool calls as spans. A call reported only as finished keeps no invented start."""
        spans, open_calls = [], {}
        rows = self.db.all(
            "SELECT id, turn, kind, content, payload, created_at FROM task_events WHERE task_id = ? "
            "AND kind IN ('status', 'tool', 'usage') ORDER BY id",
            (task_id,),
        )
        for row in rows:
            if row["kind"] == "status":
                spans.append({"type": "status", "turn": row["turn"], "at": row["created_at"],
                              "status": row["content"].split(":", 1)[0], "text": row["content"]})
                continue
            payload = _json_or_none(row["payload"]) or {}
            if row["kind"] == "usage":
                spans.append({"type": "usage", "turn": row["turn"], "at": row["created_at"], "usage": payload})
                continue
            at = payload.get("observed_at") or row["created_at"]
            key = (row["turn"], payload.get("call_id"))
            if payload.get("phase") == "started":
                span = {"type": "tool", "turn": row["turn"], "name": payload.get("name"), "summary": payload.get("summary", ""),
                        "start": at, "end": None, "phase": "running", "call_id": payload.get("call_id")}
                open_calls[key] = span
                spans.append(span)
            else:
                span = open_calls.pop(key, None)
                if span is None:
                    span = {"type": "tool", "turn": row["turn"], "name": payload.get("name"), "summary": payload.get("summary", ""),
                            "start": None, "call_id": payload.get("call_id")}
                    spans.append(span)
                span.update(end=at, phase=payload.get("phase"), result=payload.get("summary", ""))
                if "exit_code" in payload:
                    span["exit_code"] = payload["exit_code"]
        return spans[-400:]

    def precheck(self, user: dict, target: str | int, workspace: str = "") -> dict:
        """What would happen if this user dispatched to the agent now. Advisory: create_task checks again."""
        agent = self.resolve_agent(target)
        if agent["hidden"] and agent["user_id"] != user["id"] and not self.contact(user["id"], agent["id"]) and not user.get("is_admin"):
            raise HubError("智能体不存在", 404)
        problems, notes = [], []
        try:
            allowed = self.granted_workspaces(user, agent)
        except HubError as exc:
            allowed = []
            problems.append(str(exc))
        chosen = ""
        if not problems:
            try:
                chosen = self.check_workspace(user, agent, workspace)
            except HubError as exc:
                problems.append(str(exc))
        online = agent["connector_id"] in self.connections and bool(agent["online"])
        load = self.agent_load(agent["id"], online)
        if not online:
            notes.append("智能体离线：任务会排队，连接器上线后自动下发")
        elif load["running"] or load["queued"]:
            notes.append(f"当前运行 {load['running']} 个、排队 {load['queued']} 个，新任务需要排队")
        auto = self.is_auto_approved(user["id"], agent)
        if not auto:
            notes.append(f"需要 {agent['username']} 审批后才会执行")
        caps = clean_capabilities(_json_or_none(agent.get("capabilities")))
        if caps["tool_trace"] in ("none", "unknown"):
            notes.append("该智能体不上报结构化工具轨迹，执行流只显示文本过程")
        isolation = agent.get("isolation") or "none"
        return {
            "agent": {"id": agent["id"], "address": agent_address(agent), "kind": agent["kind"], "isolation": isolation,
                      "capabilities": caps},
            "ok": not problems, "problems": problems, "notes": notes, "online": online, "load": load,
            "needs_approval": not auto,
            "workspace": next((w["name"] for w in allowed if w["path"] == chosen), "默认目录" if not chosen else chosen),
            "workspaces": [{"id": w["id"], "name": w["name"]} for w in allowed],
        }

    def sees_process(self, user: dict | None, task: dict) -> bool:
        """Only the agent's owner (or an admin) sees how a task ran: tool calls, commands, raw output."""
        return user is None or bool(user.get("is_admin")) or user["id"] == task["owner_user_id"]

    def events(self, task_id: int, after_id: int = 0, limit: int = 1000, viewer: dict | None = None,
               turn_from: int = 0, turn_to: int = 0) -> list[dict]:
        """Events after `after_id`, optionally only turns turn_from..turn_to-1 (a chat shows its latest turns first)."""
        task = self.get_task(task_id)
        # filter in SQL so a long run of hidden output never fills a page and stalls the reader's cursor
        rows = self.db.all(
            "SELECT id, turn, kind, content, payload, created_at FROM task_events WHERE task_id = ? AND id > ?"
            " AND turn >= ? AND turn < ? ORDER BY id LIMIT ?",
            (task_id, after_id, turn_from, turn_to or 1 << 30, limit),
        )
        senders = None
        for row in rows:
            row["payload"] = _json_or_none(row["payload"])
            if row["kind"] == "prompt" and not (row["payload"] or {}).get("by"):
                if senders is None:
                    senders = self._prompt_senders(task)
                row["payload"] = {**(row["payload"] or {}), "by": senders.get(row["turn"], task["from_username"])}
        if not self.sees_process(viewer, task):
            rows = self._redact_events(task_id, rows)
        return rows

    def _redact_events(self, task_id: int, rows: list[dict]) -> list[dict]:
        """Process events as non-owners see them: masked, and calls that touch secrets hidden start to finish."""
        hidden = self._hidden_calls(task_id)
        in_key = bool(rows) and self._pem_open_before(task_id, rows[0]["id"])
        out = []
        for row in rows:
            if row["kind"] == "output":
                raw = row["content"] or ""
                if isinstance(row.get("payload"), dict) and "key_open" in row["payload"]:
                    in_key = bool(row["payload"].get("key_before"))
                row = redact.event(row, in_key)
                if in_key:
                    in_key = not redact._PEM_END.search(raw)
                in_key = in_key or redact.pem_open(raw)
            elif row["kind"] in PROCESS_KINDS:
                row = redact.event(row)
            elif row["kind"] in ("error", "status"):
                row = {**row, "content": redact.mask(row["content"] or "")}
            if row["kind"] == "tool" and (row["turn"], (row["payload"] or {}).get("call_id")) in hidden:
                row = {**row, "content": redact.HIDDEN, "payload": {**row["payload"], "summary": redact.HIDDEN}}
            out.append(row)
        return out

    def _hidden_calls(self, task_id: int) -> set:
        return redact.calls_hidden([
            {**r, "payload": _json_or_none(r["payload"])} for r in self.db.all(
                "SELECT turn, kind, content, payload FROM task_events WHERE task_id = ? AND kind = 'tool'", (task_id,))])

    def _key_marks(self, task_id: int, content: str) -> dict | None:
        """Whether a private key printed across output chunks is open before / after this chunk, stored
        with the chunk so hiding its body never depends on how far back a reader looks."""
        last = self.db.one("SELECT id, content, payload FROM task_events WHERE task_id = ? AND kind = 'output' "
                           "ORDER BY id DESC LIMIT 1", (task_id,))
        before = False
        if last:
            marks = _json_or_none(last["payload"]) or {}
            if "key_open" in marks:
                before = bool(marks["key_open"])
            else:  # recorded before marks existed: open if it began a key or continued one
                ended = redact._PEM_END.search(last["content"] or "")
                before = redact.pem_open(last["content"] or "") or (
                    not ended and self._pem_open_before(task_id, last["id"]))
        end = redact._PEM_END.search(content) if before else None
        after = (redact.pem_open(content[end.end():]) if end else True) if before else redact.pem_open(content)
        return {"key_before": True, "key_open": after} if before or after else {"key_open": False}

    def _in_key(self, task_id: int, row: dict) -> bool:
        """Whether this output chunk is (part of) a private key's body."""
        marks = row["payload"] if isinstance(row.get("payload"), dict) else (_json_or_none(row.get("payload")) or {})
        if "key_open" in marks:
            return bool(marks.get("key_before") or marks["key_open"])
        return redact.pem_open(row["content"] or "") or self._pem_open_before(task_id, row["id"])

    def _pem_open_before(self, task_id: int, before_id: int) -> bool:
        """Whether a private key printed in earlier output chunks is still open where this page starts."""
        last = self.db.one("SELECT payload FROM task_events WHERE task_id = ? AND id < ? AND kind = 'output' "
                           "ORDER BY id DESC LIMIT 1", (task_id, before_id))
        marks = _json_or_none(last["payload"]) if last else None
        if marks and "key_open" in marks:
            return bool(marks["key_open"])
        for r in self.db.all("SELECT content FROM task_events WHERE task_id = ? AND id < ? AND kind = 'output' "
                             "ORDER BY id DESC LIMIT 500", (task_id, before_id)):
            text = r["content"] or ""
            if redact._PEM_END.search(text) and not redact.pem_open(text):
                return False
            if redact.pem_open(text):
                return True
        return False

    def public_timeline(self, task_id: int) -> list[dict]:
        """The timeline as non-owners see it: tool spans kept, their commands and results redacted."""
        spans = []
        hidden = self._hidden_calls(task_id)
        for span in self.timeline(task_id):
            if span["type"] == "tool":
                span = dict(span)
                hide = (span["turn"], span.pop("call_id", None)) in hidden or redact.sensitive(span.get("summary", ""))
                span["summary"] = redact.HIDDEN if hide else redact.tool_text(span.get("summary", ""))
                if "result" in span:
                    span["result"] = redact.HIDDEN if hide else redact.tool_text(span["result"])
            elif span["type"] == "status":
                span = {**span, "text": redact.mask(span["text"]), "status": redact.mask(span["status"])}
            spans.append(span)
        return spans

    def _prompt_senders(self, task: dict) -> dict[int, str]:
        """Who sent each turn, for prompts recorded before the sender was stored: from the status notes."""
        senders = {1: task["from_username"]}
        for ev in self.db.all("SELECT turn, content FROM task_events WHERE task_id = ? AND kind = 'status'", (task["id"],)):
            m = re.match(r"^\w+: (\S+) 追加第 (\d+) 轮", ev["content"])
            if m:
                senders[int(m.group(2))] = m.group(1)
        return senders

    # ---- approval policy ----------------------------------------------

    def is_auto_approved(self, from_user_id: int, agent: dict) -> bool:
        if from_user_id == agent["user_id"]:
            return True
        rule = self.db.one(
            "SELECT id FROM approval_rules WHERE enabled = 1 AND owner_user_id = ? "
            "AND (from_user_id IS NULL OR from_user_id = ?) AND (agent_id IS NULL OR agent_id = ?) LIMIT 1",
            (agent["user_id"], from_user_id, agent["id"]),
        )
        return rule is not None

    # ---- task lifecycle -----------------------------------------------

    def _event(self, task_id: int, turn: int, kind: str, content: str, payload: dict | None = None) -> None:
        """`content` is the readable text (may be truncated); structured data lives whole in `payload`."""
        if len(content) > MAX_EVENT_CHARS:
            content = content[:MAX_EVENT_CHARS] + f"\n…（已截断，共 {len(content)} 字符）"
        self.db.execute(
            "INSERT INTO task_events (task_id, turn, kind, content, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, turn, kind, content, json.dumps(payload, ensure_ascii=False) if payload is not None else None, time.time()),
        )
        self._publish_task(task_id)

    def _set_status(self, task_id: int, turn: int, status: str, note: str = "", **fields: Any) -> None:
        now = time.time()
        fields["status"] = status
        fields["updated_at"] = now
        if status in FINISHED:
            fields["finished_at"] = now
            self.revoke_task_tokens(task_id)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*fields.values(), task_id))
        self._event(task_id, turn, "status", status + (f": {note}" if note else ""))
        if status in FINISHED and self.on_finished:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            for callback in self.on_finished:
                loop.create_task(callback(task_id))

    async def create_task(
        self,
        from_user: dict,
        target: str | int,
        prompt: str,
        title: str = "",
        from_agent: str = "",
        parent_task_id: int | None = None,
        workspace: str = "",
        group_id: int | None = None,
        group_message_id: int | None = None,
        workflow_step_id: int | None = None,
        on_created: Callable[[int], None] | None = None,
        images: list | None = None,
    ) -> dict:
        pictures = self.decode_images(images)
        prompt = (prompt or "").strip() or (IMAGE_EMPTY_PROMPT if pictures else "")
        if not prompt:
            raise HubError("任务内容不能为空")
        agent = self.resolve_agent(target)
        depth = 0
        if parent_task_id:
            parent = self.get_task(int(parent_task_id))
            if not self.is_party(from_user, parent):
                raise HubError("parent_task_id 不属于你参与的任务", 403)
            depth = parent["depth"] + 1
            if depth > MAX_DEPTH:
                raise HubError(f"任务委派层级超过上限 {MAX_DEPTH}，已拒绝以防智能体互相循环派单")
        workspace = self.check_workspace(from_user, agent, workspace)
        if parent_task_id:
            self.check_borrowing(from_user, self.get_task(int(parent_task_id)), agent, workspace)
        auto = self.is_auto_approved(from_user["id"], agent)
        now = time.time()
        task_id = self.db.execute(
            "INSERT INTO tasks (from_user_id, from_agent, agent_id, title, prompt, status, parent_task_id, depth, "
            "auto_approved, created_at, updated_at, workspace, group_id, group_message_id, workflow_step_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                from_user["id"],
                from_agent.strip(),
                agent["id"],
                (title or prompt.splitlines()[0])[:120],
                prompt,
                "queued" if auto else "pending_approval",
                parent_task_id,
                depth,
                int(auto),
                now,
                now,
                workspace,
                group_id,
                group_message_id,
                workflow_step_id,
            ),
        )
        if on_created:
            on_created(task_id)  # before it can be dispatched (and finish), so callers can tag it first
        self._event(task_id, 1, "prompt", prompt, {"by": from_user["username"]})
        self.save_images(task_id, 1, pictures)
        self._event(task_id, 1, "status", "queued: 免审批" if auto else "pending_approval: 等待对方审批")
        self.db.audit(from_user["id"], "task.create", task_id=task_id, agent=agent_address(agent), auto=auto)
        if auto:
            await self.dispatch(agent["connector_id"])
        return self.get_task(task_id)

    def workspaces_of(self, agent: dict) -> list[dict]:
        raw = agent.get("workspaces")
        items = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        return [{**w, "id": workspace_id(w["path"])} for w in items]

    def contact(self, user_id: int, agent_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM agent_contacts WHERE user_id = ? AND agent_id = ? AND status = 'accepted'", (user_id, agent_id)
        )

    def may_use_full(self, user_id: int, agent: dict) -> bool:
        """Whether this person may use the agent as it is now: isolated agents yes; a full-permission one only
        for its owner or someone the owner explicitly allowed (agent_contacts.allow_full)."""
        if agent.get("isolation") in ("sandbox", "limited") or user_id == agent["user_id"]:
            return True
        contact = self.contact(user_id, agent["id"])
        return bool(contact and contact["allow_full"])

    def granted_workspaces(self, user: dict, agent: dict) -> list[dict]:
        """Workspaces this user may use on the agent; raises when the user may not dispatch at all."""
        workspaces = self.workspaces_of(agent)
        if user["id"] == agent["user_id"]:
            return workspaces
        contact = self.contact(user["id"], agent["id"])
        if not contact:
            raise HubError(
                f"你还没有 {agent_address(agent)} 的使用权限：先加 {agent['username']} 为好友，再申请使用该智能体并等待对方同意", 403
            )
        if agent.get("isolation") not in ("sandbox", "limited") and not contact["allow_full"]:
            raise HubError(f"{agent_address(agent)} 以完全权限运行、未做工作区隔离，只有归属人本人和被归属人特别允许的人可以派任务", 403)
        granted = set(json.loads(contact["workspaces"] or "[]"))
        allowed = [w for w in workspaces if w["path"] in granted]
        if not allowed:
            raise HubError(f"{agent['username']} 还没有把 {agent_address(agent)} 的任何工作区授权给你", 403)
        return allowed

    def in_subtree(self, task_id: int, root_id: int) -> bool:
        """Whether the task was delegated, directly or further down, from the root task."""
        task = self.get_task(task_id)
        while task["parent_task_id"]:
            if task["parent_task_id"] == root_id:
                return True
            task = self.get_task(task["parent_task_id"])
        return False

    def check_chain_active(self, parent: dict) -> None:
        """Nobody up a delegation chain may be a disabled account: their old tasks must not keep driving new work."""
        task = parent
        while task:
            for uid in {task["from_user_id"], task.get("turn_user_id") or task["from_user_id"]}:
                who = self.get_user(uid)
                if not who or who["disabled"]:
                    raise HubError(f"任务 #{task['id']} 的发起人或追问者 {who['username'] if who else uid} 账号已停用，不能继续转派", 403)
            task = self.get_task(task["parent_task_id"]) if task["parent_task_id"] else None

    def check_borrowing(self, user: dict, parent: dict, agent: dict, workspace: str) -> None:
        """A sub-task must stay within what every requester up the delegation chain may use on the agent:
        relaying through more of the owner's own agents must not widen a colleague's access. Handing work to
        one's own agents stays allowed; the check applies when the chain reaches someone else's agent."""
        self.check_chain_active(parent)  # even handing work to one's own agents needs a live chain
        if agent["user_id"] == user["id"]:
            return
        seen, task = {user["id"], agent["user_id"]}, parent
        while task:
            # who started each task up the chain, and whoever asked its current turn (a share may let others ask)
            askers = [task["from_user_id"]] + ([task["turn_user_id"]] if task.get("turn_user_id") else [])
            for asker in askers:
                if asker in seen:
                    continue
                seen.add(asker)
                requester = self.get_user(asker)
                try:
                    if not requester or requester["disabled"]:
                        raise HubError("账号已停用")
                    allowed = self.granted_workspaces(requester, agent)
                except HubError:
                    raise HubError(
                        f"任务 #{task['id']} 由 {requester['username']} 发起或追问，他本人没有 {agent_address(agent)} 的使用权限，"
                        "不能借你的权限转派", 403
                    ) from None
                if workspace not in [w["path"] for w in allowed]:
                    raise HubError(
                        f"任务 #{task['id']} 由 {requester['username']} 发起或追问，他没有 {agent_address(agent)} 这个工作区的权限，"
                        "不能借你的权限转派", 403
                    )
            task = self.get_task(task["parent_task_id"]) if task["parent_task_id"] else None

    def check_workspace(self, user: dict, agent: dict, workspace: str) -> str:
        """Map the requested workspace (id, or path for the owner) to a folder this user may use."""
        workspace = (workspace or "").strip()
        allowed = self.granted_workspaces(user, agent)
        owner = user["id"] == agent["user_id"]
        if not workspace:
            if owner:
                return ""
            if len(allowed) == 1:
                return allowed[0]["path"]
            raise HubError("请选择工作区：" + "、".join(w["name"] for w in allowed))
        for w in allowed:
            if workspace == w["id"] or workspace == w["name"] or (owner and workspace == w["path"]):
                return w["path"]
        raise HubError(f"工作区 {workspace} 不存在或未授权给你")

    def present_task(self, user: dict, task: dict) -> dict:
        """Hide folder paths from everyone but the agent's owner; they see the workspace name."""
        if task.get("workspace") and user["id"] != task["owner_user_id"] and not user.get("is_admin"):
            agent = self.resolve_agent(task["agent_id"])
            name = next((w["name"] for w in self.workspaces_of(agent) if w["path"] == task["workspace"]), "已移除的工作区")
            task = {**task, "workspace": name}
        sees = self.sees_process(user, task)
        if not sees and task.get("error"):
            task = {**task, "error": redact.mask(task["error"])}
        if not sees and task.get("code_ref"):
            ref = task["code_ref"] if isinstance(task["code_ref"], dict) else (_json_or_none(task["code_ref"]) or {})
            task = {**task, "code_ref": public_code_ref(ref)}
        return {**task, "sees_process": sees}

    def set_hidden(self, user: dict, agent_id: int, hidden: bool) -> None:
        agent = self.resolve_agent(agent_id)
        if agent["user_id"] != user["id"]:
            raise HubError("只能设置自己智能体的可见性", 403)
        self.db.execute("UPDATE agents SET hidden = ? WHERE id = ?", (int(bool(hidden)), agent["id"]))
        self.db.audit(user["id"], "agent.visibility", agent=agent_address(agent), hidden=bool(hidden))

    async def set_workspaces(self, user: dict, agent_id: int, workspaces: list) -> None:
        agent = self.resolve_agent(agent_id)
        if agent["user_id"] != user["id"]:
            raise HubError("只能管理自己智能体的工作区", 403)
        clean, seen = [], set()
        for item in workspaces or []:
            path = str((item or {}).get("path", "")).strip()
            if not path or path in seen:
                continue
            seen.add(path)
            clean.append({"name": str(item.get("name") or path.rstrip("/\\").split("/")[-1].split("\\")[-1] or path)[:60], "path": path[:500]})
        conn = self.connections.get(agent["connector_id"])
        if not conn:
            raise HubError("智能体所在连接器离线，工作区需由连接器在本机校验后保存，请等其上线后再改", 409)
        await conn.send({"type": "set_workspaces", "agent_key": agent["key"], "workspaces": clean})
        self.db.audit(user["id"], "agent.workspaces", agent_id=agent_id, workspaces=[w["path"] for w in clean])

    async def approve(self, user: dict, task_id: int, approve: bool) -> dict:
        task = self.get_task(task_id)
        if user["id"] != task["owner_user_id"]:
            raise HubError("只有智能体归属人本人可以审批", 403)
        if task["status"] != "pending_approval":
            raise HubError(f"任务当前状态为 {task['status']}，无需审批")
        if approve:
            self._set_status(task_id, task["turn"], "queued", f"{user['username']} 已批准", decided_by=user["id"])
        else:
            self._set_status(task_id, task["turn"], "rejected", f"{user['username']} 已拒绝", decided_by=user["id"])
        self.db.audit(user["id"], "task.approve" if approve else "task.reject", task_id=task_id)
        if approve:
            await self.dispatch(task["agent_connector_id"])
        return self.get_task(task_id)

    def descendants(self, task_id: int) -> list[dict]:
        """Every task below this one, parents before children."""
        found, frontier = [], [task_id]
        while frontier and len(found) < 500:
            marks = ",".join("?" * len(frontier))
            rows = self.db.all(f"SELECT id, status, parent_task_id FROM tasks WHERE parent_task_id IN ({marks}) ORDER BY id", frontier)
            found += rows
            frontier = [r["id"] for r in rows]
        return found

    async def _stop(self, user: dict, task: dict, note: str) -> None:
        """Cancel one task. Once it reached a connector, it counts as stopped only when the connector confirms:
        a finish report, or a reconnect that no longer lists it. Until then `stop_pending` stays set."""
        reached = task["status"] in ("dispatched", "running")
        sent = False
        if reached:
            conn = self.connections.get(task["connector_id"])
            if conn:
                try:
                    await conn.send({"type": "cancel", "task_id": task["id"]})
                    sent = True
                except Exception:  # noqa: BLE001 - resent when the connector reconnects
                    LOGGER.warning("cancel send failed task=%s", task["id"])
            if not sent:
                note += "（连接器离线，重连后补发停止并确认）"
        self._set_status(task["id"], task["turn"], "canceled", note, stop_pending=int(reached))
        if reached:
            self._event(task["id"], task["turn"], "status",
                        "stopping: 已通知连接器结束进程，等待确认" if sent else "stopping: 连接器离线，重连后补发停止")

    async def resend_cancels(self, connector_id: int, running: set[int]) -> None:
        """After a reconnect: stop again whatever was canceled while the connector could not hear it."""
        conn = self.connections.get(connector_id)
        if not conn:
            return
        for row in self.db.all("SELECT id, turn FROM tasks WHERE connector_id = ? AND stop_pending = 1", (connector_id,)):
            if row["id"] in running:
                try:
                    await conn.send({"type": "cancel", "task_id": row["id"]})
                    self._event(row["id"], row["turn"], "status", "stopping: 连接器重连，已补发停止")
                except Exception:  # noqa: BLE001
                    LOGGER.warning("cancel resend failed task=%s", row["id"])

    async def cancel(self, user: dict, task_id: int, tree: bool = False) -> dict:
        task = self.get_task(task_id)
        if not self.is_party(user, task):
            raise HubError("只有发起人或智能体归属人可以取消", 403)
        if task["status"] not in ACTIVE and not tree:
            raise HubError(f"任务已结束（{task['status']}），无法取消")
        stopped = []
        if task["status"] in ACTIVE:
            await self._stop(user, task, f"{user['username']} 已取消")
            stopped.append(task_id)
        if tree:
            # sub-tasks exist to serve this task, so whoever may cancel it may stop the work it spawned
            for row in self.descendants(task_id):
                if row["status"] in ACTIVE:
                    await self._stop(user, self.get_task(row["id"]), f"{user['username']} 取消了上级任务 #{task_id}")
                    stopped.append(row["id"])
        if not stopped:
            raise HubError("这棵任务树里没有进行中的任务")
        self.db.audit(user["id"], "task.cancel", task_id=task_id, tree=tree, stopped=stopped)
        return self.get_task(task_id)

    async def follow_up(self, user: dict, task_id: int, message: str, images: list | None = None) -> dict:
        task = self.get_task(task_id)
        share = None if self.is_party(user, task) else self.share_of(user, task_id)
        if not self.is_party(user, task) and not (share and share["can_continue"]):
            raise HubError("只有发起人、智能体归属人或被分享可继续对话的人可以继续对话", 403)
        pictures = self.decode_images(images)
        message = (message or "").strip() or (IMAGE_EMPTY_PROMPT if pictures else "")
        if not message:
            raise HubError("追加内容不能为空")
        if task["status"] not in ("completed", "failed"):
            raise HubError(f"任务状态为 {task['status']}，只有已完成或失败的任务可以继续对话")
        agent = self.resolve_agent(task["agent_id"])
        if share:
            if user["id"] != agent["user_id"] and not self.may_use_full(user["id"], agent):
                raise HubError(f"{agent_address(agent)} 以完全权限运行、未做工作区隔离，只有归属人本人和被归属人特别允许的人可以派任务", 403)
            # a shared conversation stays usable without a workspace grant, but only in its own folder
            if task["workspace"] and task["workspace"] not in [w["path"] for w in self.workspaces_of(agent)]:
                raise HubError("该对话所在的工作区已被归属人移除，无法继续", 403)
            auto = bool(share["auto_approve"]) or self.is_auto_approved(user["id"], agent)
        else:
            if user["id"] != agent["user_id"]:
                allowed = [w["path"] for w in self.granted_workspaces(user, agent)]
                if task["workspace"] not in allowed:
                    raise HubError("该对话所在的工作区已不再授权给你，无法继续", 403)
            auto = self.is_auto_approved(user["id"], agent)
        turn = task["turn"] + 1
        self.db.execute(
            "UPDATE tasks SET turn = ?, pending_message = ?, error = '', finished_at = NULL, decided_by = NULL, "
            "auto_approved = ?, turn_user_id = ?, updated_at = ? WHERE id = ?",
            (turn, message, int(auto), user["id"], time.time(), task_id),
        )
        self._event(task_id, turn, "prompt", message, {"by": user["username"]})
        self.save_images(task_id, turn, pictures)
        if auto:
            self._set_status(task_id, turn, "queued", f"{user['username']} 追加第 {turn} 轮")
        else:
            self._set_status(task_id, turn, "pending_approval", f"{user['username']} 追加第 {turn} 轮，等待审批")
        self.db.audit(user["id"], "task.follow_up", task_id=task_id, turn=turn, auto=auto)
        if auto:
            await self.dispatch(agent["connector_id"])
        return self.get_task(task_id)

    def _history(self, task_id: int, current_turn: int) -> list[dict]:
        turns: dict[int, dict] = {}
        for ev in self.db.all(
            "SELECT turn, kind, content FROM task_events WHERE task_id = ? AND kind IN ('prompt', 'result', 'error') "
            "AND turn < ? ORDER BY id",
            (task_id, current_turn),
        ):
            item = turns.setdefault(ev["turn"], {"prompt": "", "result": ""})
            if ev["kind"] == "prompt":
                item["prompt"] = ev["content"]
            else:
                item["result"] = ev["content"]
        return [turns[k] for k in sorted(turns)]

    def _payload(self, task: dict) -> dict:
        prompt = task["prompt"] if task["turn"] == 1 else (task["pending_message"] or "")
        sender = task["from_username"] + (f"/{task['from_agent']}" if task["from_agent"] else "")
        if task["turn"] > 1 and task.get("turn_user_id") and task["turn_user_id"] != task["from_user_id"]:
            sender = self.get_user(task["turn_user_id"])["username"]
        # the person asking this turn may be someone the task was shared with, not who started it
        asker = task.get("turn_user_id") if task["turn"] > 1 and task.get("turn_user_id") else task["from_user_id"]
        # a 「工作」session runs in the colleague's own container, not on the owner's machine: nothing to keep from them
        if task.get("source") != "work" and (
                asker != task["owner_user_id"] or task["from_user_id"] != task["owner_user_id"] or task.get("group_id")):
            prompt = CONFIDENTIAL_NOTE + prompt
        return {
            "id": task["id"],
            "turn": task["turn"],
            "agent_key": task["agent_key"],
            "title": task["title"],
            "prompt": prompt,
            "from": sender,
            "depth": task["depth"],
            "session_id": task["session_id"],
            "history": self._history(task["id"], task["turn"]) if task["turn"] > 1 else [],
            "workspace": task["workspace"],
            "execution_id": task["execution_id"],
            "images": self._turn_images(task["id"], task["turn"]),
        }

    # ---- task tokens: let a running agent call the hub's MCP tools ------

    def issue_task_token(self, task: dict) -> str:
        """A token acting as the agent's owner, valid only while this task runs."""
        self.revoke_task_tokens(task["id"])
        return create_api_token(self.db, task["owner_user_id"], "task", f"task:{task['id']}")[1]

    def revoke_task_tokens(self, task_id: int) -> None:
        self.db.execute(
            "UPDATE api_tokens SET revoked = 1 WHERE kind = 'task' AND name = ? AND revoked = 0", (f"task:{task_id}",)
        )

    def _revoked_since_queued(self, task: dict) -> str:
        """Why a queued task may no longer run: access can be revoked while it waits for an offline connector."""
        agent = self.resolve_agent(task["agent_id"])
        asker_id = task["turn_user_id"] if task["turn"] > 1 and task.get("turn_user_id") else task["from_user_id"]
        try:
            if task["parent_task_id"]:
                self.check_chain_active(self.get_task(task["parent_task_id"]))
        except HubError as exc:
            return f"排队期间权限已变更，任务未执行：{exc}"
        for uid in {asker_id, task["from_user_id"]}:
            # the turn's asker and the task's original requester, even when the owner asks the latest turn
            who = self.get_user(uid)
            if not who or who["disabled"]:
                return f"排队期间权限已变更，任务未执行：{who['username'] if who else '发起人'} 的账号已停用"
        if asker_id == agent["user_id"] or task.get("source") == "work":
            return ""  # a 「工作」session runs in the asker's own container; work.py checks who may use it
        asker = self.get_user(asker_id)
        try:
            share = self.share_of(asker, task["id"])
            if asker_id != task["from_user_id"]:
                # a later turn by someone outside the conversation rests on the share alone, as in follow_up
                if not share or not share["can_continue"]:
                    raise HubError("对话分享已被取消或改为只读")
                if not self.may_use_full(asker_id, agent):
                    raise HubError(f"{agent_address(agent)} 已改为完全权限运行，只有归属人本人和被特别允许的人可以派任务")
                if task["workspace"] and task["workspace"] not in [w["path"] for w in self.workspaces_of(agent)]:
                    raise HubError("该对话所在的工作区已被归属人移除")
            else:
                allowed = [w["path"] for w in self.granted_workspaces(asker, agent)]
                if task["workspace"] and task["workspace"] not in allowed:
                    raise HubError("该工作区已不再授权给发起人")
            if task["parent_task_id"]:
                # a delegated task must still fit what everyone up the chain may use
                self.check_borrowing(asker, self.get_task(task["parent_task_id"]), agent, task["workspace"])
        except HubError as exc:
            return f"排队期间权限已变更，任务未执行：{exc}"
        return ""

    def fail_waiting_tasks_of(self, user_id: int) -> None:
        """A disabled account's not-yet-started turns end now, so re-enabling it later never revives them."""
        reason = "发起人或追问者账号已停用，任务未执行"
        for t in self.db.all(
            "SELECT id, turn FROM tasks WHERE status IN ('pending_approval', 'queued') AND "
            "(from_user_id = ? OR (turn > 1 AND turn_user_id = ?))", (user_id, user_id),
        ):
            self._set_status(t["id"], t["turn"], "failed", reason, error=reason)

    def _still_auto(self, task: dict) -> bool:
        agent = self.resolve_agent(task["agent_id"])
        asker = task["turn_user_id"] if task["turn"] > 1 and task.get("turn_user_id") else task["from_user_id"]
        share = self.db.one("SELECT auto_approve FROM task_shares WHERE task_id = ? AND user_id = ?", (task["id"], asker))
        return bool(share and share["auto_approve"]) or self.is_auto_approved(asker, agent)

    async def dispatch(self, connector_id: int) -> None:
        conn = self.connections.get(connector_id)
        if not conn:
            return
        rows = self.db.all(
            "SELECT t.id FROM tasks t JOIN agents a ON a.id = t.agent_id "
            "WHERE a.connector_id = ? AND t.status = 'queued' ORDER BY t.id",
            (connector_id,),
        )
        for row in rows:
            # claim atomically so concurrent dispatch calls never send a task twice
            claimed = self.db.update(
                "UPDATE tasks SET status = 'dispatched', connector_id = ?, updated_at = ?, execution_id = ?, "
                "attempts = attempts + 1 WHERE id = ? AND status = 'queued'",
                (connector_id, time.time(), uuid.uuid4().hex, row["id"]),
            )
            if not claimed:
                continue
            task = self.get_task(row["id"])
            denied = self._revoked_since_queued(task) or next(
                (why for guard in self.dispatch_guards if (why := guard(task))), "")
            if denied:
                self._set_status(task["id"], task["turn"], "failed", denied, error=denied)
                continue
            if task["auto_approved"] and not task.get("decided_by") and not self._still_auto(task):
                # skipping approval was granted when it queued and has since been withdrawn
                self.db.execute("UPDATE tasks SET auto_approved = 0 WHERE id = ?", (task["id"],))
                self._set_status(task["id"], task["turn"], "pending_approval", "免审批已被撤销，等待审批")
                continue
            self._event(task["id"], task["turn"], "status", "dispatched")
            payload = self._payload(task)
            if task["agent_kind"] in MCP_KINDS:
                payload["mcp_token"] = self.issue_task_token(task)
            for hook in self.payload_hooks:
                hook(task, payload)
            try:
                await conn.send({"type": "task", "task": payload})
            except Exception:  # noqa: BLE001
                if self.db.update(
                    "UPDATE tasks SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'dispatched'",
                    (time.time(), task["id"]),
                ):
                    self._event(task["id"], task["turn"], "status", "queued: 下发失败，等待连接器重连")
                return

    # ---- connector updates -------------------------------------------

    def update_notice(self, connector_id: int) -> dict | None:
        row = self.db.one("SELECT os, version FROM connectors WHERE id = ?", (connector_id,))
        if not row or not self.update_check:
            return None
        info = self.update_check(row["os"], row["version"])
        return {"type": "update_available", **info} if info.get("update_available") else None

    async def notify_updates(self, platform: str) -> None:
        for connector_id, conn in list(self.connections.items()):
            notice = self.update_notice(connector_id)
            if notice and notice["platform"] == platform:
                try:
                    await conn.send(notice)
                except Exception:  # noqa: BLE001
                    pass

    # ---- live load and wait reasons --------------------------------------

    def agent_load(self, agent_id: int, online: bool) -> dict:
        """What the agent is doing now. Connector telemetry wins; older connectors fall back to the hub's own view."""
        hub_running = self.db.one(
            "SELECT COUNT(*) AS n FROM tasks WHERE agent_id = ? AND status = 'running'", (agent_id,)
        )["n"]
        hub_queued = self.db.one(
            "SELECT COUNT(*) AS n, MIN(updated_at) AS oldest FROM tasks WHERE agent_id = ? AND status IN ('queued', 'dispatched')",
            (agent_id,),
        )
        live = self.telemetry.get(agent_id) if online else None
        if live and time.time() - live["at"] > TELEMETRY_TTL:
            live = None  # stale: the connector stopped reporting; fall back to the hub's own view
        if live:
            lock_wait = live.get("lock_wait")
            if lock_wait and not self.lock_holder(int(lock_wait.get("task_id") or 0)):
                lock_wait = None  # expired, or that task has started since
            return {"source": "connector", "running": live["running"], "queued": live["queued"],
                    "oldest_wait_s": live["oldest_wait_s"], "lock_wait": lock_wait, "at": live["at"]}
        oldest = round(time.time() - hub_queued["oldest"]) if hub_queued["oldest"] else 0
        return {"source": "hub", "running": hub_running, "queued": hub_queued["n"], "oldest_wait_s": oldest,
                "lock_wait": None, "at": time.time()}

    def _telemetry(self, connector_id: int, msg: dict) -> None:
        now = time.time()
        rows = {r["key"]: r["id"] for r in self.db.all("SELECT id, key FROM agents WHERE connector_id = ?", (connector_id,))}
        owners = set()
        for item in msg.get("agents") or []:
            if not isinstance(item, dict) or item.get("key") not in rows:
                continue
            agent_id = rows[item["key"]]
            queued = [int(i) for i in item.get("queued") or [] if str(i).isdigit()][:200]
            running_id = item.get("running")
            lock_wait = item.get("lock_wait") if isinstance(item.get("lock_wait"), dict) else None
            self.telemetry[agent_id] = {
                "running": 1 if running_id else 0,
                "running_task": int(running_id) if str(running_id or "").isdigit() else None,
                "queued": len(queued),
                "queued_tasks": queued,
                "oldest_wait_s": max(0, int(item.get("oldest_wait_s") or 0)),
                "lock_wait": lock_wait,
                "at": now,
            }
            if lock_wait and str(lock_wait.get("task_id", "")).isdigit() and str(lock_wait.get("holder_task_id", "")).isdigit():
                self.lock_waits[int(lock_wait["task_id"])] = (int(lock_wait["holder_task_id"]), now)
            owners.add(agent_id)
        # a task no longer reported as lock-waiting has its lock
        reported = {int(a["lock_wait"]["task_id"]) for a in (self.telemetry.get(i) for i in rows.values())
                    if a and a.get("lock_wait") and str(a["lock_wait"].get("task_id", "")).isdigit()}
        for task_id in [t for t in self.lock_waits if t not in reported]:
            if self.db.one("SELECT 1 FROM tasks WHERE id = ? AND connector_id = ?", (task_id, connector_id)):
                self.lock_waits.pop(task_id, None)
        if owners:
            self.publish("tasks")

    def lock_holder(self, task_id: int) -> int | None:
        """The task holding the workspace this one waits for, if the connector said so recently.
        Load display, wait reasons and deadlock checks all use this, so they never disagree."""
        entry = self.lock_waits.get(task_id)
        if not entry:
            return None
        holder, at = entry
        if time.time() - at > TELEMETRY_TTL:
            self.lock_waits.pop(task_id, None)  # stale: not evidence of a wait any more
            return None
        return holder

    def wait_reason(self, task: dict, last_event: dict | None = None) -> dict:
        """Why a task is not finished yet, from facts the hub has. `code` is stable; `text` is for people."""
        status = task["status"]
        if task.get("stop_pending"):
            if task.get("connector_id", task.get("agent_connector_id")) not in self.connections:
                return {"code": "stopping", "text": "已取消，连接器离线，重连后补发停止并确认"}
            return {"code": "stopping", "text": "已取消，等待连接器确认进程已停止"}
        if status in FINISHED:
            return {"code": "finished", "text": ""}
        if status == "pending_approval":
            return {"code": "approval", "text": f"等待 {task.get('owner_username', '归属人')} 审批"}
        connector_online = task.get("agent_connector_id") in self.connections
        if status == "queued":
            if not connector_online:
                return {"code": "offline", "text": "智能体所在连接器离线，上线后自动下发"}
            ahead = self.db.one(
                "SELECT COUNT(*) AS n FROM tasks WHERE agent_id = ? AND status IN ('queued', 'dispatched', 'running') AND id < ?",
                (task["agent_id"], task["id"]),
            )["n"]
            return {"code": "queued", "text": f"排队中，前面还有 {ahead} 个任务" if ahead else "即将下发"}
        holder = self.lock_holder(task["id"])
        if holder:
            return {"code": "workspace_lock", "text": f"等待工作区锁（被任务 #{holder} 占用）", "task_id": holder}
        if status == "dispatched":
            if not connector_online:
                return {"code": "offline", "text": "已下发，连接器断开，重连后核对"}
            live = self.telemetry.get(task["agent_id"])
            if live and task["id"] in live.get("queued_tasks", []):
                position = live["queued_tasks"].index(task["id"])
                return {"code": "queued", "text": f"在连接器队列中，前面还有 {position + (1 if live['running'] else 0)} 个"}
            return {"code": "starting", "text": "已下发，等待连接器开始执行"}
        waiting = sorted(self.waits.get(task["id"], ()))
        if waiting:
            return {"code": "waiting_task", "text": "等待子任务 " + "、".join(f"#{i}" for i in waiting), "task_id": waiting[0]}
        if not connector_online:
            return {"code": "offline", "text": "连接器断开，执行状态待重连核对"}
        if last_event is None:
            last_event = self.last_activity(task["id"])
        if last_event and last_event.get("open_tool"):
            return {"code": "tool", "text": f"正在执行 {last_event['open_tool']}"}
        quiet = time.time() - (last_event["at"] if last_event else task["updated_at"])
        if quiet >= QUIET_SECONDS:
            return {"code": "quiet", "text": f"{int(quiet)} 秒无新事件（不代表卡死）", "seconds": int(quiet)}
        return {"code": "running", "text": "执行中"}

    def last_activity(self, task_id: int) -> dict | None:
        """The latest progress line of a task, and the tool call still open in the current turn if any."""
        row = self.db.one(
            "SELECT id, kind, content, payload, created_at, turn FROM task_events WHERE task_id = ? "
            "AND kind IN ('output', 'tool', 'result', 'error') ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        if not row:
            return None
        payload = _json_or_none(row["payload"])
        text = payload["summary"] if row["kind"] == "tool" and payload else row["content"]
        line = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
        activity = {"kind": row["kind"], "summary": line[:160], "at": row["created_at"]}
        if row["kind"] == "tool" and payload:
            activity["tool"] = payload["name"]
            if payload.get("sensitive") or (row["turn"], payload.get("call_id")) in self._hidden_calls(task_id):
                activity["hidden"] = True
        elif row["kind"] == "output" and self._in_key(task_id, row):
            activity["hidden"] = True
        # what non-owners see, masked on the whole text before trimming so a cut never splits a value from its key
        public = redact.tool_text(text) if row["kind"] == "tool" else redact.mask(text)
        activity["public_summary"] = next((ln.strip() for ln in reversed(public.splitlines()) if ln.strip()), "")[:160]
        open_tools: dict[str, str] = {}
        for ev in self.db.all(
            "SELECT payload FROM task_events WHERE task_id = ? AND turn = ? AND kind = 'tool' ORDER BY id",
            (task_id, row["turn"]),
        ):
            p = _json_or_none(ev["payload"]) or {}
            if p.get("phase") == "started":
                open_tools[p.get("call_id", "")] = p.get("name", "")
            else:
                open_tools.pop(p.get("call_id", ""), None)
        if open_tools:
            activity["open_tool"] = list(open_tools.values())[-1]
        return activity

    # ---- connector protocol -------------------------------------------

    def register_connector(self, token_row: dict, hello: dict, remote_ip: str = "") -> int:
        info = hello.get("connector") or {}
        now = time.time()
        connector = self.db.one("SELECT id FROM connectors WHERE token_id = ?", (token_row["id"],))
        name = str(info.get("name") or info.get("hostname") or token_row["name"])[:80]
        # addresses the connector reports for its own network interfaces (informational only)
        local_ips = [str(ip)[:45] for ip in info.get("local_ips") or [] if isinstance(ip, str)][:8]
        values = (
            name, str(info.get("hostname", ""))[:120], str(info.get("os", ""))[:40], str(info.get("version", ""))[:40], now,
            remote_ip[:45], json.dumps(local_ips), now,
        )
        if connector:
            connector_id = connector["id"]
            self.db.execute(
                "UPDATE connectors SET name = ?, hostname = ?, os = ?, version = ?, online = 1, last_seen_at = ?, "
                "remote_ip = ?, local_ips = ?, connected_at = ? WHERE id = ?",
                (*values, connector_id),
            )
        else:
            connector_id = self.db.execute(
                "INSERT INTO connectors (name, hostname, os, version, online, last_seen_at, remote_ip, local_ips, "
                "connected_at, user_id, token_id) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (*values, token_row["user_id"], token_row["id"]),
            )
        workhost = info.get("workhost") if isinstance(info.get("workhost"), dict) else {}
        self.db.execute("UPDATE connectors SET workhost = ? WHERE id = ?", (json.dumps(
            {"enabled": workhost.get("enabled") is True, "docker": workhost.get("docker") is True,
             "image": str(workhost.get("image") or "")[:200]}), connector_id))
        self._forget_session(connector_id)
        keys = self._upsert_agents(connector_id, token_row["user_id"], hello.get("agents") or [])
        known = [*(hello.get("running") or []), *(hello.get("finished") or [])]
        self._reconcile(connector_id, {int(i) for i in known if str(i).isdigit()})
        self.db.audit(token_row["user_id"], "connector.online", connector_id=connector_id, agents=keys, ip=remote_ip)
        return connector_id

    def _upsert_agents(self, connector_id: int, user_id: int, items: list) -> list[str]:
        now = time.time()
        keys = []
        for item in items:
            key = str(item.get("key", "")).strip()
            if not key or "/" in key:
                continue
            keys.append(key)
            fields = (
                str(item.get("name") or key)[:80],
                str(item.get("kind") or "command")[:40],
                str(item.get("description") or "")[:2000],
                json.dumps([str(t) for t in item.get("tags") or []][:20], ensure_ascii=False),
                str(item.get("workdir") or "")[:500],
                json.dumps(_workspaces(item), ensure_ascii=False),
                item.get("isolation") if item.get("isolation") in ISOLATIONS else "none",
                json.dumps(clean_capabilities(item.get("capabilities"))),
                now,
            )
            existing = self.db.one(
                "SELECT id FROM agents WHERE user_id = ? AND key = ?", (user_id, key)
            )
            if existing:
                self.db.execute(
                    "UPDATE agents SET name = ?, kind = ?, description = ?, tags = ?, workdir = ?, workspaces = ?, isolation = ?, capabilities = ?, updated_at = ?, "
                    "connector_id = ?, online = 1 WHERE id = ?",
                    (*fields, connector_id, existing["id"]),
                )
            else:
                self.db.execute(
                    "INSERT INTO agents (name, kind, description, tags, workdir, workspaces, isolation, capabilities, updated_at, user_id, connector_id, key, online) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (*fields, user_id, connector_id, key),
                )
            # also offered to the 「工作」menu; it stays an ordinary agent for everyone it was shared with
            self.db.execute("UPDATE agents SET workhost = ? WHERE user_id = ? AND key = ?",
                            (int(item.get("workhost") is True), user_id, key))
        marks = ",".join("?" * len(keys)) or "''"
        self.db.execute(
            f"UPDATE agents SET online = 0 WHERE connector_id = ? AND key NOT IN ({marks})", (connector_id, *keys)
        )
        return keys

    def _reconcile(self, connector_id: int, running: set[int]) -> None:
        for row in self.db.all(
            "SELECT id, turn FROM tasks WHERE connector_id = ? AND stop_pending = 1", (connector_id,)
        ):
            if row["id"] not in running:
                self.db.execute("UPDATE tasks SET stop_pending = 0 WHERE id = ?", (row["id"],))
                self._event(row["id"], row["turn"], "status", "canceled: 连接器重连后确认进程已不在运行")
        rows = self.db.all(
            "SELECT t.id, t.turn, t.status FROM tasks t "
            "WHERE t.connector_id = ? AND t.status IN ('dispatched', 'running')",
            (connector_id,),
        )
        for row in rows:
            if row["id"] in running:
                continue
            if row["status"] == "dispatched":
                self._set_status(row["id"], row["turn"], "queued", "连接器重连，重新下发")
            else:
                self._set_status(row["id"], row["turn"], "failed", "连接器重启，执行中断", error="连接器重启，执行中断")

    def _forget_session(self, connector_id: int) -> None:
        """Live figures belong to one connection; drop them when it ends or is replaced."""
        for row in self.db.all("SELECT id FROM agents WHERE connector_id = ?", (connector_id,)):
            self.telemetry.pop(row["id"], None)
        for task_id in [t for t in self.lock_waits
                        if self.db.one("SELECT 1 FROM tasks WHERE id = ? AND connector_id = ?", (t, connector_id))]:
            self.lock_waits.pop(task_id, None)

    def connector_offline(self, connector_id: int) -> None:
        self._forget_session(connector_id)
        self.connections.pop(connector_id, None)
        self.db.execute("UPDATE connectors SET online = 0, last_seen_at = ? WHERE id = ?", (time.time(), connector_id))
        self.db.execute("UPDATE agents SET online = 0 WHERE connector_id = ?", (connector_id,))

    def _owned_task(self, connector_id: int, task_id: Any) -> dict | None:
        try:
            task = self.get_task(int(task_id))
        except (HubError, TypeError, ValueError):
            return None
        return task if task["connector_id"] == connector_id else None

    async def handle_connector_message(self, connector_id: int, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ping":
            self.db.execute("UPDATE connectors SET last_seen_at = ? WHERE id = ?", (time.time(), connector_id))
            conn = self.connections.get(connector_id)
            if conn:
                await conn.send({"type": "pong"})
            return
        if kind == "rpc_result":
            pending = self._requests.get(str(msg.get("request_id")))
            if pending and pending[0] == connector_id and not pending[1].done():
                pending[1].set_result(msg)
            return
        if kind == "agents":
            row = self.db.one("SELECT user_id FROM connectors WHERE id = ?", (connector_id,))
            if row and isinstance(msg.get("agents"), list):
                self._upsert_agents(connector_id, row["user_id"], msg["agents"])
                self.publish(f"user:{row['user_id']}")
            return
        if kind == "telemetry":
            self._telemetry(connector_id, msg)
            return
        msg_id = str(msg.get("msg_id") or "")[:80]
        if not msg_id:
            self._apply_report(connector_id, kind, msg)
            return
        # a persisted report is applied and marked seen in one transaction, and acknowledged only after it
        # committed: a crash before the commit leaves both undone, so the connector's replay applies it then
        if not self.db.one("SELECT 1 FROM connector_messages WHERE msg_id = ?", (msg_id,)):
            with self.db.transaction():
                self._apply_report(connector_id, kind, msg)
                self.db.execute(
                    "INSERT INTO connector_messages (msg_id, connector_id, created_at) VALUES (?, ?, ?)",
                    (msg_id, connector_id, time.time()),
                )
        conn = self.connections.get(connector_id)
        if conn:
            try:
                await conn.send({"type": "ack", "msg_id": msg_id})
            except Exception:  # noqa: BLE001 - the connector resends unacknowledged reports
                pass

    def _apply_report(self, connector_id: int, kind: str, msg: dict) -> None:
        task = self._owned_task(connector_id, msg.get("task_id"))
        if not task:
            return
        turn = task["turn"]
        if "turn" in msg and msg["turn"] != turn:
            return  # late report from an earlier turn
        execution = str(msg.get("execution_id") or "")
        if execution and task["execution_id"] and execution != task["execution_id"]:
            return  # report from an older attempt of this turn; it must not overwrite the current one
        if kind == "task_started":
            self.lock_waits.pop(task["id"], None)  # it runs, so it holds its workspace now
            live = self.telemetry.get(task["agent_id"])
            if live and task["id"] in live.get("queued_tasks", []):
                # until the next report, count it as running rather than queued
                live["queued_tasks"] = [t for t in live["queued_tasks"] if t != task["id"]]
                live.update(queued=len(live["queued_tasks"]), running=1, running_task=task["id"], lock_wait=None)
            if task["status"] == "dispatched":
                self._set_status(task["id"], turn, "running")
        elif kind == "task_event":
            event_kind = str(msg.get("kind") or "output")[:20]
            payload = None
            if event_kind == "tool":
                payload = clean_tool_payload(msg.get("payload"))
                if payload is None:
                    event_kind = "output"  # malformed structure: keep the readable text only
            content = str(msg.get("content") or (payload or {}).get("summary") or "")
            if event_kind == "output":
                payload = self._key_marks(task["id"], content)
            self._event(task["id"], turn, event_kind, content, payload)
            self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (time.time(), task["id"]))
        elif kind == "task_attachment":
            self._attachment(task["id"], turn, msg)
        elif kind == "task_done":
            result = str(msg.get("result") or "")
            error = str(msg.get("error") or "")
            session_id = str(msg.get("session_id") or task["session_id"] or "")
            usage = clean_usage(msg.get("usage"))
            code_ref = msg.get("code_ref") if isinstance(msg.get("code_ref"), dict) else None
            if usage:
                self._event(task["id"], turn, "usage", _usage_text(usage), usage)
            if code_ref:
                code_ref = {k: str(v)[:200] for k, v in code_ref.items() if k in ("commit", "branch", "dirty", "repo")}
                self.db.execute("UPDATE tasks SET code_ref = ? WHERE id = ?", (json.dumps(code_ref), task["id"]))
                # deliverables of this turn get this turn's baseline; earlier turns keep theirs
                self.db.execute("UPDATE artifacts SET code_ref = ? WHERE task_id = ? AND turn = ?",
                                (json.dumps(code_ref), task["id"], turn))
            if result:
                self._event(task["id"], turn, "result", result)
            if error:
                self._event(task["id"], turn, "error", error)
            if task["stop_pending"]:
                self.db.execute("UPDATE tasks SET stop_pending = 0 WHERE id = ?", (task["id"],))
                self._event(task["id"], turn, "status", "canceled: 连接器确认进程已停止")
            if task["status"] in FINISHED:
                return
            status = "completed" if msg.get("status") == "completed" else "failed"
            self._set_status(
                task["id"],
                turn,
                status,
                result=result[:MAX_EVENT_CHARS],
                error=error[:MAX_EVENT_CHARS],
                session_id=session_id,
                pending_message=None,
            )

    # ---- sharing ---------------------------------------------------------

    def shares(self, user: dict, task_id: int) -> list[dict]:
        self.visible_task(user, task_id)
        return self.db.all(
            "SELECT s.user_id, u.username, u.display_name, s.can_continue, s.auto_approve, s.created_at "
            "FROM task_shares s JOIN users u ON u.id = s.user_id WHERE s.task_id = ? ORDER BY s.created_at",
            (task_id,),
        )

    def share_task(self, user: dict, task_id: int, target: str | int, can_continue: bool, auto_approve: bool) -> dict:
        task = self.get_task(task_id)
        if not self.is_party(user, task):
            raise HubError("只有发起人或智能体归属人可以分享这段对话", 403)
        if task.get("group_id"):
            raise HubError("群聊里的回复请直接邀请对方进群查看")
        if isinstance(target, int):
            other = self.db.one("SELECT id, username FROM users WHERE id = ?", (target,))
            key = str(target)
        else:
            key = str(target).strip().lstrip("@")
            # a username wins over an id, so a numeric username never resolves to someone else's id
            other = self.db.one("SELECT id, username FROM users WHERE username = ?", (key,)) or (
                self.db.one("SELECT id, username FROM users WHERE id = ?", (int(key),)) if key.isdigit() else None
            )
        if not other:
            raise HubError(f"用户不存在: {key}", 404)
        if other["id"] in (task["from_user_id"], task["owner_user_id"]):
            raise HubError(f"{other['username']} 本来就能看到这段对话")
        friend = self.db.one(
            "SELECT 1 FROM friendships WHERE status = 'accepted' AND ((user_id = ? AND friend_id = ?) OR "
            "(user_id = ? AND friend_id = ?))",
            (user["id"], other["id"], other["id"], user["id"]),
        )
        if not friend:
            raise HubError(f"{other['username']} 还不是你的好友，只能分享给好友", 403)
        # only the agent's owner decides whether someone else's turns skip approval
        auto = bool(auto_approve and can_continue and user["id"] == task["owner_user_id"])
        self.db.execute(
            "INSERT INTO task_shares (task_id, user_id, can_continue, auto_approve, shared_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (task_id, user_id) DO UPDATE SET "
            "can_continue = excluded.can_continue, auto_approve = excluded.auto_approve",
            (task_id, other["id"], int(bool(can_continue)), int(auto), user["id"], time.time()),
        )
        self.db.audit(user["id"], "task.share", task_id=task_id, to=other["username"], can_continue=bool(can_continue))
        self.publish(f"user:{other['id']}")
        self._publish_task(task_id)
        return {"user_id": other["id"], "username": other["username"], "can_continue": bool(can_continue), "auto_approve": auto}

    def unshare_task(self, user: dict, task_id: int, user_id: int) -> None:
        task = self.get_task(task_id)
        if not self.is_party(user, task) and user["id"] != user_id:
            raise HubError("无权修改分享", 403)
        self.db.execute("DELETE FROM task_shares WHERE task_id = ? AND user_id = ?", (task_id, user_id))
        self.db.audit(user["id"], "task.unshare", task_id=task_id, target_user_id=user_id)
        self.publish(f"user:{user_id}")
        self._publish_task(task_id)

    # ---- local conversations on the owner's computer ---------------------

    async def ask_connector(self, connector_id: int, message: dict, timeout: float = 20) -> dict:
        conn = self.connections.get(connector_id)
        if not conn:
            raise HubError("智能体所在的连接器不在线", 409)
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._requests[request_id] = (connector_id, future)
        try:
            await conn.send({**message, "request_id": request_id})
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise HubError("连接器没有响应，可能版本过旧，请升级到最新连接器", 504) from None
        finally:
            self._requests.pop(request_id, None)
        if reply.get("error"):
            raise HubError(str(reply["error"]))
        return reply

    # ---- 本机协作: the owner's own machine, read live through its connector, never stored --------

    @staticmethod
    def _collab_supported(version: str) -> bool:
        """Connectors before 0.9.11 ignore collab requests; development builds (no x.y.z version) answer them."""
        from .releases import parse_version  # releases imports this module

        parsed = parse_version(version or "")
        return parsed is None or parsed >= COLLAB_SINCE

    def collab_machines(self, user: dict) -> list[dict]:
        rows = self.db.all("SELECT id, name, hostname, collab, version FROM connectors WHERE user_id = ? ORDER BY id",
                           (user["id"],))
        return [{"id": r["id"], "name": r["name"] or r["hostname"], "hostname": r["hostname"],
                 "online": r["id"] in self.connections, "enabled": bool(r["collab"]),
                 "supported": self._collab_supported(r["version"]), "version": r["version"]} for r in rows]

    def _collab_machine(self, user: dict, connector_id: int) -> dict:
        row = self.db.one("SELECT id, user_id, collab, version FROM connectors WHERE id = ?", (connector_id,))
        if not row or row["user_id"] != user["id"]:  # administrators included: it is someone's own machine
            raise HubError("只能查看自己电脑上的协作", 403)
        return row

    def set_collab(self, user: dict, connector_id: int, enabled: bool) -> None:
        self._collab_machine(user, connector_id)
        self.db.execute("UPDATE connectors SET collab = ? WHERE id = ?", (int(enabled), connector_id))
        self.db.audit(user["id"], "collab.switch", connector_id=connector_id, enabled=enabled)

    async def collab_query(self, user: dict, connector_id: int, message: dict) -> dict:
        """Forwards a collab_* request to the owner's connector and returns its answer as is."""
        machine = self._collab_machine(user, connector_id)
        if not machine["collab"]:
            raise HubError("这台电脑的本机协作已关闭", 409)
        if connector_id not in self.connections:
            raise HubError("这台电脑不在线，数据只在它本机", 409)
        if not self._collab_supported(machine["version"]):
            raise HubError(f"这台电脑的连接器是 {machine['version']}，升级到 {'.'.join(map(str, COLLAB_SINCE))} 以上才能查看", 409)
        try:
            reply = await self.ask_connector(connector_id, message, timeout=30)
        except HubError as exc:
            if exc.status == 504:
                raise HubError("本机日志较多，连接器还在读取，请稍后刷新", 504) from None
            raise
        return reply.get("overview") or reply.get("session") or reply.get("discussion") or {}

    def _own_agent(self, user: dict, agent_id: int) -> dict:
        agent = self.resolve_agent(agent_id)
        if agent["user_id"] != user["id"]:
            raise HubError("只能查看自己智能体的本机会话", 403)
        if agent["kind"] not in ("claude", "codex"):
            raise HubError("只有 Claude Code 和 Codex 支持导入本机会话")
        return agent

    async def local_sessions(self, user: dict, agent_id: int) -> list[dict]:
        agent = self._own_agent(user, agent_id)
        reply = await self.ask_connector(agent["connector_id"], {"type": "list_sessions", "agent_key": agent["key"]})
        imported = {
            r["session_id"]: r["id"]
            for r in self.db.all("SELECT id, session_id FROM tasks WHERE agent_id = ? AND source = 'local'", (agent["id"],))
        }
        sessions = reply.get("sessions") or []
        for s in sessions:
            s["task_id"] = imported.get(s.get("id"))
        return sessions

    async def import_local_session(self, user: dict, agent_id: int, session_id: str, title: str = "") -> dict:
        agent = self._own_agent(user, agent_id)
        existing = self.db.one(
            "SELECT id FROM tasks WHERE agent_id = ? AND source = 'local' AND session_id = ?", (agent["id"], session_id)
        )
        if existing:
            return self.get_task(existing["id"])
        reply = await self.ask_connector(
            agent["connector_id"], {"type": "read_session", "agent_key": agent["key"], "session_id": session_id}, timeout=60
        )
        session, workspace = reply.get("session") or {}, str(reply.get("workspace") or "")
        turns = [t for t in session.get("turns") or [] if t.get("prompt")]
        if not turns or session.get("id") != session_id:
            raise HubError("这个会话没有可导入的内容")
        if workspace not in [w["path"] for w in self.workspaces_of(agent)]:
            raise HubError("该会话不在智能体的授权工作区内")
        # a second import of the same conversation may have finished while this one waited for the connector
        existing = self.db.one(
            "SELECT id FROM tasks WHERE agent_id = ? AND source = 'local' AND session_id = ?", (agent["id"], session_id)
        )
        if existing:
            return self.get_task(existing["id"])
        now = time.time()
        task_id = self.db.execute(
            "INSERT INTO tasks (from_user_id, from_agent, agent_id, title, prompt, status, depth, turn, session_id, "
            "result, auto_approved, created_at, updated_at, finished_at, workspace, source) "
            "VALUES (?, '', ?, ?, ?, 'completed', 0, ?, ?, ?, 1, ?, ?, ?, ?, 'local')",
            (
                user["id"], agent["id"], (title.strip() or session.get("title") or turns[0]["prompt"])[:120],
                turns[0]["prompt"], len(turns), session_id, turns[-1].get("result") or "", now, now, now, workspace,
            ),
        )
        self._event(task_id, 1, "status", "completed: 从本机会话导入")
        for i, t in enumerate(turns, 1):
            self._event(task_id, i, "prompt", t["prompt"], {"by": user["username"]})
            if t.get("result"):
                self._event(task_id, i, "result", t["result"])
        self.db.audit(user["id"], "task.import_local", task_id=task_id, agent=agent_address(agent), turns=len(turns))
        return self.get_task(task_id)

    # ---- images people attach to their messages ----------------------------

    @staticmethod
    def decode_images(images: list | None) -> list[tuple[str, bytes]]:
        """Checks the images attached to a message ([{name, data: base64}]) and returns (name, bytes) pairs."""
        if not images:
            return []
        if not isinstance(images, list) or len(images) > MAX_INPUT_IMAGES:
            raise HubError(f"每条消息最多带 {MAX_INPUT_IMAGES} 张图片")
        out, total = [], 0
        for item in images:
            if not isinstance(item, dict):
                raise HubError("图片格式不对")
            name = Path(str(item.get("name") or "image")).name[:120] or "image"
            try:
                data = base64.b64decode(str(item.get("data") or "").split(",")[-1], validate=True)
            except (binascii.Error, ValueError):
                raise HubError(f"图片 {name} 无法读取") from None
            suffix = _image_suffix(data)
            if not suffix:
                raise HubError(f"{name} 不是支持的图片（PNG、JPG、GIF、WebP）")
            if len(data) > MAX_INPUT_IMAGE_BYTES:
                raise HubError(f"图片 {name} 超过 {MAX_INPUT_IMAGE_BYTES >> 20}MB")
            total += len(data)
            if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                name += suffix
            out.append((name, data))
        if total > MAX_INPUT_IMAGES_BYTES:
            raise HubError(f"一条消息的图片合计不能超过 {MAX_INPUT_IMAGES_BYTES >> 20}MB")
        return out

    def save_images(self, task_id: int, turn: int, pictures: list[tuple[str, bytes]]) -> None:
        """Stores a message's images as `image` events marked as input, shown with the message and sent to the agent."""
        if not pictures:
            return
        if self.attachments_dir is None:
            raise HubError("Hub 没有配置附件目录，不能发送图片", 409)
        folder = self.attachments_dir / str(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in pictures:
            stored = f"{secrets.token_hex(8)}{_image_suffix(data)}"
            (folder / stored).write_bytes(data)
            self._event(task_id, turn, "image",
                        json.dumps({"name": name, "file": stored, "size": len(data), "input": True}, ensure_ascii=False))

    def _turn_images(self, task_id: int, turn: int) -> list[dict]:
        """The images the person attached to this turn's message, for the connector to hand to the agent."""
        out = []
        for row in self.db.all("SELECT content FROM task_events WHERE task_id = ? AND turn = ? AND kind = 'image' ORDER BY id",
                               (task_id, turn)):
            meta = json.loads(row["content"])
            path = self.attachment_path(task_id, str(meta.get("file", ""))) if meta.get("input") else None
            if path:
                out.append({"name": meta["name"], "data": base64.b64encode(path.read_bytes()).decode()})
        return out

    def _attachment(self, task_id: int, turn: int, msg: dict) -> None:
        """Store a file the agent produced (e.g. a generated image) and record it as an `image`/`file` event."""
        name = Path(str(msg.get("name") or "file")).name[:120] or "file"
        try:
            data = base64.b64decode(str(msg.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            return
        if not data or len(data) > MAX_ATTACHMENT_BYTES or self.attachments_dir is None:
            return
        folder = self.attachments_dir / str(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        stored = f"{secrets.token_hex(8)}{Path(name).suffix.lower()[:8]}"
        (folder / stored).write_bytes(data)
        kind = "image" if Path(name).suffix.lower() in IMAGE_SUFFIXES else "file"
        self._event(task_id, turn, kind, json.dumps({"name": name, "file": stored, "size": len(data)}, ensure_ascii=False))
        self._record_artifact(task_id, turn, name, kind, data, stored, mimetypes.guess_type(name)[0] or "")

    # ---- artifacts: versioned deliverables other tasks can reference ---------

    def _record_artifact(self, task_id: int, turn: int, name: str, kind: str, data: bytes, stored: str, mime: str = "") -> dict:
        version = self.db.one(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM artifacts WHERE task_id = ? AND name = ?", (task_id, name)
        )["v"]
        artifact_id = self.db.execute(
            "INSERT INTO artifacts (task_id, turn, name, kind, version, sha256, size, stored, mime, code_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            # the baseline is only known when the turn ends (see task_done); until then it is unknown, never a guess
            (task_id, turn, name, kind, version, hashlib.sha256(data).hexdigest(), len(data), stored, mime, "{}", time.time()),
        )
        self._event(task_id, turn, "artifact", f"产物 A{artifact_id} {name} v{version}",
                    {"artifact_id": artifact_id, "name": name, "kind": kind, "version": version})
        return self.artifact(artifact_id)

    def submit_artifact(self, task: dict, name: str, kind: str, content: str) -> dict:
        """An agent hands in a report, diff or test result while it runs (MCP submit_artifact)."""
        if task["status"] != "running":
            raise HubError("只有执行中的任务可以提交产物")
        name = Path(str(name or "").strip()).name[:120]
        if not name:
            raise HubError("产物需要文件名，例如 review.md、change.diff、test-report.txt")
        if kind not in ARTIFACT_KINDS:
            raise HubError("kind 必须是 " + " / ".join(ARTIFACT_KINDS))
        data = (content or "").encode("utf-8")
        if not data:
            raise HubError("产物内容不能为空")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise HubError(f"产物超过 {MAX_ARTIFACT_BYTES // 1024 // 1024}MB 上限，请精简或拆分")
        if self.attachments_dir is None:
            raise HubError("Hub 未配置存储目录", 500)
        folder = self.attachments_dir / str(task["id"])
        folder.mkdir(parents=True, exist_ok=True)
        stored = f"{secrets.token_hex(8)}{Path(name).suffix.lower()[:8] or '.txt'}"
        (folder / stored).write_bytes(data)
        return self._record_artifact(task["id"], task["turn"], name, kind, data, stored, "text/plain; charset=utf-8")

    def artifact(self, artifact_id: int) -> dict:
        row = self.db.one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not row:
            raise HubError(f"产物不存在: A{artifact_id}", 404)
        row["code_ref"] = _json_or_none(row["code_ref"]) or {}
        row["ref"] = f"A{row['id']}"
        return row

    def visible_artifact(self, user: dict, artifact_id: int) -> dict:
        row = self.artifact(artifact_id)
        self.visible_task(user, row["task_id"])
        return row

    def readable_artifact(self, user: dict, artifact_id: int, reader_task: dict | None) -> dict:
        """A task may read deliverables its own requester could read: the upstream steps of a workflow or the
        sub-tasks it delegated run on other people's agents, whose owners cannot see each other's tasks."""
        row = self.artifact(artifact_id)
        source = self.get_task(row["task_id"])
        if self.can_view(user, source):
            return row
        if reader_task and reader_task["status"] in ACTIVE:
            requester = self.get_user(reader_task["from_user_id"])
            if requester and not requester["disabled"] and self.can_view(requester, source):
                return row
        raise HubError("无权读取该产物：只有能查看其来源任务的人，或由同一发起人派出的任务可以读取", 403)

    def artifacts(self, user: dict, task_id: int) -> list[dict]:
        task = self.visible_task(user, task_id)
        rows = [self.artifact(r["id"]) for r in self.db.all("SELECT id FROM artifacts WHERE task_id = ? ORDER BY id", (task_id,))]
        if not self.sees_process(user, task):
            for row in rows:
                row["code_ref"] = public_code_ref(row["code_ref"])
        return rows

    def artifact_bytes(self, artifact: dict) -> bytes:
        path = self.attachment_path(artifact["task_id"], artifact["stored"])
        if not path:
            raise HubError("产物文件已丢失", 410)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise HubError("产物文件校验失败（哈希不一致），已拒绝读取", 409)
        return data

    async def review_artifact(self, user: dict, artifact_id: int, verdict: str, note: str = "") -> dict:
        """The requester accepts a deliverable or sends it back; rework continues the same conversation."""
        artifact = self.artifact(artifact_id)
        task = self.get_task(artifact["task_id"])
        if user["id"] != task["from_user_id"] and not (task.get("turn_user_id") == user["id"]):
            raise HubError("只有任务发起人可以验收产物", 403)
        if verdict not in ("accepted", "rework"):
            raise HubError("verdict 必须是 accepted 或 rework")
        note = (note or "").strip()[:2000]
        if verdict == "rework" and not note:
            raise HubError("返工需要写明原因，智能体会按此继续修改")
        if verdict == "rework" and task["status"] not in ("completed", "failed", "running", "queued", "dispatched", "pending_approval"):
            raise HubError(f"任务已{'取消' if task['status'] == 'canceled' else '被拒绝'}，不能返工；请重新派发")
        queued = verdict == "rework" and task["status"] not in ("completed", "failed")
        if verdict == "rework" and not queued:
            # send first: if the follow-up is refused, the review is not recorded either
            await self.follow_up(user, task["id"], f"产物 A{artifact_id}（{artifact['name']} v{artifact['version']}）验收未通过，请返工：\n{note}")
        self.db.execute(
            "UPDATE artifacts SET review = ?, review_note = ?, reviewed_by = ?, reviewed_at = ?, rework_pending = ?, "
            "rework_turn = ? WHERE id = ?",
            (verdict, note, user["id"], time.time(), int(queued), task["turn"] if queued else None, artifact_id),
        )
        self._event(task["id"], task["turn"], "review",
                    f"{user['username']} {'验收通过' if verdict == 'accepted' else '要求返工'} A{artifact_id} {artifact['name']} v{artifact['version']}"
                    + (f"：{note}" if note else ""),
                    {"artifact_id": artifact_id, "verdict": verdict, "note": note})
        self.db.audit(user["id"], "artifact.review", artifact_id=artifact_id, verdict=verdict)
        result = self.artifact(artifact_id)
        if not self.sees_process(user, task):
            result["code_ref"] = public_code_ref(result["code_ref"])
        result["rework"] = "queued" if queued else ("sent" if verdict == "rework" else "")
        return result

    async def _send_queued_rework(self, task_id: int) -> None:
        """Rework asked for while a turn ran goes out once that turn ends, as one follow-up.

        Idempotent across crashes: a pending request belongs to the turn that was running when it was made. It is sent only
        while the task is still finished on that turn; once the follow-up moved the task on, the flag is merely
        cleared. So a restart before the follow-up sends it, and a restart after it never sends it twice.
        """
        task = self.get_task(task_id)
        rows = self.db.all("SELECT * FROM artifacts WHERE task_id = ? AND rework_pending = 1 ORDER BY id", (task_id,))
        if not rows:
            return
        # queued during turn N: due once turn N finished; if the task already moved past N, it was sent
        sent_already = [r for r in rows if (r["rework_turn"] or r["turn"]) < task["turn"]]
        for r in sent_already:
            self._consume_rework(r)
        due = [r for r in rows if r not in sent_already]
        if not due or task["status"] not in ("completed", "failed"):
            return
        reviewer = self.get_user(due[-1]["reviewed_by"])
        lines = [f"产物 A{r['id']}（{r['name']} v{r['version']}）验收未通过，请返工：{r['review_note']}" for r in due]
        try:
            await self.follow_up(reviewer, task_id, "\n".join(lines))
        except HubError as exc:
            self._event(task_id, task["turn"], "status", f"返工未能自动发出：{exc}")
            return
        for r in due:
            self._consume_rework(r)

    def _consume_rework(self, row: dict) -> None:
        # only the request that was read: a newer one made meanwhile (another turn, another note) stays pending
        self.db.execute(
            "UPDATE artifacts SET rework_pending = 0 WHERE id = ? AND rework_pending = 1 AND rework_turn IS ? "
            "AND reviewed_at IS ? AND review_note = ?",
            (row["id"], row["rework_turn"], row["reviewed_at"], row["review_note"]),
        )

    async def recover_rework(self) -> None:
        """At start-up: send rework that was queued when the hub stopped before sending it."""
        for row in self.db.all("SELECT DISTINCT task_id FROM artifacts WHERE rework_pending = 1"):
            try:
                await self._send_queued_rework(row["task_id"])
            except Exception:  # noqa: BLE001 - one task must not block the others
                LOGGER.exception("rework recovery failed task=%s", row["task_id"])

    def attachments(self, task_id: int) -> list[dict]:
        rows = self.db.all(
            "SELECT id, turn, kind, content FROM task_events WHERE task_id = ? AND kind IN ('image', 'file') ORDER BY id",
            (task_id,),
        )
        return [{"id": r["id"], "turn": r["turn"], "kind": r["kind"], **json.loads(r["content"])} for r in rows]

    def attachment_path(self, task_id: int, stored: str) -> Path | None:
        if self.attachments_dir is None or Path(stored).name != stored:
            return None
        path = self.attachments_dir / str(task_id) / stored
        return path if path.is_file() else None

    def _blocked_on(self, task_id: int) -> set[int]:
        """Tasks this one cannot progress without: its blocking waits, the holder of a workspace lock it
        waits for, and, while it is queued, the task occupying its agent (connectors run one task per agent)."""
        edges = set(self.waits.get(task_id, ()))
        holder = self.lock_holder(task_id)
        if holder:
            edges.add(holder)
        row = self.db.one("SELECT agent_id, status FROM tasks WHERE id = ?", (task_id,))
        if row and row["status"] in ("queued", "dispatched"):
            live = self.telemetry.get(row["agent_id"])
            if live and live.get("running_task") and live["running_task"] != task_id:
                edges.add(live["running_task"])
            else:
                edges |= {r["id"] for r in self.db.all(
                    "SELECT id FROM tasks WHERE agent_id = ? AND status = 'running' AND id != ?", (row["agent_id"], task_id)
                )}
        return edges

    def wait_cycle(self, waiter: int, target: int) -> list[int] | None:
        """The chain target → … → waiter if waiter blocking on target would deadlock, else None."""
        stack, seen = [(target, [waiter, target])], {target}
        while stack:
            node, path = stack.pop()
            for nxt in self._blocked_on(node):
                if nxt == waiter:
                    return path + [waiter]
                if nxt not in seen and len(seen) < 1000:
                    seen.add(nxt)
                    stack.append((nxt, path + [nxt]))
        return None

    async def wait_for(self, task_id: int, timeout: float, waiter: int | None = None) -> dict:
        """Block until the task finishes. A waiting task is recorded so wait reasons and cycle checks see it."""
        if waiter is not None:
            cycle = self.wait_cycle(waiter, task_id)
            if cycle:
                chain = " → ".join(f"#{i}" for i in cycle)
                raise HubError(
                    f"同步等待会造成互相等待（{chain}）：被等待的任务需要你当前占用的智能体或工作区才能继续。"
                    "请改为 wait_seconds=0 异步派发，结束本轮后再用 get_task 查看结果，或换用其他智能体/独立工作区", 409
                )
            self.waits.setdefault(waiter, set()).add(task_id)
            self._publish_task(waiter)
        deadline = time.monotonic() + max(0.0, min(timeout, 600))
        try:
            while True:
                task = self.get_task(task_id)
                if task["status"] in FINISHED or time.monotonic() >= deadline:
                    return task
                if waiter is not None and self.wait_cycle(waiter, task_id):
                    raise HubError("被等待的任务进入了需要你当前占用资源的状态，已停止等待以免互相阻塞；请稍后用 get_task 查看", 409)
                await asyncio.sleep(1)
        finally:
            if waiter is not None:
                targets = self.waits.get(waiter)
                if targets is not None:
                    targets.discard(task_id)
                    if not targets:
                        self.waits.pop(waiter, None)
                self._publish_task(waiter)


def _image_suffix(data: bytes) -> str:
    """The file type of an image by its first bytes; '' when it is not one the agents can read."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""
