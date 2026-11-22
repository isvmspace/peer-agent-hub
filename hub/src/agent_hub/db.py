"""SQLite storage. The hub is a single process, so one shared connection guarded by a lock is enough."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,              -- personal | connector
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS connectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_id INTEGER NOT NULL UNIQUE REFERENCES api_tokens(id),
    name TEXT NOT NULL,
    hostname TEXT NOT NULL DEFAULT '',
    os TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    online INTEGER NOT NULL DEFAULT 0,
    last_seen_at REAL
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    connector_id INTEGER NOT NULL REFERENCES connectors(id),
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    workdir TEXT NOT NULL DEFAULT '',
    workspaces TEXT NOT NULL DEFAULT '[]',
    online INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE (user_id, key)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id),
    from_agent TEXT NOT NULL DEFAULT '',
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_task_id INTEGER REFERENCES tasks(id),
    depth INTEGER NOT NULL DEFAULT 0,
    turn INTEGER NOT NULL DEFAULT 1,
    pending_message TEXT,
    session_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    decided_by INTEGER REFERENCES users(id),
    auto_approved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    connector_id INTEGER,
    workspace TEXT NOT NULL DEFAULT '',
    group_id INTEGER,
    group_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_status ON tasks(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_from ON tasks(from_user_id);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    turn INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- prompt | status | output | result | error
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);
CREATE TABLE IF NOT EXISTS approval_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    from_user_id INTEGER REFERENCES users(id),   -- NULL means anyone
    agent_id INTEGER REFERENCES agents(id),      -- NULL means all my agents
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    platform TEXT NOT NULL,          -- windows/amd64 | darwin/arm64 | darwin/amd64 | linux/amd64
    object_key TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    policy TEXT NOT NULL DEFAULT 'OPTIONAL',
    min_supported TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,            -- published | withdrawn
    created_by INTEGER REFERENCES users(id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    status TEXT NOT NULL,            -- pending | accepted
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (user_id, agent_id)
);
CREATE TABLE IF NOT EXISTS chat_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS group_agents (
    group_id INTEGER NOT NULL REFERENCES chat_groups(id),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    workspace TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, agent_id)
);
CREATE TABLE IF NOT EXISTS group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES chat_groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_group_messages ON group_messages(group_id, id);
CREATE TABLE IF NOT EXISTS friendships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),     -- requester
    friend_id INTEGER NOT NULL REFERENCES users(id),   -- must accept
    status TEXT NOT NULL,            -- pending | accepted
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (user_id, friend_id)
);
CREATE TABLE IF NOT EXISTS direct_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id),
    to_user_id INTEGER NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    read_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dm_pair ON direct_messages(from_user_id, to_user_id, id);
CREATE TABLE IF NOT EXISTS auto_replies (
    user_id INTEGER NOT NULL REFERENCES users(id),
    friend_id INTEGER NOT NULL DEFAULT 0,   -- 0: the default for every friend
    agent_id INTEGER,                       -- NULL: no auto-reply
    workspace TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, friend_id)
);
CREATE TABLE IF NOT EXISTS auto_reply_threads (
    user_id INTEGER NOT NULL REFERENCES users(id),
    friend_id INTEGER NOT NULL REFERENCES users(id),
    task_id INTEGER,                        -- the conversation later messages continue
    last_message_id INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,     -- messages arrived while it was answering
    notified_offline INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, friend_id)
);
CREATE TABLE IF NOT EXISTS auto_reply_turns (  -- turns started by a friend's message; only these are posted back
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    turn INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    friend_id INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, turn)
);
CREATE TABLE IF NOT EXISTS group_users (
    group_id INTEGER NOT NULL REFERENCES chat_groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS task_shares (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    can_continue INTEGER NOT NULL DEFAULT 0,
    auto_approve INTEGER NOT NULL DEFAULT 0,
    shared_by INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, user_id)
);
CREATE TABLE IF NOT EXISTS group_reads (
    group_id INTEGER NOT NULL REFERENCES chat_groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    last_message_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS connector_messages (
    msg_id TEXT PRIMARY KEY,         -- delivered-once id of a persisted connector report
    connector_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    turn INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- report | diff | test | image | file
    version INTEGER NOT NULL DEFAULT 1,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    stored TEXT NOT NULL,            -- file name under attachments/<task_id>/
    mime TEXT NOT NULL DEFAULT '',
    code_ref TEXT NOT NULL DEFAULT '{}',
    review TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted | rework
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, id);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template TEXT NOT NULL,
    title TEXT NOT NULL,
    from_user_id INTEGER NOT NULL REFERENCES users(id),
    input TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | completed | failed | canceled
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
    idx INTEGER NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    workspace TEXT NOT NULL DEFAULT '',
    depends_on TEXT NOT NULL DEFAULT '[]',
    gate INTEGER NOT NULL DEFAULT 0,   -- 1: wait for the requester before starting
    status TEXT NOT NULL,              -- waiting | gated | running | completed | failed | skipped | canceled
    task_id INTEGER REFERENCES tasks(id),
    UNIQUE (run_id, idx)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
-- 「工作」菜单：每个同事在工作服务器上的独立容器、目录、仓库和会话
CREATE TABLE IF NOT EXISTS work_profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    connector_id INTEGER NOT NULL REFERENCES connectors(id),
    git_name TEXT NOT NULL,
    git_email TEXT NOT NULL,
    git_token TEXT NOT NULL DEFAULT '',   -- encrypted with the hub secret
    push_mode TEXT NOT NULL DEFAULT 'confirm',  -- confirm | direct
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_dirs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL,                 -- real folder name in the container, never shown
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir_id INTEGER NOT NULL REFERENCES work_dirs(id),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    branch TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (dir_id, name)
);
CREATE TABLE IF NOT EXISTS work_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    dir_id INTEGER NOT NULL REFERENCES work_dirs(id),
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
    branch TEXT NOT NULL,
    push_mode TEXT NOT NULL DEFAULT 'confirm',
    created_at REAL NOT NULL
);
-- agent accounts on a work server and how turns pick among them
CREATE TABLE IF NOT EXISTS work_accounts (
    connector_id INTEGER NOT NULL REFERENCES connectors(id),
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',        -- masked by the connector
    quota TEXT NOT NULL DEFAULT '{}',      -- last read: primary / secondary windows, usable
    exhausted_until REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (connector_id, account_id)
);
CREATE TABLE IF NOT EXISTS work_settings (
    connector_id INTEGER PRIMARY KEY REFERENCES connectors(id),
    auto_switch INTEGER NOT NULL DEFAULT 1,
    threshold INTEGER NOT NULL DEFAULT 20,       -- switch when the current account has less than this % left
    on_limit TEXT NOT NULL DEFAULT 'auto',       -- auto: continue on the new account | confirm: wait for the colleague
    current TEXT NOT NULL DEFAULT '{}'           -- kind -> account_id
);
-- GitLab OAuth result kept until the colleague finishes 「建立连接」
CREATE TABLE IF NOT EXISTS work_gitlab_pending (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    access TEXT NOT NULL,                 -- encrypted
    refresh TEXT NOT NULL DEFAULT '',     -- encrypted
    expires_at REAL NOT NULL DEFAULT 0,
    username TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_mrs (
    session_id INTEGER NOT NULL REFERENCES work_sessions(id),
    repo TEXT NOT NULL,
    project TEXT NOT NULL,
    iid INTEGER NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    merge_status TEXT NOT NULL DEFAULT '',
    pipeline TEXT NOT NULL DEFAULT '',
    pipeline_url TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, repo)
);
-- who may use a work server: everyone, or only the people its owner lists
CREATE TABLE IF NOT EXISTS work_servers (
    connector_id INTEGER PRIMARY KEY REFERENCES connectors(id),
    access TEXT NOT NULL DEFAULT 'all',   -- all | list
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_server_users (
    connector_id INTEGER NOT NULL REFERENCES connectors(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (connector_id, user_id)
);
CREATE TABLE IF NOT EXISTS work_switches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connector_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    from_account TEXT NOT NULL DEFAULT '',
    to_account TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# columns added after the first release; applied to existing databases on start
MIGRATIONS = (
    ("agents", "workspaces TEXT NOT NULL DEFAULT '[]'"),
    ("tasks", "workspace TEXT NOT NULL DEFAULT ''"),
    ("tasks", "group_id INTEGER"),
    ("tasks", "group_message_id INTEGER"),
    ("agents", "isolation TEXT NOT NULL DEFAULT 'none'"),
    ("agent_contacts", "workspaces TEXT NOT NULL DEFAULT '[]'"),
    ("users", "disabled INTEGER NOT NULL DEFAULT 0"),
    ("group_messages", "mentions TEXT NOT NULL DEFAULT '[]'"),
    ("group_reads", "read_at REAL NOT NULL DEFAULT 0"),
    ("tasks", "source TEXT NOT NULL DEFAULT ''"),
    ("tasks", "turn_user_id INTEGER"),
    ("agents", "hidden INTEGER NOT NULL DEFAULT 0"),
    ("connectors", "remote_ip TEXT NOT NULL DEFAULT ''"),
    ("connectors", "local_ips TEXT NOT NULL DEFAULT '[]'"),
    ("connectors", "connected_at REAL"),
    ("task_events", "payload TEXT"),
    ("agents", "capabilities TEXT NOT NULL DEFAULT '{}'"),
    ("tasks", "execution_id TEXT NOT NULL DEFAULT ''"),
    ("tasks", "attempts INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "stop_pending INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "code_ref TEXT NOT NULL DEFAULT '{}'"),
    ("tasks", "workflow_step_id INTEGER"),
    ("artifacts", "rework_pending INTEGER NOT NULL DEFAULT 0"),
    ("artifacts", "rework_turn INTEGER"),
    ("direct_messages", "auto_task_id INTEGER"),
    ("agents", "workhost INTEGER NOT NULL DEFAULT 0"),
    ("work_sessions", "last_account TEXT NOT NULL DEFAULT ''"),
    ("work_sessions", "deleted INTEGER NOT NULL DEFAULT 0"),
    ("work_profiles", "token_kind TEXT NOT NULL DEFAULT 'pat'"),
    ("work_profiles", "refresh_token TEXT NOT NULL DEFAULT ''"),
    ("work_profiles", "token_expires REAL NOT NULL DEFAULT 0"),
    ("connectors", "workhost TEXT NOT NULL DEFAULT '{}'"),
    ("connectors", "collab INTEGER NOT NULL DEFAULT 1"),  # 本机协作 on/off
    ("agent_contacts", "allow_full INTEGER NOT NULL DEFAULT 0"),
    ("direct_messages", "auto_agent TEXT NOT NULL DEFAULT ''"),
)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            for table, column in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column.split()[0] not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_group ON tasks(group_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)")

    @contextmanager
    def transaction(self):
        """Run several statements atomically; other threads wait on the lock meanwhile."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """Run a write statement and return lastrowid."""
        with self._lock:
            return self._conn.execute(sql, params).lastrowid

    def update(self, sql: str, params: tuple | list = ()) -> int:
        """Run a write statement and return the number of affected rows."""
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def audit(self, user_id: int | None, action: str, **detail: Any) -> None:
        self.execute(
            "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (user_id, action, json.dumps(detail, ensure_ascii=False), time.time()),
        )
