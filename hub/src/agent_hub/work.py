"""「工作」menu: each colleague gets a container of their own on a work server and talks to its agents there.

A work server is a connector running in workhost mode: it starts one container per colleague, clones their
repositories into folders they group as directories, and gives every conversation (a work session) its own
git worktree per repository, so sessions never touch each other's files. A session is an ordinary task row
underneath (turns, events, cancel and acks all reuse the task machinery) but it is listed only here.

The colleague's GitLab token is stored encrypted and only travels to the connector for one git command at a
time; it is never written into the container. The real folder names in the container are generated ids, so
renaming a directory moves nothing and no server path is ever shown.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import gitlab
from .db import Database
from .hub import ACTIVE, FINISHED, IMAGE_EMPTY_PROMPT, Hub, HubError, agent_address

LOGGER = logging.getLogger(__name__)

PUSH_MODES = ("confirm", "direct")
ON_LIMIT = ("auto", "confirm")
# what the agent CLIs say when an account has run out (Codex: "You've hit your usage limit")
LIMIT_ERROR = re.compile(r"usage limit|rate limit|out of .*messages|quota", re.I)
DEFAULT_SETTINGS = {"auto_switch": 1, "threshold": 20, "on_limit": "auto", "current": "{}"}
# times shown to colleagues and the "today" of usage stats are Beijing time, whatever the server's zone is
LOCAL_TZ = timezone(timedelta(hours=8))
CONTINUE_AFTER_SWITCH ="（额度已自动切换）请继续刚才的任务，从中断的地方接着做。"
MAX_ACTIVE_SESSIONS = 3
GIT_URL = re.compile(r"^(https?://[^\s]+|file:///[^\s]+)$")
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
CLONE_TIMEOUT = 600
GIT_TIMEOUT = 120


class Vault:
    """Encrypts short secrets (GitLab tokens) with the hub secret: SHA-256 keystream plus an HMAC tag."""

    def __init__(self, secret: str):
        self.key = hashlib.sha256(("work-vault\n" + secret).encode()).digest()

    def _stream(self, nonce: bytes, size: int) -> bytes:
        out = b""
        counter = 0
        while len(out) < size:
            out += hashlib.sha256(self.key + nonce + counter.to_bytes(8, "big")).digest()
            counter += 1
        return out[:size]

    def seal(self, plain: str) -> str:
        if not plain:
            return ""
        data = plain.encode()
        nonce = secrets.token_bytes(16)
        cipher = bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data))))
        tag = hmac.new(self.key, nonce + cipher, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(nonce + tag + cipher).decode()

    def open(self, sealed: str) -> str:
        if not sealed:
            return ""
        raw = base64.urlsafe_b64decode(sealed.encode())
        nonce, tag, cipher = raw[:16], raw[16:32], raw[32:]
        if not hmac.compare_digest(tag, hmac.new(self.key, nonce + cipher, hashlib.sha256).digest()[:16]):
            raise HubError("GitLab 令牌无法解密，请在「工作」里重新连接", 409)
        return bytes(a ^ b for a, b in zip(cipher, self._stream(nonce, len(cipher)))).decode()


def _clean(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


def repo_name_of(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


class Work:
    def __init__(self, db: Database, hub: Hub, secret: str):
        self.db = db
        self.hub = hub
        self.vault = Vault(secret)
        hub.payload_hooks.append(self._payload)
        hub.on_finished.append(self._on_finished)

    # ---- servers and the colleague's profile --------------------------------

    def servers(self, viewer: dict | None = None) -> list[dict]:
        """Connectors serving workhost agents, with those agents (what a new session can pick).

        With a viewer, only the servers that person may use: everyone's, or those whose owner listed them.
        """
        rows = self.db.all(
            "SELECT a.id, a.key, a.name, a.kind, a.description, a.online, a.connector_id, c.name AS connector_name, "
            "c.user_id AS owner_id, u.username AS owner_username, u.display_name AS owner_name "
            "FROM agents a JOIN connectors c ON c.id = a.connector_id JOIN users u ON u.id = c.user_id "
            "WHERE a.workhost = 1 ORDER BY a.connector_id, a.key"
        )
        servers: dict[int, dict] = {}
        for row in rows:
            if viewer is not None and not self.may_use(viewer, row["connector_id"], row["owner_id"]):
                continue
            server = servers.setdefault(row["connector_id"], {
                "id": row["connector_id"], "name": row["connector_name"], "online": False, "agents": [],
                "users": self.db.one("SELECT COUNT(*) AS n FROM work_profiles WHERE connector_id = ?",
                                     (row["connector_id"],))["n"],
                "owner": row["owner_name"] or row["owner_username"], "owner_id": row["owner_id"],
                "access": self._access(row["connector_id"]),
            })
            server["online"] = server["online"] or bool(row["online"])
            server["agents"].append({"id": row["id"], "key": row["key"], "name": row["name"], "kind": row["kind"],
                                     "description": row["description"], "online": bool(row["online"])})
        return list(servers.values())

    def profile(self, user: dict) -> dict | None:
        row = self.db.one("SELECT * FROM work_profiles WHERE user_id = ?", (user["id"],))
        if not row:
            return None
        server = next((s for s in self.servers() if s["id"] == row["connector_id"]), None)
        return {
            "connector_id": row["connector_id"], "server": server, "git_name": row["git_name"],
            "git_email": row["git_email"], "has_token": bool(row["git_token"]), "push_mode": row["push_mode"],
            "container": container_name(user["id"]), "token_kind": row["token_kind"],
        }

    def _profile_row(self, user: dict) -> dict:
        row = self.db.one("SELECT * FROM work_profiles WHERE user_id = ?", (user["id"],))
        if not row:
            raise HubError("还没有连接工作服务器，请先在「工作」里建立连接", 409)
        return row

    def _access(self, connector_id: int) -> str:
        row = self.db.one("SELECT access FROM work_servers WHERE connector_id = ?", (connector_id,))
        return row["access"] if row else "all"

    def may_use(self, user: dict, connector_id: int, owner_id: int | None = None) -> bool:
        if owner_id is None:
            row = self.db.one("SELECT user_id FROM connectors WHERE id = ?", (connector_id,))
            owner_id = row["user_id"] if row else None
        if user.get("is_admin") or user["id"] == owner_id or self._access(connector_id) == "all":
            return True
        return bool(self.db.one("SELECT 1 FROM work_server_users WHERE connector_id = ? AND user_id = ?",
                                (connector_id, user["id"])))

    def pick_server(self, user: dict) -> int:
        """Automatic placement for a new colleague: the online work server with the fewest colleagues on it."""
        online = [s for s in self.servers(user) if s["online"]]
        if not online:
            raise HubError("没有在线的工作服务器", 409)
        return min(online, key=lambda s: (s["users"], s["id"]))["id"]

    async def connect(self, user: dict, connector_id: int, git_name: str, git_email: str, token: str,
                      push_mode: str = "confirm") -> dict:
        git_name, git_email = _clean(git_name, 80), _clean(git_email, 120)
        if not git_name or "@" not in git_email:
            raise HubError("请填写提交用的姓名和邮箱")
        if push_mode not in PUSH_MODES:
            raise HubError("推送方式只能是 confirm 或 direct")
        existing = self.db.one("SELECT * FROM work_profiles WHERE user_id = ?", (user["id"],))
        if not connector_id:
            # a colleague already placed stays where their files are
            connector_id = existing["connector_id"] if existing else self.pick_server(user)
        if not any(s["id"] == connector_id and s["online"] for s in self.servers(user)):
            raise HubError("工作服务器不在线、不存在，或它的归属人没有开放给你", 409)
        if existing and existing["connector_id"] != connector_id and self.db.one(
                "SELECT 1 FROM work_dirs WHERE user_id = ?", (user["id"],)):
            raise HubError("你的目录和仓库在原来的工作服务器上，暂不支持换服务器", 409)
        kind, refresh, expires = "pat", "", 0.0
        pending = self.db.one("SELECT * FROM work_gitlab_pending WHERE user_id = ?", (user["id"],))
        if token.strip():
            await self._check_token(token.strip())
            sealed = self.vault.seal(token.strip())
        elif pending:
            sealed, kind, refresh, expires = pending["access"], "oauth", pending["refresh"], pending["expires_at"]
        elif existing:
            sealed, kind = existing["git_token"], existing["token_kind"]
            refresh, expires = existing["refresh_token"], existing["token_expires"]
        else:
            sealed = ""
        now = time.time()
        await self.hub.ask_connector(connector_id, {
            "type": "work_ensure", "work": self._identity(user, git_name, git_email),
        }, timeout=180)
        self.db.execute(
            "INSERT INTO work_profiles (user_id, connector_id, git_name, git_email, git_token, push_mode, created_at, "
            "updated_at, token_kind, refresh_token, token_expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET connector_id = excluded.connector_id, "
            "git_name = excluded.git_name, git_email = excluded.git_email, git_token = excluded.git_token, "
            "push_mode = excluded.push_mode, updated_at = excluded.updated_at, token_kind = excluded.token_kind, "
            "refresh_token = excluded.refresh_token, token_expires = excluded.token_expires",
            (user["id"], connector_id, git_name, git_email, sealed, push_mode, now, now, kind, refresh, expires),
        )
        self.db.execute("DELETE FROM work_gitlab_pending WHERE user_id = ?", (user["id"],))
        self.db.audit(user["id"], "work.connect", connector_id=connector_id)
        return self.profile(user)

    # ---- GitLab: tokens, OAuth, projects, merge requests ------------------------

    async def _check_token(self, token: str) -> None:
        """Turns away a token GitLab rejects; when GitLab can't be asked, the token is kept and git will tell."""
        try:
            status, _ = await gitlab.request(gitlab.base_url(), {"kind": "pat", "value": token}, "GET", "/user")
        except HubError:
            return
        if status in (401, 403):
            raise HubError("GitLab 不认这个令牌：可能填错、已过期或被撤销，请重新生成", 400)

    async def _fresh(self, user: dict) -> dict:
        """The colleague's GitLab token as {kind, value}; an OAuth token is refreshed shortly before it expires."""
        row = self._profile_row(user)
        if row["token_kind"] == "oauth" and row["refresh_token"] and row["token_expires"] < time.time() + 120:
            cfg = gitlab.oauth_config()
            if cfg:
                got = await gitlab.refresh_token(cfg, self.vault.open(row["refresh_token"]))
                self.db.execute(
                    "UPDATE work_profiles SET git_token = ?, refresh_token = ?, token_expires = ? WHERE user_id = ?",
                    (self.vault.seal(got["access_token"]), self.vault.seal(got.get("refresh_token", "")),
                     time.time() + int(got.get("expires_in") or 7200), user["id"]),
                )
                row = self._profile_row(user)
        return {"kind": row["token_kind"], "value": self.vault.open(row["git_token"])}

    def oauth_enabled(self) -> bool:
        return gitlab.oauth_config() is not None

    def oauth_start(self, user: dict, secret: str) -> str:
        cfg = gitlab.oauth_config()
        if not cfg:
            raise HubError("管理员还没有配置 GitLab 授权，请填写个人访问令牌", 409)
        stamp = str(int(time.time()))
        sig = hmac.new(secret.encode(), f"gitlab-oauth\n{user['id']}\n{stamp}".encode(), hashlib.sha256).hexdigest()[:32]
        return gitlab.authorize_url(cfg, f"{user['id']}.{stamp}.{sig}")

    async def oauth_callback(self, code: str, state: str, secret: str) -> dict:
        cfg = gitlab.oauth_config()
        if not cfg:
            raise HubError("没有配置 GitLab 授权", 409)
        try:
            user_id, stamp, sig = state.split(".")
            expected = hmac.new(secret.encode(), f"gitlab-oauth\n{user_id}\n{stamp}".encode(), hashlib.sha256).hexdigest()[:32]
            valid = hmac.compare_digest(sig, expected) and time.time() - int(stamp) < 600
        except ValueError:
            valid = False
        if not valid:
            raise HubError("授权链接无效或已过期，请重新发起", 400)
        got = await gitlab.exchange_code(cfg, code)
        token = {"kind": "oauth", "value": got["access_token"]}
        who = await gitlab.current_user(cfg["base"], token)
        self.db.execute(
            "INSERT INTO work_gitlab_pending (user_id, access, refresh, expires_at, username, name, email, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET access = excluded.access, "
            "refresh = excluded.refresh, expires_at = excluded.expires_at, username = excluded.username, "
            "name = excluded.name, email = excluded.email, created_at = excluded.created_at",
            (int(user_id), self.vault.seal(got["access_token"]), self.vault.seal(got.get("refresh_token", "")),
             time.time() + int(got.get("expires_in") or 7200), who["username"] or "", who["name"] or "",
             who["email"] or "", time.time()),
        )
        if self.db.one("SELECT 1 FROM work_profiles WHERE user_id = ?", (int(user_id),)):
            # already connected: switch the profile to the new authorization right away
            pending = self.db.one("SELECT * FROM work_gitlab_pending WHERE user_id = ?", (int(user_id),))
            self.db.execute(
                "UPDATE work_profiles SET git_token = ?, token_kind = 'oauth', refresh_token = ?, token_expires = ? "
                "WHERE user_id = ?", (pending["access"], pending["refresh"], pending["expires_at"], int(user_id)))
            self.db.execute("DELETE FROM work_gitlab_pending WHERE user_id = ?", (int(user_id),))
        self.db.audit(int(user_id), "work.gitlab.authorize", gitlab_user=who["username"])
        return who

    def gitlab_pending(self, user: dict) -> dict | None:
        row = self.db.one("SELECT username, name, email FROM work_gitlab_pending WHERE user_id = ?", (user["id"],))
        return dict(row) if row else None

    async def projects(self, user: dict, search: str) -> list[dict]:
        token = await self._fresh(user)
        if not token["value"]:
            raise HubError("没有 GitLab 令牌，无法列出项目，请直接填仓库地址", 409)
        return await gitlab.projects(gitlab.base_url(), token, _clean(search, 80))

    async def create_mrs(self, user: dict, session_id: int, repos: list[str] | None = None) -> list[dict]:
        row = self._session(user, session_id)
        token = await self._fresh(user)
        if not token["value"]:
            raise HubError("没有 GitLab 令牌，无法创建合并请求", 409)
        status = await self.changes(user, session_id)
        targets = {r["name"]: r for r in self.db.all(
            "SELECT name, url, branch FROM work_repos WHERE dir_id = ?", (row["dir_id"],))}
        results = []
        for st in status["repos"]:
            name = st["repo"]
            if repos and name not in repos:
                continue
            if not st.get("commits"):
                results.append({"repo": name, "skipped": "没有提交"})
                continue
            if st.get("unpushed"):
                results.append({"repo": name, "skipped": "还有提交没推送，先推送再建合并请求"})
                continue
            repo = targets.get(name)
            if not repo or not repo["url"].startswith("http"):
                results.append({"repo": name, "skipped": "不是 GitLab 仓库"})
                continue
            project = gitlab.project_path(repo["url"])
            commits = "\n".join(f"- {c['subject']}" for c in st["commits"][:30])
            mr = await gitlab.create_mr(gitlab.base_url(repo["url"]), token, project, row["branch"], repo["branch"],
                                        row["title"], f"由 Agent Hub 工作会话 #{row['id']}（{row['agent_name']}）创建。\n\n{commits}")
            self._save_mr(row["id"], name, project, mr)
            results.append({"repo": name, **mr})
        self.db.audit(user["id"], "work.mr.create", session_id=session_id,
                      repos=[r["repo"] for r in results if r.get("iid")])
        return results

    def _save_mr(self, session_id: int, repo: str, project: str, mr: dict) -> None:
        self.db.execute(
            "INSERT INTO work_mrs (session_id, repo, project, iid, url, state, merge_status, pipeline, pipeline_url, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, repo) DO UPDATE SET project = excluded.project, "
            "iid = excluded.iid, url = excluded.url, state = excluded.state, merge_status = excluded.merge_status, "
            "pipeline = excluded.pipeline, pipeline_url = excluded.pipeline_url, updated_at = excluded.updated_at",
            (session_id, repo, project, int(mr["iid"]), mr.get("url") or "", mr.get("state") or "",
             mr.get("merge_status") or "", mr.get("pipeline") or "", mr.get("pipeline_url") or "", time.time()),
        )

    async def mrs(self, user: dict, session_id: int, refresh: bool = False) -> list[dict]:
        row = self._session(user, session_id)
        saved = self.db.all("SELECT * FROM work_mrs WHERE session_id = ? ORDER BY repo", (row["id"],))
        if refresh and saved:
            token = await self._fresh(user)
            for mr in saved:
                fresh = await gitlab.mr_status(gitlab.base_url(), token, mr["project"], mr["iid"])
                self._save_mr(row["id"], mr["repo"], mr["project"], fresh)
            saved = self.db.all("SELECT * FROM work_mrs WHERE session_id = ? ORDER BY repo", (row["id"],))
        return saved

    def _identity(self, user: dict, git_name: str, git_email: str) -> dict:
        return {"user": container_name(user["id"]), "git_name": git_name, "git_email": git_email}

    # ---- directories and repositories ---------------------------------------

    def dirs(self, user: dict) -> list[dict]:
        out = []
        for d in self.db.all("SELECT * FROM work_dirs WHERE user_id = ? ORDER BY id", (user["id"],)):
            repos = self.db.all("SELECT id, name, url, branch FROM work_repos WHERE dir_id = ? ORDER BY id", (d["id"],))
            out.append({"id": d["id"], "name": d["name"], "description": d["description"], "repos": repos})
        return out

    def _dir(self, user: dict, dir_id: int) -> dict:
        row = self.db.one("SELECT * FROM work_dirs WHERE id = ? AND user_id = ?", (dir_id, user["id"]))
        if not row:
            raise HubError("目录不存在", 404)
        return row

    def create_dir(self, user: dict, name: str, description: str = "") -> dict:
        self._profile_row(user)
        name = _clean(name, 40)
        if not name:
            raise HubError("目录名不能为空")
        if self.db.one("SELECT 1 FROM work_dirs WHERE user_id = ? AND name = ?", (user["id"], name)):
            raise HubError(f"已经有叫「{name}」的目录")
        dir_id = self.db.execute(
            "INSERT INTO work_dirs (user_id, name, description, folder, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], name, _clean(description, 500), "d-" + secrets.token_hex(4), time.time()),
        )
        return next(d for d in self.dirs(user) if d["id"] == dir_id)

    def update_dir(self, user: dict, dir_id: int, name: str, description: str) -> dict:
        self._dir(user, dir_id)
        name = _clean(name, 40)
        if not name:
            raise HubError("目录名不能为空")
        if self.db.one("SELECT 1 FROM work_dirs WHERE user_id = ? AND name = ? AND id != ?", (user["id"], name, dir_id)):
            raise HubError(f"已经有叫「{name}」的目录")
        # only the label changes: the folder in the container stays where it is
        self.db.execute("UPDATE work_dirs SET name = ?, description = ? WHERE id = ?",
                        (name, _clean(description, 500), dir_id))
        return next(d for d in self.dirs(user) if d["id"] == dir_id)

    async def add_repo(self, user: dict, dir_id: int, url: str, branch: str, name: str = "") -> dict:
        await self._fresh(user)
        profile = self._profile_row(user)
        folder = self._dir(user, dir_id)
        url, branch = _clean(url, 500), _clean(branch, 120)
        name = _clean(name, 80) or repo_name_of(url)
        if not GIT_URL.match(url):
            raise HubError("仓库地址需以 http://、https:// 或 file:/// 开头")
        if not BRANCH.match(branch) or ".." in branch:
            raise HubError("分支名不合法")
        if not REPO_NAME.match(name):
            raise HubError("仓库名只能包含字母、数字、点、下划线和短横线")
        if self.db.one("SELECT 1 FROM work_repos WHERE dir_id = ? AND name = ?", (dir_id, name)):
            raise HubError(f"目录里已经有 {name}")
        await self.hub.ask_connector(profile["connector_id"], {
            "type": "work_clone",
            "work": {**self._identity(user, profile["git_name"], profile["git_email"]),
                     "folder": folder["folder"], "token": self.vault.open(profile["git_token"])},
            "repo": {"name": name, "url": url, "branch": branch},
        }, timeout=CLONE_TIMEOUT)
        self.db.execute("INSERT INTO work_repos (dir_id, name, url, branch, created_at) VALUES (?, ?, ?, ?, ?)",
                        (dir_id, name, url, branch, time.time()))
        self.db.audit(user["id"], "work.repo.add", dir_id=dir_id, repo=name)
        return next(d for d in self.dirs(user) if d["id"] == dir_id)

    async def set_repo_branch(self, user: dict, repo_id: int, branch: str) -> None:
        """The branch new sessions of this repository start from; sessions already running keep theirs."""
        row = self.db.one(
            "SELECT r.*, d.folder FROM work_repos r JOIN work_dirs d ON d.id = r.dir_id WHERE r.id = ? AND d.user_id = ?",
            (repo_id, user["id"]),
        )
        if not row:
            raise HubError("仓库不存在", 404)
        branch = _clean(branch, 120)
        if not BRANCH.match(branch) or ".." in branch:
            raise HubError("分支名不合法")
        await self._fresh(user)
        profile = self._profile_row(user)
        await self.hub.ask_connector(profile["connector_id"], {  # fetches, and checks the branch is on the remote
            "type": "work_clone",
            "work": {**self._identity(user, profile["git_name"], profile["git_email"]),
                     "folder": row["folder"], "token": self.vault.open(profile["git_token"])},
            "repo": {"name": row["name"], "url": row["url"], "branch": branch},
        }, timeout=CLONE_TIMEOUT)
        self.db.execute("UPDATE work_repos SET branch = ? WHERE id = ?", (branch, repo_id))
        self.db.audit(user["id"], "work.repo.branch", repo=row["name"], branch=branch)

    def remove_repo(self, user: dict, repo_id: int) -> None:
        row = self.db.one(
            "SELECT r.id, r.dir_id FROM work_repos r JOIN work_dirs d ON d.id = r.dir_id WHERE r.id = ? AND d.user_id = ?",
            (repo_id, user["id"]),
        )
        if not row:
            raise HubError("仓库不存在", 404)
        if self.db.one(
            "SELECT 1 FROM work_sessions s JOIN tasks t ON t.id = s.task_id WHERE s.dir_id = ? AND t.status IN "
            f"({','.join('?' * len(ACTIVE))})", (row["dir_id"], *ACTIVE),
        ):
            raise HubError("这个目录里还有会话在运行，请先结束再移除仓库", 409)
        self.db.execute("DELETE FROM work_repos WHERE id = ?", (repo_id,))

    # ---- sessions ---------------------------------------------------------

    def sessions(self, user: dict) -> list[dict]:
        rows = self.db.all(
            "SELECT s.id, s.dir_id, s.branch, s.push_mode, s.created_at, t.id AS task_id, t.title, t.status, t.turn, "
            "t.updated_at, a.id AS agent_id, a.name AS agent_name, a.kind AS agent_kind, d.name AS dir_name "
            "FROM work_sessions s JOIN tasks t ON t.id = s.task_id JOIN agents a ON a.id = t.agent_id "
            "JOIN work_dirs d ON d.id = s.dir_id WHERE s.user_id = ? AND s.deleted = 0 ORDER BY t.updated_at DESC",
            (user["id"],),
        )
        return rows

    def _session(self, user: dict, session_id: int) -> dict:
        row = next((s for s in self.sessions(user) if s["id"] == session_id), None)
        if not row:
            raise HubError("会话不存在", 404)
        return row

    def session(self, user: dict, session_id: int, after_id: int = 0) -> dict:
        row = self._session(user, session_id)
        return {**row, "events": self.hub.events(row["task_id"], after_id, viewer=user)}

    def _agent(self, profile: dict, agent_id: int) -> dict:
        user = self.hub.get_user(profile["user_id"])
        if not self.may_use(user, profile["connector_id"]):
            raise HubError("这台工作服务器的归属人已不再开放给你", 403)
        agent = self.hub.resolve_agent(agent_id)
        if not agent.get("workhost") or agent["connector_id"] != profile["connector_id"]:
            raise HubError("这个智能体不在你的工作服务器上", 403)
        if not agent["online"]:
            raise HubError(f"{agent['name']} 不在线", 409)
        return agent

    async def create_session(self, user: dict, agent_id: int, dir_id: int, prompt: str, push_mode: str = "",
                             images: list | None = None) -> dict:
        await self._fresh(user)  # the turn carries the token for fetching; an OAuth one must not expire mid-turn
        profile = self._profile_row(user)
        agent = self._agent(profile, agent_id)
        folder = self._dir(user, dir_id)
        if not self.db.one("SELECT 1 FROM work_repos WHERE dir_id = ?", (dir_id,)):
            raise HubError("这个目录里还没有仓库，请先添加仓库")
        pictures = self.hub.decode_images(images)
        prompt = (prompt or "").strip() or (IMAGE_EMPTY_PROMPT if pictures else "")
        if not prompt:
            raise HubError("请输入要智能体做的事")
        push_mode = push_mode or profile["push_mode"]
        if push_mode not in PUSH_MODES:
            raise HubError("推送方式只能是 confirm 或 direct")
        if not self.accounts(profile["connector_id"], agent["kind"]):
            try:  # learn the server's accounts once, so the first turn can already pick by quota
                await self.refresh_accounts(profile["connector_id"])
            except HubError as exc:
                LOGGER.warning("reading work accounts failed: %s", exc)
        now = time.time()
        with self.db.transaction():
            task_id = self.db.execute(
                "INSERT INTO tasks (from_user_id, agent_id, title, prompt, status, auto_approved, decided_by, created_at, "
                "updated_at, source) VALUES (?, ?, ?, ?, 'queued', 1, ?, ?, ?, 'work')",
                (user["id"], agent["id"], prompt.splitlines()[0][:120], prompt, user["id"], now, now),
            )
            session_id = self.db.execute(
                "INSERT INTO work_sessions (user_id, dir_id, task_id, branch, push_mode, created_at) VALUES (?, ?, ?, '', ?, ?)",
                (user["id"], dir_id, task_id, push_mode, now),
            )
            self.db.execute("UPDATE work_sessions SET branch = ? WHERE id = ?",
                            (f"agent/{_branch_user(user)}/s-{session_id}", session_id))
        self.hub._event(task_id, 1, "prompt", prompt, {"by": user["username"]})
        self.hub.save_images(task_id, 1, pictures)
        self.hub._event(task_id, 1, "status", "queued: 工作会话")
        self.db.audit(user["id"], "work.session.create", session_id=session_id, agent=agent_address(agent),
                      dir=folder["name"])
        await self.hub.dispatch(agent["connector_id"])
        return self.session(user, session_id)

    async def continue_session(self, user: dict, session_id: int, message: str, images: list | None = None) -> dict:
        row = self._session(user, session_id)
        pictures = self.hub.decode_images(images)
        message = (message or "").strip() or (IMAGE_EMPTY_PROMPT if pictures else "")
        if not message:
            raise HubError("消息不能为空")
        await self._fresh(user)
        task = self.hub.get_task(row["task_id"])
        if task["status"] not in FINISHED:
            raise HubError("智能体还在处理上一条，请等它结束或先取消")
        agent = self._agent(self._profile_row(user), task["agent_id"])
        turn = task["turn"] + 1
        self.db.execute(
            "UPDATE tasks SET turn = ?, pending_message = ?, error = '', finished_at = NULL, turn_user_id = ?, "
            "updated_at = ? WHERE id = ?",
            (turn, message, user["id"], time.time(), task["id"]),
        )
        self.hub._event(task["id"], turn, "prompt", message, {"by": user["username"]})
        self.hub.save_images(task["id"], turn, pictures)
        self.hub._set_status(task["id"], turn, "queued", "继续对话")
        await self.hub.dispatch(agent["connector_id"])
        return self.session(user, session_id)

    async def cancel_session(self, user: dict, session_id: int) -> dict:
        row = self._session(user, session_id)
        await self.hub.cancel(user, row["task_id"])
        return self.session(user, session_id)

    def set_push_mode(self, user: dict, session_id: int, push_mode: str) -> dict:
        self._session(user, session_id)
        if push_mode not in PUSH_MODES:
            raise HubError("推送方式只能是 confirm 或 direct")
        self.db.execute("UPDATE work_sessions SET push_mode = ? WHERE id = ?", (push_mode, session_id))
        return self._session(user, session_id)

    def _work_context(self, user: dict, profile: dict, session: dict) -> dict:
        folder = self._dir(user, session["dir_id"])
        repos = self.db.all("SELECT name, url, branch FROM work_repos WHERE dir_id = ? ORDER BY id", (folder["id"],))
        return {
            **self._identity(user, profile["git_name"], profile["git_email"]),
            "session": f"s-{session['id']}", "branch": session["branch"], "folder": folder["folder"],
            "dir_name": folder["name"], "dir_description": folder["description"], "repos": repos,
            "token": self.vault.open(profile["git_token"]),
        }

    async def changes(self, user: dict, session_id: int) -> dict:
        row = self._session(user, session_id)
        profile = self._profile_row(user)
        reply = await self.hub.ask_connector(profile["connector_id"], {
            "type": "work_status", "work": self._work_context(user, profile, row),
        }, timeout=GIT_TIMEOUT)
        repos = reply.get("repos") or []
        self._follow_branches(row["dir_id"], repos)
        return {"repos": repos, "branch": row["branch"], "push_mode": row["push_mode"]}

    def _follow_branches(self, dir_id: int, repos: list[dict]) -> list[str]:
        """The branch a repository is on follows the colleague: once a session works on another branch, the
        directory shows it and new sessions start from it."""
        moved = []
        for r in repos:
            base = r.get("base") or ""
            if not base or not BRANCH.match(base) or ".." in base:
                continue
            row = self.db.one("SELECT id, branch FROM work_repos WHERE dir_id = ? AND name = ?", (dir_id, r.get("repo")))
            if row and row["branch"] != base:
                self.db.execute("UPDATE work_repos SET branch = ? WHERE id = ?", (base, row["id"]))
                moved.append(f"{r['repo']} → {base}")
        return moved

    async def push(self, user: dict, session_id: int, repos: list[str] | None = None) -> dict:
        row = self._session(user, session_id)
        if row["status"] in ACTIVE:
            raise HubError("智能体还在运行，结束后再推送", 409)
        return await self._push(user, row, repos)

    async def _push(self, user: dict, row: dict, repos: list[str] | None) -> dict:
        await self._fresh(user)
        profile = self._profile_row(user)
        context = self._work_context(user, profile, row)
        if not context["token"] and any(r["url"].startswith("http") for r in context["repos"]):
            raise HubError("没有保存 GitLab 令牌，无法推送，请在「工作」里重新连接", 409)
        reply = await self.hub.ask_connector(profile["connector_id"], {
            "type": "work_push", "work": context, "only": [str(r) for r in repos or []],
        }, timeout=GIT_TIMEOUT)
        pushed = reply.get("pushed") or []
        self.db.audit(user["id"], "work.push", session_id=row["id"],
                      repos=[f"{p.get('repo')}→{p.get('branch')}" for p in pushed if isinstance(p, dict) and p.get("pushed")])
        return {"pushed": pushed}

    async def delete_session(self, user: dict, session_id: int, force: bool = False) -> None:
        """Gives a finished session's disk back; its branch stays in the repositories and on the remote."""
        row = self._session(user, session_id)
        if row["status"] in ACTIVE:
            raise HubError("会话还在运行，请先停止", 409)
        profile = self._profile_row(user)
        context = self._work_context(user, profile, row)
        if not force:
            status = await self.hub.ask_connector(profile["connector_id"], {"type": "work_status", "work": context},
                                                  timeout=GIT_TIMEOUT)
            waiting = [r for r in status.get("repos") or [] if r.get("unpushed") or r.get("files")]
            if waiting:
                names = "、".join(r["repo"] for r in waiting)
                raise HubError(f"{names} 还有未推送的提交或未提交的改动，确认不要了再删除", 409)
        await self.hub.ask_connector(profile["connector_id"], {"type": "work_remove_session", "work": context},
                                     timeout=GIT_TIMEOUT)
        self.db.execute("UPDATE work_sessions SET deleted = 1 WHERE id = ?", (session_id,))
        self.db.audit(user["id"], "work.session.delete", session_id=session_id, force=force)

    # ---- agent accounts: choosing, switching, quotas ---------------------------

    def settings(self, connector_id: int) -> dict:
        row = self.db.one("SELECT * FROM work_settings WHERE connector_id = ?", (connector_id,)) or \
            {"connector_id": connector_id, **DEFAULT_SETTINGS}
        return {**row, "current": json.loads(row["current"] or "{}"), "auto_switch": bool(row["auto_switch"])}

    def _save_settings(self, connector_id: int, **fields) -> None:
        current = self.settings(connector_id)
        merged = {**current, **fields}
        self.db.execute(
            "INSERT INTO work_settings (connector_id, auto_switch, threshold, on_limit, current) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(connector_id) DO UPDATE SET auto_switch = excluded.auto_switch, threshold = excluded.threshold, "
            "on_limit = excluded.on_limit, current = excluded.current",
            (connector_id, int(bool(merged["auto_switch"])), int(merged["threshold"]), merged["on_limit"],
             json.dumps(merged["current"])),
        )

    def accounts(self, connector_id: int, kind: str = "") -> list[dict]:
        rows = self.db.all(
            "SELECT * FROM work_accounts WHERE connector_id = ?" + (" AND kind = ?" if kind else "") + " ORDER BY account_id",
            (connector_id, kind) if kind else (connector_id,),
        )
        now = time.time()
        for row in rows:
            row["quota"] = json.loads(row["quota"] or "{}")
            row["left"] = _left_percent(row["quota"])
            row["exhausted"] = row["exhausted_until"] > now or row["quota"].get("usable") is False
        return rows

    def _switch(self, connector_id: int, kind: str, from_id: str, to_id: str, reason: str) -> None:
        current = self.settings(connector_id)["current"]
        current[kind] = to_id
        self._save_settings(connector_id, current=current)
        self.db.execute(
            "INSERT INTO work_switches (connector_id, kind, from_account, to_account, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (connector_id, kind, from_id, to_id, reason, time.time()),
        )
        LOGGER.info("work account switch on connector %s (%s): %s -> %s, %s", connector_id, kind, from_id, to_id, reason)

    def _best(self, accounts: list[dict], exclude: str = "") -> dict | None:
        usable = [a for a in accounts if not a["exhausted"] and a["account_id"] != exclude]
        return max(usable, key=lambda a: (a["left"] if a["left"] is not None else 50), default=None)

    def choose_account(self, connector_id: int, kind: str) -> str:
        """The account a new turn runs on. Switches the current one first when it is out or below the threshold."""
        accounts = self.accounts(connector_id, kind)
        if not accounts:
            return ""  # the connector falls back to its first account of that kind
        settings = self.settings(connector_id)
        current_id = settings["current"].get(kind)
        current = next((a for a in accounts if a["account_id"] == current_id), None)
        if current is None:
            best = self._best(accounts) or accounts[0]
            self._switch(connector_id, kind, "", best["account_id"], "首次选择额度最多的账号")
            return best["account_id"]
        low = current["exhausted"] or (current["left"] is not None and current["left"] < settings["threshold"])
        if low and settings["auto_switch"]:
            best = self._best(accounts, exclude=current["account_id"])
            if best and (current["exhausted"] or (best["left"] or 0) > (current["left"] or 0)):
                why = "额度已用完" if current["exhausted"] else f"剩余 {current['left']}%，低于阈值 {settings['threshold']}%"
                self._switch(connector_id, kind, current["account_id"], best["account_id"], why)
                return best["account_id"]
        return current["account_id"]

    async def refresh_accounts(self, connector_id: int) -> dict:
        """Reads every account's plan and quota on the work server (no model call) and the containers there."""
        reply = await self.hub.ask_connector(connector_id, {"type": "work_admin"}, timeout=90)
        now = time.time()
        seen = []
        for a in reply.get("accounts") or []:
            account_id = str(a.get("id") or "")[:80]
            if not account_id:
                continue
            seen.append(account_id)
            quota = {k: a.get(k) for k in ("primary", "secondary", "usable", "error")}
            resets = [w.get("resets_at") or 0 for w in (a.get("primary"), a.get("secondary"))
                      if isinstance(w, dict) and (w.get("used_percent") or 0) >= 100]
            exhausted_until = max(resets) if resets else 0
            self.db.execute(
                "INSERT INTO work_accounts (connector_id, account_id, kind, plan, email, quota, exhausted_until, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(connector_id, account_id) DO UPDATE SET kind = excluded.kind, "
                "plan = excluded.plan, email = excluded.email, quota = excluded.quota, "
                "exhausted_until = excluded.exhausted_until, updated_at = excluded.updated_at",
                (connector_id, account_id, str(a.get("kind") or "")[:20], str(a.get("plan") or "")[:40],
                 str(a.get("email") or "")[:120], json.dumps(quota), exhausted_until, now),
            )
        if seen:
            marks = ",".join("?" * len(seen))
            self.db.execute(f"DELETE FROM work_accounts WHERE connector_id = ? AND account_id NOT IN ({marks})",
                            (connector_id, *seen))
        return reply

    async def _out_of_quota(self, session: dict, task: dict) -> None:
        """A turn failed because its account ran out: mark it, switch, and continue or ask the colleague."""
        user = self.hub.get_user(session["user_id"])
        profile = self._profile_row(user)
        connector_id, kind, used = profile["connector_id"], task["agent_kind"], session["last_account"]
        try:
            await self.refresh_accounts(connector_id)  # learns when the account resets
        except HubError:
            pass
        if used:
            self.db.execute(
                "UPDATE work_accounts SET exhausted_until = MAX(exhausted_until, ?) WHERE connector_id = ? AND account_id = ?",
                (time.time() + 3600, connector_id, used),
            )
        settings = self.settings(connector_id)
        best = self._best(self.accounts(connector_id, kind), exclude=used) if settings["auto_switch"] else None
        turn = task["turn"]
        if not best:
            soonest = min((a["exhausted_until"] for a in self.accounts(connector_id, kind) if a["exhausted_until"]), default=0)
            when = datetime.fromtimestamp(soonest, LOCAL_TZ).strftime("%m-%d %H:%M") if soonest else "稍后"
            self.hub._event(task["id"], turn, "output", f"智能体额度已用完，暂时没有可切换的额度，预计 {when} 恢复，届时点「发送」继续。")
            self.hub._publish_task(task["id"])
            return
        self._switch(connector_id, kind, used, best["account_id"], f"{user['username']} 的会话执行中额度用完")
        if settings["on_limit"] == "auto":
            self.hub._event(task["id"], turn, "output", "上一轮执行中额度用完，已自动切换额度并继续，上下文和已改的文件都保留。")
            try:
                await self.continue_session(user, session["id"], CONTINUE_AFTER_SWITCH)
            except HubError as exc:
                self.hub._event(task["id"], turn, "error", f"切换额度后继续失败：{exc}")
        else:
            self.hub._event(task["id"], turn, "output", "上一轮执行中额度用完，已切换额度。上下文和已改的文件都保留，发送「继续」即可接着做。")
        self.hub._publish_task(task["id"])

    def usage(self, connector_id: int) -> list[dict]:
        """Tokens reported by work sessions per colleague: today, the last 7 days and in total."""
        today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        week = time.time() - 7 * 86400
        rows = self.db.all(
            "SELECT s.user_id, u.username, u.display_name, e.payload, e.created_at FROM task_events e "
            "JOIN work_sessions s ON s.task_id = e.task_id JOIN users u ON u.id = s.user_id "
            "JOIN work_profiles p ON p.user_id = s.user_id WHERE e.kind = 'usage' AND p.connector_id = ?",
            (connector_id,),
        )
        per: dict[int, dict] = {}
        for row in rows:
            usage = json.loads(row["payload"] or "{}")
            tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            item = per.setdefault(row["user_id"], {"user": row["display_name"] or row["username"], "today": 0, "week": 0, "total": 0})
            item["total"] += tokens
            if row["created_at"] >= week:
                item["week"] += tokens
            if row["created_at"] >= today:
                item["today"] += tokens
        return sorted(per.values(), key=lambda r: -r["total"])

    def _manage(self, user: dict, connector_id: int) -> dict:
        """Admins maintain every work server; anyone else the servers on their own machines."""
        row = self.db.one("SELECT * FROM connectors WHERE id = ?", (connector_id,))
        if not row:
            raise HubError("服务器不存在", 404)
        if not user.get("is_admin") and row["user_id"] != user["id"]:
            raise HubError("只有服务器的归属人或管理员可以维护它", 403)
        return row

    def hosts(self, user: dict) -> list[dict]:
        """Machines the user may manage, with whether each serves the 「工作」menu and every agent on it."""
        rows = self.db.all(
            "SELECT c.id, c.name, c.hostname, c.os, c.online, c.workhost, c.user_id, u.username, u.display_name "
            "FROM connectors c JOIN users u ON u.id = c.user_id" + ("" if user.get("is_admin") else " WHERE c.user_id = ?")
            + " ORDER BY c.online DESC, c.id",
            () if user.get("is_admin") else (user["id"],),
        )
        out = []
        for row in rows:
            info = json.loads(row["workhost"] or "{}")
            agents = self.db.all("SELECT id, key, name, kind, workhost, online FROM agents WHERE connector_id = ? ORDER BY key",
                                 (row["id"],))
            if not agents:
                continue
            users = self.db.all("SELECT u.id, u.username, u.display_name FROM work_server_users w JOIN users u ON u.id = w.user_id "
                                "WHERE w.connector_id = ? ORDER BY u.username", (row["id"],))
            out.append({"id": row["id"], "name": row["name"], "hostname": row["hostname"], "os": row["os"],
                        "online": bool(row["online"]), "owner": row["display_name"] or row["username"],
                        "mine": row["user_id"] == user["id"], "workhost": info.get("enabled") is True,
                        "docker": info.get("docker") is True, "image": info.get("image", ""),
                        "agents": [{**a, "workhost": bool(a["workhost"]), "online": bool(a["online"])} for a in agents],
                        "access": self._access(row["id"]), "allowed": users,
                        "colleagues": self.db.one("SELECT COUNT(*) AS n FROM work_profiles WHERE connector_id = ?", (row["id"],))["n"]})
        return out

    async def set_work_agents(self, user: dict, connector_id: int, keys: list[str]) -> None:
        self._manage(user, connector_id)
        conn = self.hub.connections.get(connector_id)
        if not conn:
            raise HubError("这台机器的连接器不在线", 409)
        known = {a["key"] for a in self.db.all("SELECT key FROM agents WHERE connector_id = ?", (connector_id,))}
        keys = [str(k) for k in keys if str(k) in known]
        if not keys:
            raise HubError("至少开放一个智能体")
        await conn.send({"type": "set_work_agents", "keys": keys})
        self.db.audit(user["id"], "work.server.agents", connector_id=connector_id, agents=keys)

    def set_access(self, user: dict, connector_id: int, access: str, usernames: list[str]) -> dict:
        self._manage(user, connector_id)
        if access not in ("all", "list"):
            raise HubError("access 只能是 all 或 list")
        ids = []
        for name in usernames:
            row = self.db.one("SELECT id FROM users WHERE username = ?", (str(name).strip(),))
            if not row:
                raise HubError(f"用户 {name} 不存在", 404)
            ids.append(row["id"])
        with self.db.transaction():
            self.db.execute("INSERT INTO work_servers (connector_id, access, updated_at) VALUES (?, ?, ?) "
                            "ON CONFLICT(connector_id) DO UPDATE SET access = excluded.access, updated_at = excluded.updated_at",
                            (connector_id, access, time.time()))
            self.db.execute("DELETE FROM work_server_users WHERE connector_id = ?", (connector_id,))
            for uid in ids:
                self.db.execute("INSERT INTO work_server_users (connector_id, user_id) VALUES (?, ?)", (connector_id, uid))
        self.db.audit(user["id"], "work.server.access", connector_id=connector_id, access=access, users=usernames)
        return next(h for h in self.hosts(user) if h["id"] == connector_id)

    async def admin_status(self, user: dict, connector_id: int) -> dict:
        self._manage(user, connector_id)
        if not any(s["id"] == connector_id for s in self.servers()):
            raise HubError("这台机器还没有开放任何智能体给「工作」使用", 404)
        reply = await self.refresh_accounts(connector_id)
        users = {container_name(r["user_id"]): r for r in self.db.all(
            "SELECT p.user_id, u.username, u.display_name FROM work_profiles p JOIN users u ON u.id = p.user_id")}
        containers = []
        for c in reply.get("containers") or []:
            who = users.get(c.get("user"))
            containers.append({**c, "username": (who["display_name"] or who["username"]) if who else c.get("user")})
        settings = self.settings(connector_id)
        accounts = self.accounts(connector_id)
        for a in accounts:
            a["current"] = settings["current"].get(a["kind"]) == a["account_id"]
        switches = self.db.all("SELECT * FROM work_switches WHERE connector_id = ? ORDER BY id DESC LIMIT 20", (connector_id,))
        sessions = self.db.one(
            "SELECT COUNT(*) AS n FROM work_sessions s JOIN tasks t ON t.id = s.task_id JOIN work_profiles p ON p.user_id = s.user_id "
            f"WHERE p.connector_id = ? AND t.status IN ({','.join('?' * len(ACTIVE))})", (connector_id, *ACTIVE),
        )["n"]
        return {"host": {**(reply.get("host") or {}), "running_sessions": sessions}, "containers": containers,
                "accounts": accounts, "settings": settings, "switches": switches, "usage": self.usage(connector_id),
                "agents": next(s for s in self.servers() if s["id"] == connector_id)["agents"]}

    def update_settings(self, user: dict, connector_id: int, auto_switch: bool, threshold: int, on_limit: str) -> dict:
        self._manage(user, connector_id)
        if on_limit not in ON_LIMIT:
            raise HubError("额度用完时的处理只能是 auto 或 confirm")
        if not 0 <= threshold <= 90:
            raise HubError("阈值需在 0～90 之间")
        self._save_settings(connector_id, auto_switch=auto_switch, threshold=threshold, on_limit=on_limit)
        self.db.audit(user["id"], "work.settings", connector_id=connector_id, auto_switch=auto_switch,
                      threshold=threshold, on_limit=on_limit)
        return self.settings(connector_id)

    def set_current(self, user: dict, connector_id: int, account_id: str) -> dict:
        self._manage(user, connector_id)
        account = next((a for a in self.accounts(connector_id) if a["account_id"] == account_id), None)
        if not account:
            raise HubError("账号不存在", 404)
        before = self.settings(connector_id)["current"].get(account["kind"], "")
        if before != account_id:
            self._switch(connector_id, account["kind"], before, account_id, f"{user['username']} 设为当前")
            self.db.audit(user["id"], "work.account.current", connector_id=connector_id, account=account_id)
        return self.settings(connector_id)

    async def container_action(self, user: dict, connector_id: int, target: str, action: str) -> None:
        self._manage(user, connector_id)
        if action not in ("stop", "start", "restart"):
            raise HubError("只能休眠、启动或重启", 400)
        await self.hub.ask_connector(connector_id, {"type": "work_admin_action", "action": action, "user": target},
                                     timeout=90)
        self.db.audit(user["id"], "work.container", connector_id=connector_id, container=target, op=action)

    # ---- hub hooks ----------------------------------------------------------

    def _payload(self, task: dict, payload: dict) -> None:
        session = self.db.one("SELECT * FROM work_sessions WHERE task_id = ?", (task["id"],))
        if not session:
            return
        # sessions run inside the colleague's container: no hub MCP token there
        if payload.pop("mcp_token", None):
            self.hub.revoke_task_tokens(task["id"])
        user = self.hub.get_user(session["user_id"])
        profile = self._profile_row(user)
        payload["work"] = self._work_context(user, profile, session)
        account = self.choose_account(profile["connector_id"], task["agent_kind"])
        payload["work"]["account"] = account
        self.db.execute("UPDATE work_sessions SET last_account = ? WHERE id = ?", (account, session["id"]))

    async def _on_finished(self, task_id: int) -> None:
        session = self.db.one("SELECT * FROM work_sessions WHERE task_id = ?", (task_id,))
        if not session:
            return
        task = self.hub.get_task(task_id)
        if task["status"] == "failed" and LIMIT_ERROR.search(task["error"] or ""):
            await self._out_of_quota(session, task)
            return
        user = self.hub.get_user(session["user_id"])
        if task["status"] in ("completed", "failed"):
            try:
                profile = self._profile_row(user)
                reply = await self.hub.ask_connector(profile["connector_id"], {
                    "type": "work_status", "work": self._work_context(user, profile, self._session(user, session["id"])),
                }, timeout=GIT_TIMEOUT)
                moved = self._follow_branches(session["dir_id"], reply.get("repos") or [])
                if moved:
                    self.hub._event(task_id, task["turn"], "output", f"已切换分支：{'、'.join(moved)}，之后新会话也从这里开始")
                    self.hub._publish_task(task_id)
            except HubError:
                pass
        if session["push_mode"] != "direct" or task["status"] != "completed":
            return
        try:
            result = await self._push(user, self._session(user, session["id"]), None)
            names = "、".join(f"{p['repo']} → {p.get('branch')}" for p in result["pushed"] if p.get("pushed")) or "没有新的提交"
            failed = [f"{p['repo']}：{p['error']}" for p in result["pushed"] if p.get("error")]
            self.hub._event(task_id, task["turn"], "output", f"已自动推送：{names}")
            if failed:
                self.hub._event(task_id, task["turn"], "error", "自动推送失败：" + "；".join(failed))
        except HubError as exc:
            self.hub._event(task_id, task["turn"], "error", f"自动推送失败：{exc}")
        self.hub._publish_task(task_id)


def _left_percent(quota: dict) -> int | None:
    """What is left of an account's tightest window, in percent; None when unknown."""
    used = [w.get("used_percent") for w in (quota.get("primary"), quota.get("secondary"))
            if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float))]
    return max(0, round(100 - max(used))) if used else None


def container_name(user_id: int) -> str:
    return f"u{user_id}"


def _branch_user(user: dict) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "", user["username"])
    return name or f"u{user['id']}"
