"""MCP endpoint so agents (Codex, Claude Code, Hermes...) can discover each other and delegate tasks."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from pydantic import Field
from starlette.responses import JSONResponse

from .auth import bearer, resolve_api_token
from .hub import FINISHED, Hub, HubError, public_code_ref
from .social import Social

INSTRUCTIONS = """\
Agent Hub：团队智能体协作中心。你可以发现同事的智能体并给它们派任务，任务会在对方电脑上由对方的智能体执行；
也可以代表用户和好友私聊、在群聊里发言（@ 智能体会让它干活），用户不需要打开 Hub 网页。
- 用户问“有没有新消息/谁找我”时先调用 inbox；私聊用 send_message / read_messages，群聊用 list_groups / read_group / post_group。
- 使用同事的智能体需要先加好友（add_friend，对方同意），再申请该智能体的权限（request_agent_access，对方授权工作区）。
- 有人派任务给你用户的智能体且等待审批时，可在用户明确同意后用 approve_task 批准或拒绝。
- 先用 list_agents 找到目标智能体地址（格式 用户名/智能体标识）。
- 用 create_task 派任务；任务描述要自包含（对方看不到你的上下文），写清目标、路径、期望产出。
- 对方未设置免审批时任务处于 pending_approval，需要对方本人批准。
- 用 get_task 查询/等待结果；用 follow_up 在同一会话里继续追问。
- 对话可以分享给好友（share_task），对方可只读或继续追问；别人分享给我的对话用 list_tasks(box="shared") 查看，用 follow_up 继续。
- 用户本机的 Claude Code / Codex 历史会话：list_local_sessions 列出（只含工作区内的），import_local_session 导入为 Hub 对话后再 share_task；导入和分享前要让用户确认是哪个会话。
- 如果你本身正在执行 Agent Hub 派来的任务，再派子任务时把 parent_task_id 设为当前任务 ID。
- 执行任务时，报告、diff、测试结果等成果用 submit_artifact 提交为产物（得到编号 A123，带版本和哈希），下游任务用 get_artifact 读取。
- 同步等待（wait_seconds>0）一个需要你当前占用的智能体或工作区才能开始的任务会被拒绝（互相等待）；这时改为异步派发。
"""


def _task_view(hub: Hub, task: dict, include_output: bool = False, viewer: dict | None = None) -> dict:
    if viewer:
        task = hub.present_task(viewer, task)  # non-owners get the masked error, as in the console
    view = {
        "task_id": task["id"],
        "title": task["title"],
        "agent": task["agent_address"],
        "from": task["from_username"] + (f"/{task['from_agent']}" if task["from_agent"] else ""),
        "status": task["status"],
        "turn": task["turn"],
        "finished": task["status"] in FINISHED,
        "result": task["result"],
        "error": task["error"],
    }
    if task["status"] == "pending_approval":
        view["hint"] = f"等待 {task['owner_username']} 审批，可稍后用 get_task 查询"
    elif task["status"] not in FINISHED:
        view["waiting_for"] = hub.wait_reason(task)["text"]
    artifacts = hub.db.all("SELECT id, name, kind, version, review FROM artifacts WHERE task_id = ? ORDER BY id", (task["id"],))
    if artifacts:
        view["artifacts"] = [f"A{a['id']} {a['name']} v{a['version']}（{a['kind']}，验收：{a['review']}）" for a in artifacts]
    if include_output:
        view["output"] = [
            f"[{e['kind']}] {e['content']}" for e in hub.events(task["id"], viewer=viewer) if e["kind"] in ("output", "status")
        ][-80:]
    return view


def _next_step(agent: dict) -> str:
    """What the caller must do before it can dispatch to this agent."""
    if agent.get("access") == "accepted" and agent.get("isolation") not in ("sandbox", "limited") and not agent.get("allow_full"):
        return f"{agent['username']} 的这个智能体以完全权限运行，需要对方在「谁能用我的智能体」里允许你使用完全权限"
    if agent.get("access") == "owner" or (agent.get("access") == "accepted" and agent["workspaces"]):
        return ""
    if agent.get("access") == "accepted":
        return f"{agent['username']} 已同意但还没授权任何工作区，请对方在控制台「智能体」页给你勾选工作区"
    if agent.get("access") == "pending":
        return f"已申请，等待 {agent['username']} 同意"
    if agent.get("is_friend"):
        return f"用 request_agent_access 申请使用 {agent['address']}"
    return f"先用 add_friend 加 {agent['username']} 为好友，再申请使用"


def _person(row: dict) -> str:
    return row["username"] + (f"（{row['display_name']}）" if row.get("display_name") and row["display_name"] != row["username"] else "")


async def _wait_until(check, wait_seconds: int, interval: float = 2.0):
    """Poll `check` until it returns something truthy or the wait runs out."""
    deadline = time.monotonic() + max(0, min(wait_seconds, 600))
    while True:
        result = check()
        if result or time.monotonic() >= deadline:
            return result
        await asyncio.sleep(interval)


def build_mcp(hub: Hub, social: Social) -> FastMCP:
    mcp = FastMCP("agent-hub", instructions=INSTRUCTIONS)

    def token_row(task_ok: bool) -> dict:
        token = bearer(get_http_headers(include_all=True))
        row = resolve_api_token(hub.db, token, ("personal", "task")) if token else None
        if not row:
            raise HubError("未授权：请在 Agent Hub 设置中创建个人令牌，并以 Bearer 方式配置到 MCP", 401)
        if row["kind"] == "task" and not task_ok:
            raise HubError("执行 Hub 任务时只能和其他智能体协作（查找、派任务、查询、追问），不能代表用户私聊、发群消息或做审批授权", 403)
        return row

    def current_user(task_ok: bool = False) -> dict:
        return hub.get_user(token_row(task_ok)["user_id"])

    def running_task() -> dict | None:
        """The task whose agent is calling, when it authenticated with a task token."""
        row = token_row(True)
        return hub.get_task(int(row["name"].split(":", 1)[1])) if row["kind"] == "task" else None

    def waiter_id() -> int | None:
        task = running_task()
        return task["id"] if task else None

    def wrap(fn):
        async def runner(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except HubError as exc:
                return {"error": str(exc)}

        return runner

    @mcp.tool
    async def list_agents(
        query: Annotated[str, Field(description="按地址/名称/描述/标签/归属人模糊过滤，可为空")] = "",
        online_only: Annotated[bool, Field(description="只返回在线智能体")] = False,
    ) -> dict:
        """列出已注册到 Agent Hub 的智能体（包括同事的）。"""

        async def run():
            user = current_user(True)
            agents = hub.list_agents(query, online_only, viewer=user)
            return {
                "agents": [
                    {
                        "address": a["address"],
                        "name": a["name"],
                        "kind": a["kind"],
                        "owner": a["owner_name"],
                        "host": f"{a['hostname']} ({a['os']})",
                        "online": a["online"],
                        "description": a["description"],
                        "tags": a["tags"],
                        "workspaces": a["workspaces"],
                        "isolation": a["isolation"],
                        "access": a.get("access"),
                        "can_dispatch": a.get("access") == "owner" or (
                            a.get("access") == "accepted" and bool(a["workspaces"])
                            and (a.get("isolation") in ("sandbox", "limited") or a.get("allow_full"))),
                        "next_step": _next_step(a),
                    }
                    for a in agents
                ]
            }

        return await wrap(run)()

    @mcp.tool
    async def create_task(
        to: Annotated[str, Field(description="目标智能体地址，格式 用户名/智能体标识，如 zhangsan/claude")],
        prompt: Annotated[str, Field(description="自包含的任务描述")],
        title: Annotated[str, Field(description="简短标题，可为空")] = "",
        from_agent: Annotated[str, Field(description="你自己的智能体标识，如 codex、claude，便于对方识别")] = "",
        parent_task_id: Annotated[int | None, Field(description="若你正在执行 Agent Hub 任务，填当前任务 ID")] = None,
        wait_seconds: Annotated[int, Field(description="同步等待结果的秒数，0 表示立即返回，最长 600")] = 0,
        workspace: Annotated[str, Field(description="在哪个工作区执行：list_agents 返回的 workspaces[].id 或名称。派给同事的智能体时必须是对方授权给你的工作区；只授权了一个时可留空")] = "",
    ) -> dict:
        """给另一个智能体派任务，任务在对方电脑上执行。派给同事的智能体需先成为好友并获得工作区授权。"""

        async def run():
            user = current_user(True)
            parent_id, sender = parent_task_id, from_agent
            parent = running_task()
            if parent:  # sub-task of the running task: link it so depth limits and audit apply
                parent_id, sender = parent["id"], from_agent or parent["agent_key"]
            task = await hub.create_task(user, to, prompt, title, sender, parent_id, workspace=workspace)
            if wait_seconds > 0 and task["status"] != "pending_approval":
                task = await hub.wait_for(task["id"], wait_seconds, waiter_id())
            return _task_view(hub, task, viewer=user)

        return await wrap(run)()

    @mcp.tool
    async def get_task(
        task_id: int,
        wait_seconds: Annotated[int, Field(description="等待任务结束的秒数，0 表示立即返回，最长 600")] = 0,
        include_output: Annotated[bool, Field(description="附带最近的执行过程输出")] = False,
    ) -> dict:
        """查询任务状态与结果，可选择等待其结束。"""

        async def run():
            user = current_user(True)
            hub.visible_task(user, task_id)
            task = await hub.wait_for(task_id, wait_seconds, waiter_id()) if wait_seconds > 0 else hub.get_task(task_id)
            return _task_view(hub, task, include_output, user)

        return await wrap(run)()

    @mcp.tool
    async def follow_up(
        task_id: int,
        message: Annotated[str, Field(description="追加给对方智能体的消息，会在同一会话中继续")],
        wait_seconds: Annotated[int, Field(description="等待结果的秒数，最长 600")] = 0,
    ) -> dict:
        """对已完成/失败的任务继续追问或补充要求（多轮协作）。"""

        async def run():
            user = current_user(True)
            running = running_task()
            if running and not hub.in_subtree(task_id, running["id"]):
                # a running task's credential only continues the sub-tasks it started, not the owner's other tasks
                raise HubError(f"任务 #{task_id} 不是当前任务 #{running['id']} 派出的子任务，不能用任务凭证追问", 403)
            if running:
                # access may have been withdrawn since the sub-task was made: check the chain again
                target = hub.get_task(task_id)
                hub.check_borrowing(user, hub.get_task(target["parent_task_id"]), hub.resolve_agent(target["agent_id"]),
                                    target["workspace"])
            task = await hub.follow_up(user, task_id, message)
            if wait_seconds > 0:
                task = await hub.wait_for(task_id, wait_seconds, waiter_id())
            return _task_view(hub, task, viewer=user)

        return await wrap(run)()

    @mcp.tool
    async def submit_artifact(
        name: Annotated[str, Field(description="产物文件名，如 review.md、change.diff、test-report.txt；同名再次提交会生成新版本")],
        kind: Annotated[str, Field(description="report（报告/说明）| diff（代码改动）| test（测试结果）| file（其他文本）")],
        content: Annotated[str, Field(description="产物的完整文本内容，最大 2MB")],
    ) -> dict:
        """执行 Hub 任务时提交一个产物（报告、diff、测试结果），供发起人验收、下游任务引用。只能在执行任务时调用。"""

        async def run():
            task = running_task()
            if not task:
                raise HubError("submit_artifact 只能在执行 Agent Hub 任务时使用（任务令牌）", 403)
            artifact = hub.submit_artifact(task, name, kind, content)
            return {"artifact": artifact["ref"], "name": artifact["name"], "version": artifact["version"], "sha256": artifact["sha256"]}

        return await wrap(run)()

    @mcp.tool
    async def get_artifact(
        artifact: Annotated[str, Field(description="产物编号，如 A123 或 123")],
    ) -> dict:
        """读取一个产物的内容（需能查看其来源任务），返回内容、版本、哈希、来源任务和代码基线。"""

        async def run():
            user = current_user(True)
            raw = str(artifact).strip().lstrip("Aa")
            if not raw.isdigit():
                raise HubError("产物编号格式为 A123")
            row = hub.readable_artifact(user, int(raw), running_task())
            data = hub.artifact_bytes(row)
            text = data.decode("utf-8", errors="replace") if row["kind"] not in ("image",) else f"（图片，{len(data)} 字节，请在控制台查看）"
            return {"artifact": row["ref"], "name": row["name"], "kind": row["kind"], "version": row["version"],
                    "sha256": row["sha256"], "task_id": row["task_id"],
                    "code_ref": row["code_ref"] if hub.sees_process(user, hub.get_task(row["task_id"])) else public_code_ref(row["code_ref"]), "review": row["review"],
                    "content": text}

        return await wrap(run)()

    @mcp.tool
    async def cancel_task(task_id: int) -> dict:
        """取消尚未结束的任务。"""

        async def run():
            user = current_user(True)
            running = running_task()
            if running and not hub.in_subtree(task_id, running["id"]):
                # same limit as follow_up: a task credential only cancels the sub-tasks it started
                raise HubError(f"任务 #{task_id} 不是当前任务 #{running['id']} 派出的子任务，不能用任务凭证取消", 403)
            return _task_view(hub, await hub.cancel(user, task_id), viewer=user)

        return await wrap(run)()

    @mcp.tool
    async def list_tasks(
        box: Annotated[str, Field(description="sent=我发出的，received=派给我智能体的，shared=别人分享给我的，all=全部")] = "all",
        status: Annotated[str, Field(description="按状态过滤，可为空")] = "",
        limit: int = 20,
    ) -> dict:
        """列出与我相关的任务。"""

        async def run():
            user = current_user(True)
            return {
                "tasks": [
                    {k: t[k] for k in ("id", "title", "status", "agent_address", "from_username", "turn")}
                    for t in hub.list_tasks(user, box, status, limit)
                ]
            }

        return await wrap(run)()

    @mcp.tool
    async def share_task(
        task_id: int,
        user: Annotated[str, Field(description="分享给哪个好友（用户名）")],
        can_continue: Annotated[bool, Field(description="true=对方可以在这段对话里继续追问，false=只读")] = False,
        auto_approve: Annotated[bool, Field(description="对方的追问免审批（仅智能体归属人可设置）")] = False,
        revoke: Annotated[bool, Field(description="true=取消对该好友的分享")] = False,
    ) -> dict:
        """把一段对话分享给好友（只读或可继续对话），或取消分享。需用户明确同意。"""

        async def run():
            me = current_user()
            if revoke:
                other = social._user(user)
                hub.unshare_task(me, task_id, other["id"])
            else:
                hub.share_task(me, task_id, user, can_continue, auto_approve)
            return {"shares": hub.shares(me, task_id)}

        return await wrap(run)()

    @mcp.tool
    async def list_local_sessions(
        agent: Annotated[str, Field(description="我自己的智能体标识或地址，如 claude、codex")],
    ) -> dict:
        """列出我本机该智能体（Claude Code / Codex）在授权工作区内的最近会话，可导入后分享给同事。"""

        async def run():
            me = current_user()
            target = agent if "/" in agent else f"{me['username']}/{agent}"
            return {"sessions": await hub.local_sessions(me, hub.resolve_agent(target)["id"])}

        return await wrap(run)()

    @mcp.tool
    async def import_local_session(
        agent: Annotated[str, Field(description="我自己的智能体标识或地址")],
        session_id: Annotated[str, Field(description="list_local_sessions 返回的会话 id")],
        title: str = "",
    ) -> dict:
        """把本机的一个历史会话导入为 Hub 对话（之后可 share_task 分享，追问会在原会话中继续）。"""

        async def run():
            me = current_user()
            target = agent if "/" in agent else f"{me['username']}/{agent}"
            task = await hub.import_local_session(me, hub.resolve_agent(target)["id"], session_id, title)
            return _task_view(hub, task)

        return await wrap(run)()

    # ---- people: friends, direct messages, groups ---------------------

    @mcp.tool
    async def inbox() -> dict:
        """查看待处理事项：未读私聊、未读群聊、好友申请、智能体权限申请、等我审批的任务。"""

        async def run():
            user = current_user()
            friends = social.friends(user)
            contacts = social.contacts(user)
            return {
                "unread_messages": [
                    {"from": _person(f), "unread": f["unread"], "last": (f["last_message"] or {}).get("content", "")[:200]}
                    for f in friends["friends"] if f["unread"]
                ],
                "unread_groups": [
                    {"group_id": g["id"], "name": g["name"], "unread": g["unread"],
                     "last": f"{g['last_message']['username']}: {g['last_message']['content'][:200]}"}
                    for g in social.groups(user) if g["unread"]
                ],
                "friend_requests": [{"from": _person(r), "note": r["note"]} for r in friends["incoming"]],
                "access_requests": [
                    {"request_id": r["id"], "from": _person(r), "agent": r["agent"]["address"], "note": r["note"],
                     "workspaces": [{"id": w["id"], "name": w["name"]} for w in r["agent"]["workspaces"]]}
                    for r in contacts["incoming"]
                ],
                "shared_with_me": [
                    {"task_id": r["task_id"], "title": r["title"], "by": r["username"], "can_continue": bool(r["can_continue"])}
                    for r in hub.db.all(
                        "SELECT s.task_id, s.can_continue, t.title, u.username FROM task_shares s "
                        "JOIN tasks t ON t.id = s.task_id JOIN users u ON u.id = s.shared_by "
                        "WHERE s.user_id = ? AND s.created_at > ? ORDER BY s.created_at DESC LIMIT 20",
                        (user["id"], time.time() - 7 * 86400),
                    )
                ],
                "tasks_waiting_my_approval": [
                    {"task_id": t["id"], "title": t["title"], "from": t["from_username"], "agent": t["agent_address"]}
                    for t in hub.list_tasks(user, "approval", "", 20)
                ],
            }

        return await wrap(run)()

    @mcp.tool
    async def list_friends() -> dict:
        """列出好友（含未读数、最近一条消息、他们的智能体及我的使用权限），以及待处理的好友申请。"""

        async def run():
            data = social.friends(current_user())
            return {
                "friends": [
                    {
                        "username": f["username"], "name": f["display_name"], "unread": f["unread"],
                        "last_message": (f["last_message"] or {}).get("content", "")[:200],
                        "agents": [{"address": a["address"], "name": a["name"], "online": a["online"], "access": a["access"],
                                    "workspaces": a["workspaces"]} for a in f["agents"]],
                    }
                    for f in data["friends"]
                ],
                "requests_to_me": [{"username": r["username"], "note": r["note"]} for r in data["incoming"]],
                "my_pending_requests": [r["username"] for r in data["outgoing"]],
            }

        return await wrap(run)()

    @mcp.tool
    async def add_friend(
        username: Annotated[str, Field(description="对方用户名")],
        note: Annotated[str, Field(description="附言，可为空")] = "",
    ) -> dict:
        """发送好友申请；对方同意后才能私聊、拉群和申请其智能体权限。如果对方已向我申请，则直接成为好友。"""

        async def run():
            row = social.request_friend(current_user(), username, note)
            return {"status": row["status"], "hint": "已是好友" if row["status"] == "accepted" else "已发送申请，等待对方同意"}

        return await wrap(run)()

    @mcp.tool
    async def answer_friend_request(
        username: Annotated[str, Field(description="发来好友申请的用户名")],
        accept: Annotated[bool, Field(description="true 同意，false 拒绝")],
    ) -> dict:
        """处理别人发给我的好友申请（需用户明确同意）。"""

        async def run():
            user = current_user()
            row = social.friendship(user["id"], social._user(username)["id"])
            if not row:
                raise HubError(f"{username} 没有向你发好友申请", 404)
            social.answer_friend(user, row["id"], accept)
            return {"ok": True}

        return await wrap(run)()

    @mcp.tool
    async def send_message(
        to: Annotated[str, Field(description="好友用户名")],
        content: Annotated[str, Field(description="消息内容")],
        wait_reply_seconds: Annotated[int, Field(description="发送后等待对方回复的秒数，0 不等待，最长 600")] = 0,
    ) -> dict:
        """给好友发私聊消息，可选择等待对方回复。"""

        async def run():
            user = current_user()
            sent = social.send_direct(user, to, content)
            other = social._user(to)

            def replies():
                return [m for m in social.direct_messages(user, to, sent["id"]) if m["from_user_id"] == other["id"]]

            got = await _wait_until(replies, wait_reply_seconds) if wait_reply_seconds > 0 else []
            return {"sent": True, "replies": [{"content": m["content"], "at": m["created_at"]} for m in got or []]}

        return await wrap(run)()

    @mcp.tool
    async def read_messages(
        with_user: Annotated[str, Field(description="好友用户名")],
        limit: Annotated[int, Field(description="返回最近多少条")] = 30,
    ) -> dict:
        """读取和某位好友的私聊记录（会标记为已读）。"""

        async def run():
            user = current_user()
            rows = social.direct_messages(user, with_user)[-max(1, min(limit, 300)):]
            return {"messages": [{"from": m["username"], "content": m["content"], "at": m["created_at"]} for m in rows]}

        return await wrap(run)()

    @mcp.tool
    async def list_groups() -> dict:
        """列出我所在的群聊（含成员、智能体、未读数）。"""

        async def run():
            return {
                "groups": [
                    {"group_id": g["id"], "name": g["name"], "unread": g["unread"],
                     "people": [p["username"] for p in g["people"]], "agents": [m["address"] for m in g["members"]]}
                    for g in social.groups(current_user())
                ]
            }

        return await wrap(run)()

    def _group_view(user: dict, group_id: int, limit: int) -> list[dict]:
        items = []
        for m in social.timeline(group_id, user)[-max(1, min(limit, 200)):]:
            items.append({"message_id": m["id"], "from": m["username"], "content": m["content"], "at": m["created_at"]})
            for r in m["replies"]:
                items.append({"task_id": r["id"], "from": r["agent_address"], "status": r["status"],
                              "content": r["result"] or r["error"], "at": r["finished_at"] or r["created_at"]})
        return items

    @mcp.tool
    async def read_group(
        group: Annotated[str, Field(description="群 ID 或群名")],
        limit: Annotated[int, Field(description="返回最近多少条人发的消息（附带智能体回复）")] = 20,
    ) -> dict:
        """读取群聊记录，包括群里智能体的回复（会标记为已读）。"""

        async def run():
            user = current_user()
            g = social.resolve_group(user, group)
            social.mark_group_read(user, g["id"])
            return {"group_id": g["id"], "name": g["name"], "timeline": _group_view(user, g["id"], limit)}

        return await wrap(run)()

    @mcp.tool
    async def post_group(
        group: Annotated[str, Field(description="群 ID 或群名")],
        content: Annotated[str, Field(description="消息；@智能体地址或标识（如 @zhangsan/claude、@claude）会让它干活，@所有人（或 @所有智能体）叫上全部智能体，@用户名 只是提醒")],
        wait_seconds: Annotated[int, Field(description="等待被 @ 的智能体全部回复的秒数，0 不等待，最长 600")] = 0,
    ) -> dict:
        """在群里发言。被 @ 的智能体会以我的身份收到任务（需有其使用权限）。"""

        async def run():
            user = current_user()
            g = social.resolve_group(user, group)
            result = await social.post(user, g["id"], content)
            def ids():
                return [t["id"] for t in hub.db.all("SELECT id FROM tasks WHERE group_message_id = ? ORDER BY id",
                                                    (result["message_id"],))]

            def done():
                # hand-offs add tasks later: wait until the agents still waiting have run, or the chain stopped
                tasks = [hub.get_task(i) for i in ids()]
                if not all(t["status"] in FINISHED for t in tasks):
                    return None
                stopped = any(t["status"] != "completed" for t in tasks)
                ran = {t["agent_address"] for t in tasks}
                return tasks if stopped or all(w in ran for w in result["waiting"]) else None

            if ids() and wait_seconds > 0:
                await _wait_until(done, wait_seconds)
            return {**result, "replies": [_task_view(hub, hub.get_task(i), viewer=user) for i in ids()]}

        return await wrap(run)()

    @mcp.tool
    async def request_agent_access(
        agent: Annotated[str, Field(description="好友的智能体地址，如 zhangsan/claude")],
        note: Annotated[str, Field(description="申请理由，可为空")] = "",
    ) -> dict:
        """申请使用好友的智能体；对方同意并选择授权的工作区后即可派任务。"""

        async def run():
            row = social.request_contact(current_user(), agent, note)
            return {"status": row["status"]}

        return await wrap(run)()

    @mcp.tool
    async def answer_agent_access(
        request_id: Annotated[int, Field(description="inbox 返回的 access_requests[].request_id")],
        accept: bool,
        workspaces: Annotated[list[str], Field(description="同意时授权的工作区 ID 或名称列表（inbox 中列出）")] = [],
        trust: Annotated[bool, Field(description="同时设为免审批：对方派来的任务不再需要逐个批准")] = False,
    ) -> dict:
        """处理别人对我智能体的使用申请（需用户明确同意，并说明授权哪些工作区）。"""

        async def run():
            social.answer_contact(current_user(), request_id, accept, trust, workspaces)
            return {"ok": True}

        return await wrap(run)()

    @mcp.tool
    async def approve_task(
        task_id: int,
        approve: Annotated[bool, Field(description="true 批准执行，false 拒绝")] = True,
    ) -> dict:
        """审批别人派给我智能体、正在等待审批的任务（需用户明确同意）。"""

        async def run():
            user = current_user()
            return _task_view(hub, await hub.approve(user, task_id, approve), viewer=user)

        return await wrap(run)()

    return mcp


class McpAuthGate:
    """Reject unauthenticated MCP requests before they reach the FastMCP transport."""

    def __init__(self, app, hub: Hub):
        self.app = app
        self.hub = hub

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = bearer(headers)
            if not token or not resolve_api_token(self.hub.db, token, ("personal", "task")):
                response = JSONResponse({"error": "invalid or missing personal token"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
