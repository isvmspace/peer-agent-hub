"""Friends between people, direct messages, agent access grants and group chats with @mentions.

A person first befriends another person (the other side must accept). Only then may they ask for
access to that friend's agents; the owner decides which workspaces the friend can use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from . import redact
from .db import Database
from .hub import FINISHED, Hub, HubError, agent_address

log = logging.getLogger(__name__)

GROUP_CONTEXT_MESSAGES = 20
GROUP_CONTEXT_CHARS = 1500
GROUP_LATEST, GROUP_OLDER = 200, 100  # group messages per first load / per "load older" page
GROUP_SINCE_MAX = 1_000_000  # a live view keeps everything it has shown; it only grows as far as the reader scrolls back
DM_LATEST, DM_OLDER = 300, 100  # direct messages per first load / per "load older" page
DM_MAX_CHARS = 10_000
MENTION_ALL = ("all", "所有人", "全体", "所有智能体", "全部智能体")
PARALLEL = re.compile(r"同时|并行|一起|分别|各自")
MENTION = re.compile(r"@([\w.\-/\u4e00-\u9fff]+)")


class Social:
    def __init__(self, db: Database, hub: Hub):
        self.db = db
        self.hub = hub
        # group hand-offs: steps already relayed, and agents an earlier reply asked for that still wait their turn
        self._relayed: set[int] = set()
        self._handoffs: dict[int, list[tuple[int, str]]] = {}
        hub.on_finished.append(self.relay)
        # called with (sender, recipient_id) after a person sends a direct message (auto-reply listens here)
        self.on_direct: list = []

    # ---- friends (people) ---------------------------------------------

    def _user(self, target: str | int) -> dict:
        cols = "SELECT id, username, display_name, is_admin FROM users WHERE "
        if isinstance(target, int):
            row = self.db.one(cols + "id = ?", (target,))
        else:
            # a username wins over an id, so a numeric username never resolves to someone else's id
            key = str(target).strip().lstrip("@")
            row = self.db.one(cols + "username = ?", (key,)) or (
                self.db.one(cols + "id = ?", (int(key),)) if key.isdigit() else None
            )
        if not row:
            raise HubError(f"用户 {target} 不存在", 404)
        return row

    def friendship(self, a: int, b: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM friendships WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)",
            (a, b, b, a),
        )

    def are_friends(self, a: int, b: int) -> bool:
        if a == b:
            return True
        row = self.friendship(a, b)
        return bool(row and row["status"] == "accepted")

    def friends(self, user: dict) -> dict:
        rows = self.db.all(
            "SELECT f.*, u.username AS from_username, u.display_name AS from_display_name, "
            "v.username AS to_username, v.display_name AS to_display_name FROM friendships f "
            "JOIN users u ON u.id = f.user_id JOIN users v ON v.id = f.friend_id "
            "WHERE f.user_id = ? OR f.friend_id = ? ORDER BY f.id DESC",
            (user["id"], user["id"]),
        )
        agents = self.hub.list_agents(viewer=user)
        unread = {
            r["from_user_id"]: r["n"]
            for r in self.db.all(
                "SELECT from_user_id, COUNT(*) AS n FROM direct_messages WHERE to_user_id = ? AND read_at IS NULL "
                "GROUP BY from_user_id",
                (user["id"],),
            )
        }
        friends, outgoing, incoming = [], [], []
        for r in rows:
            mine = r["user_id"] == user["id"]
            other_id = r["friend_id"] if mine else r["user_id"]
            item = {
                "id": r["id"], "status": r["status"], "note": r["note"], "created_at": r["created_at"],
                "user_id": other_id,
                "username": r["to_username"] if mine else r["from_username"],
                "display_name": r["to_display_name"] if mine else r["from_display_name"],
            }
            if r["status"] == "accepted":
                item["agents"] = [a for a in agents if a["user_id"] == other_id]
                item["unread"] = unread.get(other_id, 0)
                last = self.db.one(
                    "SELECT content, created_at FROM direct_messages WHERE (from_user_id = ? AND to_user_id = ?) "
                    "OR (from_user_id = ? AND to_user_id = ?) ORDER BY id DESC LIMIT 1",
                    (user["id"], other_id, other_id, user["id"]),
                )
                item["last_message"] = last
                friends.append(item)
            else:
                (outgoing if mine else incoming).append(item)
        return {"friends": friends, "outgoing": outgoing, "incoming": incoming}

    def request_friend(self, user: dict, target: str | int, note: str = "") -> dict:
        other = self._user(target)
        if other["id"] == user["id"]:
            raise HubError("不能添加自己为好友")
        existing = self.friendship(user["id"], other["id"])
        if existing:
            if existing["status"] == "pending" and existing["friend_id"] == user["id"]:
                # they already asked me: adding back means accepting
                self.answer_friend(user, existing["id"], True)
                return self.friendship(user["id"], other["id"])
            return existing
        row_id = self.db.execute(
            "INSERT INTO friendships (user_id, friend_id, status, note, created_at) VALUES (?, ?, 'pending', ?, ?)",
            (user["id"], other["id"], (note or "").strip()[:200], time.time()),
        )
        self.db.audit(user["id"], "friend.request", friend=other["username"])
        self.hub.publish(f"user:{other['id']}")
        return self.db.one("SELECT * FROM friendships WHERE id = ?", (row_id,))

    def answer_friend(self, user: dict, friendship_id: int, accept: bool) -> None:
        row = self.db.one("SELECT * FROM friendships WHERE id = ?", (friendship_id,))
        if not row:
            raise HubError("好友申请不存在", 404)
        if row["friend_id"] != user["id"] or row["status"] != "pending":
            raise HubError("只能处理别人发给你的好友申请", 403)
        if accept:
            self.db.execute("UPDATE friendships SET status = 'accepted' WHERE id = ?", (friendship_id,))
        else:
            self.db.execute("DELETE FROM friendships WHERE id = ?", (friendship_id,))
        self.db.audit(user["id"], "friend.accept" if accept else "friend.reject", friendship_id=friendship_id)
        self.hub.publish(f"user:{row['user_id']}")

    def remove_friend(self, user: dict, friendship_id: int) -> None:
        row = self.db.one("SELECT * FROM friendships WHERE id = ?", (friendship_id,))
        if not row or user["id"] not in (row["user_id"], row["friend_id"]):
            raise HubError("好友关系不存在", 404)
        a, b = row["user_id"], row["friend_id"]
        self.db.execute("DELETE FROM friendships WHERE id = ?", (friendship_id,))
        # agent access only exists between friends
        self.db.execute(
            "DELETE FROM agent_contacts WHERE (user_id = ? AND agent_id IN (SELECT id FROM agents WHERE user_id = ?)) "
            "OR (user_id = ? AND agent_id IN (SELECT id FROM agents WHERE user_id = ?))",
            (a, b, b, a),
        )
        self.db.audit(user["id"], "friend.remove", friendship_id=friendship_id)
        self.hub.publish(f"user:{b if user['id'] == a else a}")

    # ---- direct messages ----------------------------------------------

    @staticmethod
    def dm_channel(a: int, b: int) -> str:
        return f"dm:{min(a, b)}:{max(a, b)}"

    def _friend(self, user: dict, target: str | int) -> dict:
        other = self._user(target)
        if other["id"] == user["id"] or not self.are_friends(user["id"], other["id"]):
            raise HubError(f"{other['username']} 还不是你的好友", 403)
        return other

    def direct_messages(self, user: dict, target: str | int, after: int = 0, before: int = 0) -> list[dict]:
        """The latest 300 messages after `after`, or with `before` the 100 just older than it (history paging)."""
        other = self._friend(user, target)
        rows = self.db.all(
            "SELECT m.id, m.from_user_id, m.to_user_id, m.content, m.created_at, u.username, u.display_name, "
            "m.auto_task_id IS NOT NULL AS auto, m.auto_agent "
            "FROM direct_messages m JOIN users u ON u.id = m.from_user_id WHERE m.id > ? AND m.id < ? AND "
            "((m.from_user_id = ? AND m.to_user_id = ?) OR (m.from_user_id = ? AND m.to_user_id = ?)) "
            # catching up after a cursor goes oldest-first so a long gap is filled page by page, never skipped
            f"ORDER BY m.id {'ASC' if after else 'DESC'} LIMIT {DM_OLDER if before else DM_LATEST}",
            (after, before or 1 << 62, user["id"], other["id"], other["id"], user["id"]),
        )
        if not after:
            rows.reverse()
        if before:
            return rows
        if self.db.update(
            "UPDATE direct_messages SET read_at = ? WHERE to_user_id = ? AND from_user_id = ? AND read_at IS NULL",
            (time.time(), user["id"], other["id"]),
        ):
            self.hub.publish(f"user:{user['id']}")
        return rows

    def send_direct(self, user: dict, target: str | int, content: str) -> dict:
        other = self._friend(user, target)
        content = (content or "").strip()
        if not content:
            raise HubError("消息不能为空")
        if len(content) > DM_MAX_CHARS:
            raise HubError(f"消息太长（{len(content)} 字），单条最多 {DM_MAX_CHARS} 字，请分开发送或作为附件")
        msg_id = self.db.execute(
            "INSERT INTO direct_messages (from_user_id, to_user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], other["id"], content, time.time()),
        )
        self.hub.publish(self.dm_channel(user["id"], other["id"]))
        self.hub.publish(f"user:{other['id']}")
        if self.on_direct:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for callback in self.on_direct if loop else []:
                loop.create_task(callback(user, other["id"]))
        return {"id": msg_id}

    # ---- agent access ("申请智能体权限") ---------------------------------

    def contacts(self, user: dict) -> dict:
        agents = {a["id"]: a for a in self.hub.list_agents(viewer=user)}
        full = {a["id"]: a for a in self.hub.list_agents()}
        rows = self.db.all(
            "SELECT c.*, u.username, u.display_name FROM agent_contacts c JOIN users u ON u.id = c.user_id "
            "JOIN agents a ON a.id = c.agent_id WHERE c.user_id = ? OR a.user_id = ? ORDER BY c.id DESC",
            (user["id"], user["id"]),
        )
        mine = [a for a in agents.values() if a["user_id"] == user["id"]]
        friends, outgoing, incoming, followers = [], [], [], []
        for row in rows:
            agent = agents.get(row["agent_id"])
            if not agent:
                continue
            item = {"id": row["id"], "status": row["status"], "note": row["note"], "created_at": row["created_at"],
                    "agent": agent, "username": row["username"], "display_name": row["display_name"],
                    "allow_full": bool(row["allow_full"])}
            if row["user_id"] == user["id"]:
                (friends if row["status"] == "accepted" else outgoing).append(item)
            else:
                # owner side: full agent (with paths) and the folders granted to this friend
                item["agent"] = full[row["agent_id"]]
                granted = set(json.loads(row["workspaces"] or "[]"))
                item["granted"] = [w["id"] for w in full[row["agent_id"]]["workspaces"] if w["path"] in granted]
                (followers if row["status"] == "accepted" else incoming).append(item)
        return {"mine": mine, "friends": friends, "outgoing": outgoing, "incoming": incoming, "followers": followers}

    def is_contact(self, user: dict, agent: dict) -> bool:
        if agent["user_id"] == user["id"]:
            return True
        return bool(self.db.one(
            "SELECT id FROM agent_contacts WHERE user_id = ? AND agent_id = ? AND status = 'accepted'",
            (user["id"], agent["id"]),
        ))

    def request_contact(self, user: dict, target: str | int, note: str = "") -> dict:
        agent = self.hub.resolve_agent(target)
        if agent["user_id"] == user["id"]:
            raise HubError("自己的智能体无需申请权限")
        if agent["hidden"] and not self.db.one(
            "SELECT 1 FROM agent_contacts WHERE user_id = ? AND agent_id = ? AND status = 'accepted'", (user["id"], agent["id"])
        ):
            # same answer as for a missing agent, so hidden ones can't be probed
            raise HubError(f"智能体不存在: {target}", 404)
        if not self.are_friends(user["id"], agent["user_id"]):
            raise HubError(f"请先添加 {agent['username']} 为好友，对方同意后才能申请其智能体的权限", 403)
        existing = self.db.one(
            "SELECT * FROM agent_contacts WHERE user_id = ? AND agent_id = ?", (user["id"], agent["id"])
        )
        if existing:
            return existing
        contact_id = self.db.execute(
            "INSERT INTO agent_contacts (user_id, agent_id, status, note, created_at) VALUES (?, ?, 'pending', ?, ?)",
            (user["id"], agent["id"], note.strip()[:200], time.time()),
        )
        self.db.audit(user["id"], "contact.request", agent=agent_address(agent))
        self.hub.publish(f"user:{agent['user_id']}")
        return self.db.one("SELECT * FROM agent_contacts WHERE id = ?", (contact_id,))

    def _contact(self, contact_id: int) -> dict:
        row = self.db.one(
            "SELECT c.*, a.user_id AS owner_user_id FROM agent_contacts c JOIN agents a ON a.id = c.agent_id "
            "WHERE c.id = ?",
            (contact_id,),
        )
        if not row:
            raise HubError("权限申请不存在", 404)
        return row

    def _grant_paths(self, agent_id: int, workspaces: list | None) -> list[str]:
        agent = self.hub.resolve_agent(agent_id)
        by_key = {}
        for w in self.hub.workspaces_of(agent):
            by_key[w["id"]] = by_key[w["path"]] = by_key[w["name"]] = w["path"]
        paths = []
        for key in workspaces or []:
            if str(key) not in by_key:
                raise HubError(f"工作区 {key} 不存在，请先在「智能体」页授权该文件夹")
            paths.append(by_key[str(key)])
        return sorted(set(paths))

    def set_contact_workspaces(self, user: dict, contact_id: int, workspaces: list) -> None:
        row = self._contact(contact_id)
        if row["owner_user_id"] != user["id"]:
            raise HubError("只有智能体归属人可以授权工作区", 403)
        paths = self._grant_paths(row["agent_id"], workspaces)
        self.db.execute("UPDATE agent_contacts SET workspaces = ? WHERE id = ?", (json.dumps(paths, ensure_ascii=False), contact_id))
        self.db.audit(user["id"], "contact.workspaces", contact_id=contact_id, workspaces=paths)

    def set_contact_full(self, user: dict, contact_id: int, allow: bool) -> None:
        """Let this person use the agent even while it runs with full permissions (an explicit owner decision)."""
        row = self._contact(contact_id)
        if row["owner_user_id"] != user["id"]:
            raise HubError("只有智能体归属人可以设置", 403)
        if row["status"] != "accepted":
            raise HubError("先同意对方的使用申请")
        if allow and self.hub.resolve_agent(row["agent_id"]).get("isolation") in ("sandbox", "limited"):
            raise HubError("该智能体不是完全权限，无需单独允许")
        self.db.execute("UPDATE agent_contacts SET allow_full = ? WHERE id = ?", (int(allow), contact_id))
        self.db.audit(user["id"], "contact.allow_full", contact_id=contact_id, allow=allow)

    def answer_contact(
        self, user: dict, contact_id: int, accept: bool, trust: bool = False, workspaces: list | None = None
    ) -> None:
        row = self._contact(contact_id)
        if row["owner_user_id"] != user["id"]:
            raise HubError("只有智能体归属人可以处理权限申请", 403)
        if accept:
            paths = self._grant_paths(row["agent_id"], workspaces)
            self.db.execute(
                "UPDATE agent_contacts SET status = 'accepted', workspaces = ? WHERE id = ?",
                (json.dumps(paths, ensure_ascii=False), contact_id),
            )
            if trust and not self.db.one(
                "SELECT id FROM approval_rules WHERE owner_user_id = ? AND from_user_id = ? AND agent_id = ?",
                (user["id"], row["user_id"], row["agent_id"]),
            ):
                self.db.execute(
                    "INSERT INTO approval_rules (owner_user_id, from_user_id, agent_id, note, enabled, created_at) "
                    "VALUES (?, ?, ?, '通过好友申请时设置', 1, ?)",
                    (user["id"], row["user_id"], row["agent_id"], time.time()),
                )
        else:
            self.db.execute("DELETE FROM agent_contacts WHERE id = ?", (contact_id,))
        self.db.audit(user["id"], "contact.accept" if accept else "contact.reject", contact_id=contact_id, trust=trust)

    def remove_contact(self, user: dict, contact_id: int) -> None:
        row = self._contact(contact_id)
        if user["id"] not in (row["user_id"], row["owner_user_id"]):
            raise HubError("无权操作", 403)
        self.db.execute("DELETE FROM agent_contacts WHERE id = ?", (contact_id,))
        self.db.audit(user["id"], "contact.remove", contact_id=contact_id)

    def conversations(self, user: dict, target: str | int) -> list[dict]:
        """Direct (non-group) task threads between this user and one agent, including ones shared with the user,
        newest first."""
        agent = self.hub.resolve_agent(target)
        rows = self.db.all(
            "SELECT id, title, status, turn, workspace, source, from_user_id, created_at, updated_at FROM tasks "
            "WHERE agent_id = ? AND group_id IS NULL AND (from_user_id = ? OR ? = ? OR "
            "id IN (SELECT task_id FROM task_shares WHERE user_id = ?)) ORDER BY updated_at DESC LIMIT 100",
            (agent["id"], user["id"], agent["user_id"], user["id"], user["id"]),
        )
        return [self.hub.present_task(user, {**r, "agent_id": agent["id"], "owner_user_id": agent["user_id"]}) for r in rows]

    # ---- groups -------------------------------------------------------

    def _group(self, group_id: int) -> dict:
        group = self.db.one(
            "SELECT g.*, u.username AS owner_username FROM chat_groups g JOIN users u ON u.id = g.owner_user_id "
            "WHERE g.id = ?",
            (group_id,),
        )
        if not group:
            raise HubError("群聊不存在", 404)
        return group

    def members(self, group_id: int, viewer: dict | None = None) -> list[dict]:
        """Agent members; for viewers who are not the owner the workspace is shown by name."""
        full = {a["id"]: a for a in self.hub.list_agents()}
        shown = {a["id"]: a for a in self.hub.list_agents(viewer=viewer)} if viewer else full
        rows = self.db.all("SELECT agent_id, workspace FROM group_agents WHERE group_id = ?", (group_id,))
        result = []
        for r in rows:
            if r["agent_id"] not in full:
                continue
            ws = r["workspace"]
            if viewer and ws and full[r["agent_id"]]["user_id"] != viewer["id"] and not viewer.get("is_admin"):
                ws = next((w["name"] for w in full[r["agent_id"]]["workspaces"] if w["path"] == ws), "")
            ws_id = next((w["id"] for w in full[r["agent_id"]]["workspaces"] if w["path"] == r["workspace"]), "")
            agent = shown.get(r["agent_id"])
            if agent is None:
                # hidden from this viewer: still a member of the group, but no folders or live load
                agent = {**full[r["agent_id"]], "workspaces": [], "workdir": "", "load": None, "access": "none"}
            result.append({**agent, "group_workspace": ws, "group_workspace_id": ws_id})
        return result

    def people(self, group_id: int) -> list[dict]:
        """Human members, the owner first."""
        return self.db.all(
            "SELECT u.id, u.username, u.display_name, (u.id = g.owner_user_id) AS is_owner FROM chat_groups g "
            "JOIN users u ON u.id = g.owner_user_id OR u.id IN (SELECT user_id FROM group_users WHERE group_id = g.id) "
            "WHERE g.id = ? ORDER BY is_owner DESC, u.username",
            (group_id,),
        )

    def can_view_group(self, user: dict, group: dict) -> bool:
        if user["is_admin"] or any(p["id"] == user["id"] for p in self.people(group["id"])):
            return True
        return any(m["user_id"] == user["id"] for m in self.members(group["id"]))

    def visible_group(self, user: dict, group_id: int) -> dict:
        group = self._group(group_id)
        if not self.can_view_group(user, group):
            raise HubError("无权查看该群聊", 403)
        return group

    def groups(self, user: dict) -> list[dict]:
        if user["is_admin"]:
            rows = self.db.all("SELECT * FROM chat_groups ORDER BY id DESC")
        else:
            rows = self.db.all(
                "SELECT DISTINCT g.* FROM chat_groups g LEFT JOIN group_agents ga ON ga.group_id = g.id "
                "LEFT JOIN agents a ON a.id = ga.agent_id LEFT JOIN group_users gu ON gu.group_id = g.id "
                "WHERE g.owner_user_id = ? OR a.user_id = ? OR gu.user_id = ? ORDER BY g.id DESC",
                (user["id"], user["id"], user["id"]),
            )
        for row in rows:
            row["members"] = [{"id": m["id"], "address": m["address"], "name": m["name"], "online": m["online"]}
                              for m in self.members(row["id"])]
            row["people"] = self.people(row["id"])
            row["unread"] = self.group_unread(user, row["id"])
            row["last_message"] = self.db.one(
                "SELECT m.content, m.created_at, u.username, u.display_name FROM group_messages m "
                "JOIN users u ON u.id = m.user_id WHERE m.group_id = ? ORDER BY m.id DESC LIMIT 1",
                (row["id"],),
            )
        return rows

    def resolve_group(self, user: dict, target: str | int) -> dict:
        """A group by id or by (unique) name among the groups this user can see."""
        key = str(target).strip()
        if key.isdigit():
            return self.visible_group(user, int(key))
        found = [g for g in self.groups(user) if g["name"] == key]
        if len(found) != 1:
            raise HubError(f"找不到群聊「{key}」" if not found else f"有多个群聊叫「{key}」，请用群 ID", 404)
        return self._group(found[0]["id"])

    def group_unread(self, user: dict, group_id: int) -> int:
        """Messages from other people and finished agent replies since this user last opened the group."""
        read = self.db.one(
            "SELECT last_message_id, read_at FROM group_reads WHERE group_id = ? AND user_id = ?", (group_id, user["id"])
        ) or {"last_message_id": 0, "read_at": 0}
        messages = self.db.one(
            "SELECT COUNT(*) AS n FROM group_messages WHERE group_id = ? AND user_id != ? AND id > ?",
            (group_id, user["id"], read["last_message_id"]),
        )["n"]
        replies = self.db.one(
            "SELECT COUNT(*) AS n FROM tasks WHERE group_id = ? AND finished_at > ?", (group_id, read["read_at"])
        )["n"]
        return messages + replies

    def mark_group_read(self, user: dict, group_id: int) -> None:
        last = self.db.one("SELECT COALESCE(MAX(id), 0) AS id FROM group_messages WHERE group_id = ?", (group_id,))["id"]
        read_at = self.db.one(
            "SELECT COALESCE(MAX(finished_at), 0) AS t FROM tasks WHERE group_id = ?", (group_id,)
        )["t"]
        changed = self.db.update(
            "INSERT INTO group_reads (group_id, user_id, last_message_id, read_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (group_id, user_id) DO UPDATE SET last_message_id = excluded.last_message_id, "
            "read_at = excluded.read_at "
            "WHERE last_message_id < excluded.last_message_id OR read_at < excluded.read_at",
            (group_id, user["id"], last, read_at),
        )
        if changed:
            self.hub.publish(f"user:{user['id']}")

    def _set_members(self, user: dict, group_id: int, members: list, people: list) -> None:
        items = []
        for m in members or []:
            target = m.get("agent") if isinstance(m, dict) else m
            agent = self.hub.resolve_agent(target)
            if agent["user_id"] != user["id"] and not self.is_contact(user, agent):
                raise HubError(f"你还没有 {agent_address(agent)} 的使用权限，请先向 {agent['username']} 申请")
            workspace = self.hub.check_workspace(user, agent, m.get("workspace", "") if isinstance(m, dict) else "")
            items.append((agent["id"], workspace))
        users = []
        for target in people or []:
            other = self._user(target)
            if other["id"] == user["id"]:
                continue
            if not self.are_friends(user["id"], other["id"]):
                raise HubError(f"{other['username']} 还不是你的好友，不能拉进群")
            users.append(other["id"])
        if not items and not users:
            raise HubError("群聊至少需要一位好友或一个智能体")
        self.db.execute("DELETE FROM group_agents WHERE group_id = ?", (group_id,))
        for agent_id, workspace in dict(items).items():
            self.db.execute(
                "INSERT INTO group_agents (group_id, agent_id, workspace) VALUES (?, ?, ?)", (group_id, agent_id, workspace)
            )
        self.db.execute("DELETE FROM group_users WHERE group_id = ?", (group_id,))
        for user_id in sorted(set(users)):
            self.db.execute("INSERT INTO group_users (group_id, user_id) VALUES (?, ?)", (group_id, user_id))
            self.hub.publish(f"user:{user_id}")

    def create_group(self, user: dict, name: str, members: list, people: list | None = None) -> dict:
        name = (name or "").strip()[:60]
        if not name:
            raise HubError("群名不能为空")
        group_id = self.db.execute(
            "INSERT INTO chat_groups (name, owner_user_id, created_at) VALUES (?, ?, ?)", (name, user["id"], time.time())
        )
        try:
            self._set_members(user, group_id, members, people or [])
        except HubError:
            self.db.execute("DELETE FROM chat_groups WHERE id = ?", (group_id,))
            raise
        self.db.audit(user["id"], "group.create", group_id=group_id)
        return self._group(group_id)

    def update_group(
        self, user: dict, group_id: int, name: str | None, members: list | None, people: list | None = None
    ) -> dict:
        group = self._group(group_id)
        if group["owner_user_id"] != user["id"]:
            raise HubError("只有群主可以修改群聊", 403)
        if name is not None and not isinstance(name, str):
            raise HubError("群名必须是文字")  # checked before any write, so a bad edit changes nothing
        if members is not None or people is not None:
            current_people = [p["id"] for p in self.people(group_id) if not p["is_owner"]]
            current_agents = [{"agent": m["id"], "workspace": m["group_workspace_id"]} for m in self.members(group_id)]
            self._set_members(
                user, group_id, current_agents if members is None else members, current_people if people is None else people
            )
        # renamed only after the member change passed its checks, so a rejected edit changes nothing
        if name is not None and name.strip():
            self.db.execute("UPDATE chat_groups SET name = ? WHERE id = ?", (name.strip()[:60], group_id))
        self.db.audit(user["id"], "group.update", group_id=group_id)
        self.hub.publish(f"group:{group_id}")
        return self._group(group_id)

    def leave_group(self, user: dict, group_id: int) -> None:
        group = self._group(group_id)
        if group["owner_user_id"] == user["id"]:
            raise HubError("群主不能退出，可以解散群聊")
        self.db.execute("DELETE FROM group_users WHERE group_id = ? AND user_id = ?", (group_id, user["id"]))
        self.hub.publish(f"group:{group_id}")

    def delete_group(self, user: dict, group_id: int) -> None:
        group = self._group(group_id)
        if group["owner_user_id"] != user["id"] and not user["is_admin"]:
            raise HubError("只有群主可以解散群聊", 403)
        self.db.execute("UPDATE tasks SET group_id = NULL WHERE group_id = ?", (group_id,))
        self.db.execute("DELETE FROM group_messages WHERE group_id = ?", (group_id,))
        self.db.execute("DELETE FROM group_agents WHERE group_id = ?", (group_id,))
        self.db.execute("DELETE FROM group_users WHERE group_id = ?", (group_id,))
        self.db.execute("DELETE FROM group_reads WHERE group_id = ?", (group_id,))
        self.db.execute("DELETE FROM chat_groups WHERE id = ?", (group_id,))
        self.db.audit(user["id"], "group.delete", group_id=group_id)

    def timeline(self, group_id: int, viewer: dict | None = None, before: int = 0, since: int = 0) -> list[dict]:
        """The latest 200 messages with their replies; with `before` the 100 just older than it (history paging);
        with `since` everything from that message on, so a live view keeps what it already shows."""
        limit = GROUP_OLDER if before else GROUP_SINCE_MAX if since else GROUP_LATEST
        messages = self.db.all(
            "SELECT m.id, m.user_id, m.content, m.mentions, m.created_at, u.username, u.display_name FROM group_messages m "
            f"JOIN users u ON u.id = m.user_id WHERE m.group_id = ? AND m.id < ? AND m.id >= ? ORDER BY m.id DESC LIMIT {limit}",
            (group_id, before or 1 << 62, since),
        )
        if not messages:
            return []
        tasks = self.db.all(
            "SELECT t.id, t.agent_id, a.user_id AS owner_user_id, t.group_message_id, t.status, t.result, t.error, "
            "t.workspace, t.created_at, t.finished_at, "
            "a.key AS agent_key, a.name AS agent_name, u.username AS owner_username FROM tasks t "
            "JOIN agents a ON a.id = t.agent_id JOIN users u ON u.id = a.user_id WHERE t.group_id = ? "
            "AND t.group_message_id BETWEEN ? AND ? ORDER BY t.id",
            (group_id, messages[-1]["id"], messages[0]["id"]),
        )
        by_message: dict[int, list] = {}
        for t in tasks:
            t["agent_address"] = f"{t['owner_username']}/{t['agent_key']}"
            if viewer:
                t.update(self.hub.present_task(viewer, t))
            t["attachments"] = self.hub.attachments(t["id"])
            if t["status"] not in FINISHED and viewer:
                last = self.db.one(
                    "SELECT id, content, payload FROM task_events WHERE task_id = ? AND kind = 'output' ORDER BY id DESC LIMIT 1",
                    (t["id"],),
                )
                live = last["content"] if last else ""
                if not self.hub.sees_process(viewer, t):
                    live = "***" if last and self.hub._in_key(t["id"], last) else redact.mask(live)
                t["live"] = live
            by_message.setdefault(t["group_message_id"], []).append(t)
        messages.reverse()
        for m in messages:
            m["mentions"] = json.loads(m["mentions"] or "[]")
            m["replies"] = by_message.get(m["id"], [])
        return messages

    def _context(self, group_id: int, viewer: dict) -> str:
        """Recent group talk for a new task, as the asking member sees it: a colleague's masked errors stay masked."""
        lines = []
        for m in self.timeline(group_id, viewer)[-GROUP_CONTEXT_MESSAGES:]:
            lines.append(f"[{m['username']}] {m['content'][:GROUP_CONTEXT_CHARS]}")
            for r in m["replies"]:
                text = r["result"] or r["error"] or f"（{r['status']}）"
                lines.append(f"  [{r['agent_address']}] {text[:GROUP_CONTEXT_CHARS]}")
        return "\n".join(lines)

    def _mentions(self, content: str, agents: list[dict], people: list[dict]) -> tuple[list[dict], list[str]]:
        """Resolve @tokens to agent members (by address, or by key when unique) and people (by username)."""
        by_name: dict[str, list[dict]] = {}
        for a in agents:
            by_name.setdefault(a["address"].lower(), []).append(a)
            by_name.setdefault(a["key"].lower(), []).append(a)
        usernames = {p["username"].lower(): p["username"] for p in people}
        hit_agents, hit_people = {}, []
        for token in MENTION.findall(content):
            key = token.rstrip(".,，。:：").lower()
            if key in MENTION_ALL:
                hit_agents.update({a["id"]: a for a in agents})
                continue
            found = by_name.get(key, [])
            if len(found) == 1:
                hit_agents[found[0]["id"]] = found[0]
            elif key in usernames:
                hit_people.append(usernames[key])
        return list(hit_agents.values()), sorted(set(hit_people))

    async def post(self, user: dict, group_id: int, content: str, targets: list | None = None) -> dict:
        group = self.visible_group(user, group_id)
        if not user["is_admin"] and not any(p["id"] == user["id"] for p in self.people(group_id)):
            raise HubError("你不是群成员，不能发言", 403)
        content = (content or "").strip()
        if not content:
            raise HubError("消息不能为空")
        members = self.members(group_id)
        people = self.people(group_id)
        mentioned, mentioned_people = self._mentions(content, members, people)
        if targets:
            wanted = {str(t) for t in targets}
            mentioned += [m for m in members if (str(m["id"]) in wanted or m["address"] in wanted) and m not in mentioned]
        everyone = not mentioned and len(people) == 1
        if everyone:
            mentioned = members  # a group of just me and agents: everyone answers
        # several agents: the first answers and hands off by @-mentioning the others in its reply,
        # unless the message asks for them at the same time
        parallel = everyone or len(mentioned) < 2 or bool(PARALLEL.search(content)) or any(
            t.rstrip(".,，。:：").lower() in MENTION_ALL for t in MENTION.findall(content))
        first = mentioned if parallel else mentioned[:1]
        message_id = self.db.execute(
            "INSERT INTO group_messages (group_id, user_id, content, mentions, created_at) VALUES (?, ?, ?, ?, ?)",
            (group_id, user["id"], content, json.dumps([m["address"] for m in mentioned] + mentioned_people,
                                                        ensure_ascii=False), time.time()),
        )
        self.mark_group_read(user, group_id)
        errors = []
        for m in first:
            if parallel:
                others = [o["address"] for o in mentioned if o["id"] != m["id"]]
                note = f"这条消息同时 @ 了 {'、'.join(others)}，请专注于你能完成的部分，必要时说明分工。" if others else ""
            else:
                waiting = "、".join(o["address"] for o in mentioned[1:])
                note = (f"这条消息还 @ 了 {waiting}，它们在等你先完成。需要它们做什么，就在回复里 @ 它（写完整地址）并写清要求，"
                        "你的回复会自动转给被 @ 的智能体；不 @ 的话，它们会在你完成后按顺序依次收到原消息。")
            error = await self._dispatch(user, group, m, f"{user['username']} 的新消息：\n{content}", note, message_id)
            if error:
                errors.append(error)
        self.hub.publish(f"group:{group_id}")
        for p in people:
            if p["id"] != user["id"]:
                self.hub.publish(f"user:{p['id']}")
        return {"message_id": message_id, "errors": errors, "dispatched": [m["address"] for m in first],
                "waiting": [m["address"] for m in mentioned if m not in first]}

    async def _dispatch(self, user: dict, group: dict, m: dict, message: str, note: str, message_id: int,
                        parent: dict | None = None) -> str:
        topic = self.db.one("SELECT content FROM group_messages WHERE id = ?", (message_id,))["content"]
        """One group task for agent `m`, as `user`; returns an error text instead of raising."""
        members = self.members(group["id"])
        roster = "、".join(a["address"] for a in members)
        humans = "、".join(p["username"] for p in self.people(group["id"]))
        prompt = f"你是 {m['address']}，在 Agent Hub 群聊「{group['name']}」中。群成员：{humans}；智能体：{roster}。{note}\n"
        context = self._context(group["id"], user)
        if context:
            prompt += f"\n群聊最近记录：\n{context}\n"
        prompt += "\n" + message
        try:
            await self.hub.create_task(
                user, m["id"], prompt, title=f"[{group['name']}] {topic.splitlines()[0]}"[:120],
                from_agent=parent["agent_key"] if parent else "", parent_task_id=parent["id"] if parent else None,
                workspace=m["group_workspace_id"], group_id=group["id"], group_message_id=message_id,
            )
            return ""
        except HubError as exc:
            return f"{m['address']}: {exc}"

    async def relay(self, task_id: int) -> None:
        """Hand-off within a group message, strictly one agent at a time. When a step completes, the next agent is
        the first one its reply @-mentions (others it mentions queue up behind), else the next agent still waiting
        from the original message. A failed or canceled step ends the chain. Every step hangs under the message's
        first task, so cancelling that tree stops the rest and long chains stay within the delegation depth limit."""
        task = self.hub.get_task(task_id)
        if not task["group_id"] or not task["group_message_id"] or task["turn"] != 1 or task["status"] != "completed":
            return
        if task_id in self._relayed:
            return
        self._relayed.add(task_id)
        group = self.db.one("SELECT * FROM chat_groups WHERE id = ?", (task["group_id"],))
        message = self.db.one("SELECT * FROM group_messages WHERE id = ?", (task["group_message_id"],))
        if not group or not message:
            return
        busy = self.db.one(
            "SELECT 1 FROM tasks WHERE group_message_id = ? AND id != ? AND status NOT IN (%s)" % ",".join("?" * len(FINISHED)),
            (message["id"], task_id, *FINISHED),
        )
        if busy:
            return  # sent at the same time on purpose; no chain to continue
        user = self.hub.get_user(message["user_id"])
        members = {m["id"]: m for m in self.members(group["id"])}
        reply = task["result"] or ""
        dispatched = {r["agent_id"] for r in self.db.all("SELECT agent_id FROM tasks WHERE group_message_id = ?", (message["id"],))}
        queue = self._handoffs.setdefault(message["id"], [])
        handoff = f"{task['agent_address']} 在群里接力给你（{user['username']} 的原消息：{message['content']}）：\n{reply}"
        for a in self._mentions(reply, list(members.values()), [])[0]:
            if a["id"] != task["agent_id"] and a["id"] not in dispatched and all(a["id"] != q[0] for q in queue):
                queue.append((a["id"], handoff))
        if not queue:
            wanted = json.loads(message["mentions"] or "[]")
            queue += [(a["id"], "") for a in sorted(members.values(), key=lambda a: wanted.index(a["address"]) if a["address"] in wanted else 0)
                      if a["address"] in wanted and a["id"] not in dispatched]
        errors = []
        while queue:
            agent_id, asked = queue.pop(0)
            if agent_id not in members or agent_id in dispatched:
                continue
            rest = "、".join(members[q[0]]["address"] for q in queue if q[0] in members)
            note = "这是群聊接力任务：按顺序轮到你了，请完成你的部分。"
            if rest:
                note += f"之后还有 {rest} 在等你；需要它们做什么，就在回复里 @ 它并写清要求。"
            body = asked or f"{user['username']} 的新消息：\n{message['content']}"
            if not asked or asked != handoff:
                body += f"\n\n上一棒 {task['agent_address']} 的回复：\n{reply}"
            root = self.db.one("SELECT id FROM tasks WHERE group_message_id = ? ORDER BY id LIMIT 1", (message["id"],))
            error = await self._dispatch(user, group, members[agent_id], body, note, message["id"],
                                         {"id": root["id"], "agent_key": task["agent_key"]})
            if error:
                errors.append(error)
                continue
            break
        if not queue:
            self._handoffs.pop(message["id"], None)
        if errors:
            log.warning("group relay for task %s: %s", task_id, "; ".join(errors))
        self.hub.publish(f"group:{group['id']}")
