#!/usr/bin/env python3
"""Make a fake home directory whose agent logs show a believable collaboration, for screenshots and trying the
「本机协作」page without real data.

    python3 scripts/demo/demo_home.py /tmp/agent-hub-demo/home
    HOME=/tmp/agent-hub-demo/home agent-hub-connector run -config ...

Writes Claude Code logs (~/.claude/projects), Codex rollouts (~/.codex/sessions), a Hermes database
(~/.hermes/state.db) and an OpenCode database (~/.local/share/opencode/opencode.db), in the formats those agents use.
Timestamps are relative to now, so a few sessions show as working right now.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

NOW = time.time()
CWD = "/home/dev/payments"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ago(minutes: float) -> float:
    return NOW - minutes * 60


# ---- Claude Code ---------------------------------------------------------------------------

class ClaudeLog:
    def __init__(self, home: Path, sid: str, cwd: str = CWD, sub: tuple[str, str, str] | None = None):
        folder = home / ".claude" / "projects" / cwd.replace("/", "-")
        self.sid, self.cwd = sid, cwd
        if sub:  # (agent id, description, Task call id)
            agent, description, call = sub
            path = folder / sid / "subagents" / f"agent-{agent}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.with_suffix(".meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": description, "toolUseId": call}))
        else:
            path = folder / f"{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.lines = path, []

    def _line(self, kind: str, t: float, content) -> None:
        self.lines.append({"type": kind, "sessionId": self.sid, "cwd": self.cwd, "timestamp": iso(t), "entrypoint": "cli",
                           "message": {"role": kind, "content": content}})

    def title(self, text: str) -> None:
        self.lines.append({"type": "ai-title", "aiTitle": text, "sessionId": self.sid})

    def ask(self, t: float, text: str) -> None:
        self._line("user", t, text)

    def bash(self, t: float, description: str, command: str, result: str = "done", took: float = 0.4) -> str:
        call = "toolu_" + uuid.uuid4().hex[:20]
        self._line("assistant", t, [{"type": "tool_use", "id": call, "name": "Bash", "input": {"command": command, "description": description}}])
        self._line("user", t + took, [{"type": "tool_result", "tool_use_id": call, "content": result}])
        return call

    def task(self, t: float, description: str, prompt: str, took: float) -> str:
        call = "toolu_" + uuid.uuid4().hex[:20]
        self._line("assistant", t, [{"type": "tool_use", "id": call, "name": "Task", "input": {"description": description, "prompt": prompt}}])
        self._line("user", t + took, [{"type": "tool_result", "tool_use_id": call, "content": "finished"}])
        return call

    def read(self, t: float, path: str) -> None:
        call = "toolu_" + uuid.uuid4().hex[:20]
        self._line("assistant", t, [{"type": "tool_use", "id": call, "name": "Read", "input": {"file_path": path}}])
        self._line("user", t + 0.2, [{"type": "tool_result", "tool_use_id": call, "content": "(file contents)"}])

    def reply(self, t: float, text: str) -> None:
        self._line("assistant", t, [{"type": "text", "text": text}])

    def save(self) -> None:
        self.path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in self.lines))


# ---- Codex -----------------------------------------------------------------------------------

def codex(home: Path, started: float, prompt: str, reply: str | None, ended: float, interactive: bool = False) -> None:
    sid = str(uuid.uuid4())
    day = datetime.fromtimestamp(started)
    folder = home / ".codex" / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": iso(started), "type": "session_meta", "payload": {"id": sid, "timestamp": iso(started), "cwd": CWD,
                                                                       "originator": "codex-tui" if interactive else "codex_exec", "source": "exec"}},
        {"timestamp": iso(started + 0.2), "type": "response_item", "payload": {"type": "message", "role": "user",
                                                                              "content": [{"type": "input_text", "text": "# AGENTS.md instructions\n..."}]}},
        {"timestamp": iso(started + 0.3), "type": "response_item", "payload": {"type": "message", "role": "user",
                                                                              "content": [{"type": "input_text", "text": prompt}]}},
        {"timestamp": iso(started + 8), "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                                                                            "input": 'const r = await tools.exec_command({cmd:"rg -n retry src/"})'}},
        {"timestamp": iso(started + 9), "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": "src/retry.ts:12"}},
    ]
    if reply:
        rows.append({"timestamp": iso(ended), "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": reply}})
    else:  # still working: its log keeps growing
        rows.append({"timestamp": iso(ended), "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c2",
                                                                                  "input": 'const r = await tools.exec_command({cmd:"npm test -- retry"})'}})
    (folder / f"rollout-{day:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


# ---- Hermes ----------------------------------------------------------------------------------

def hermes_db(home: Path) -> sqlite3.Connection:
    path = home / ".hermes" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
            cwd TEXT, display_name TEXT, tool_call_count INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            tool_calls TEXT, tool_call_id TEXT, timestamp REAL, active INTEGER DEFAULT 1);""")
    return db


def hermes(db, sid: str, source: str, started: float, prompt: str, reply: str | None, ended: float, parent: str | None = None,
           delegate: list[str] | None = None, title: str | None = None) -> None:
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", (sid, source, parent, started, ended if reply else None, CWD, title, 3))
    db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (sid, "user", prompt, started))
    if delegate:
        calls = [{"id": "d1", "type": "function", "function": {"name": "delegate_task",
                                                              "arguments": json.dumps({"tasks": [{"goal": g} for g in delegate]})}}]
        db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                   (sid, "assistant", "", json.dumps(calls), started + 5))
    db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
               (sid, "assistant", reply or "Reading the retry handler and the test plan…", ended))


# ---- OpenCode --------------------------------------------------------------------------------

def opencode(home: Path, sid: str, started: float, prompt: str, reply: str, ended: float, title: str) -> None:
    path = home / ".local" / "share" / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE IF NOT EXISTS session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT);""")
    db.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?)", (sid, None, CWD, title, int(started * 1000), int(ended * 1000)))
    db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?)", (sid + "-1", sid, "user", 1, int(started * 1000), json.dumps({"text": prompt})))
    db.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?)", (sid + "-2", sid, "assistant", 2, int(ended * 1000),
               json.dumps({"content": [{"type": "text", "text": reply}], "time": {"completed": int(ended * 1000)}})))
    db.commit()


# ---- the story ---------------------------------------------------------------------------------

REVIEW = ("You are a reviewer. Read docs/retry-design.md and src/payments/retry.ts. "
          "Find what breaks when a payment provider times out after charging, and say what to change.")
FIX_CHECK = "Round 2: check that PR #42 fixes the double-charge on retry. Read the diff and run the retry tests."


def build(home: Path) -> None:
    hdb = hermes_db(home)

    # 1. Claude asks Codex, Hermes and OpenCode for a review in parallel, adds a sub-agent, then a second round now
    main = ClaudeLog(home, str(uuid.uuid4()))
    main.title("Payment retry redesign")
    t0 = ago(52)
    main.ask(t0, "Review the payment retry design with Codex, Hermes and OpenCode, then merge what they find.")
    main.read(t0 + 20, f"{CWD}/docs/retry-design.md")
    t1 = t0 + 60
    main.bash(t1, "Run Codex, Hermes and OpenCode reviews in parallel",
              f'cd {CWD} && (codex exec "$(cat review.md)" > codex.md) & (hermes chat -q "$(cat review.md)" > hermes.md) & '
              f'(opencode run "$(cat review.md)" > opencode.md) & wait')
    codex(home, t1 + 3, REVIEW, "**Blocking:** the retry reuses no idempotency key, so a timeout after the provider charged leads to a "
          "second charge.\n\n- Generate the key once per payment intent and send it on every attempt.\n- Treat `409 duplicate` as success.",
          t1 + 190)
    hermes(hdb, "20261118_0901_a1", "oneshot", t1 + 2, REVIEW,
           "Agree with the idempotency gap. Also: the backoff has no jitter, so 500 clients retry in lock-step after an outage. "
           "Add full jitter and cap retries at 5.", t1 + 240)
    opencode(home, "ses_demo_review_1", t1 + 4, REVIEW,
             "Two issues: (1) retries are not idempotent; (2) the refund path retries on 4xx errors, which never succeed. "
             "Only retry on timeouts and 5xx.", t1 + 150, "Payment retry review")
    sub_call = main.task(t1 + 260, "Check the refund path for the same bug", "Check whether refunds have the same double-charge risk.", 120)
    sub = ClaudeLog(home, main.sid, sub=("a7f3", "Check the refund path for the same bug", sub_call))
    sub.ask(t1 + 262, "Check whether refunds have the same double-charge risk.")
    sub.read(t1 + 280, f"{CWD}/src/payments/refund.ts")
    sub.reply(t1 + 375, "Refunds are safe: the provider rejects a second refund for the same charge. No change needed.")
    sub.save()
    main.reply(t1 + 400, "All three agree on the missing idempotency key. Plan: one key per payment intent, retry only timeouts and 5xx, "
                         "full jitter, max 5 attempts. Refunds are fine.")
    # round 2, happening now
    t2 = ago(4)
    main.ask(t2, "PR #42 is up. Have Codex and Hermes verify the fix.")
    main.bash(t2 + 30, "Ask Codex and Hermes to verify PR #42",
              f'cd {CWD} && (codex exec "{FIX_CHECK}" > v2-codex.md) & (hermes chat -q "{FIX_CHECK}" > v2-hermes.md) & wait')
    codex(home, t2 + 33, FIX_CHECK, None, NOW - 12)
    hermes(hdb, "20261118_0955_b2", "oneshot", t2 + 32, FIX_CHECK, None, NOW - 6)
    main.reply(NOW - 20, "Waiting for Codex and Hermes to finish verifying PR #42…")
    main.save()

    # 2. an earlier session: Claude splits an accessibility pass across OpenCode and two sub-agents
    a11y = ClaudeLog(home, str(uuid.uuid4()), cwd=CWD)
    a11y.title("Checkout page accessibility pass")
    t = ago(170)
    a11y.ask(t, "Make the checkout page pass an accessibility audit.")
    a11y.bash(t + 30, "Ask OpenCode to audit the checkout form", f'cd {CWD} && opencode run "Audit web/checkout for WCAG 2.2 AA issues"')
    opencode(home, "ses_demo_a11y", t + 33, "Audit web/checkout for WCAG 2.2 AA issues",
             "Found 6 issues: 3 inputs without labels, low contrast on the coupon hint (2.9:1), focus lost after the payment modal closes, "
             "and the card error is not announced to screen readers.", t + 300, "Checkout accessibility audit")
    for i, (desc, answer) in enumerate([("Fix form labels and contrast", "Labels added, coupon hint now 5.1:1."),
                                        ("Fix focus and live error messages", "Focus returns to the pay button; errors use aria-live.")]):
        call = a11y.task(t + 320 + i * 5, desc, desc, 200)
        s = ClaudeLog(home, a11y.sid, sub=(f"b{i}", desc, call))
        s.ask(t + 322 + i * 5, desc)
        s.reply(t + 500 + i * 30, answer)
        s.save()
    a11y.reply(t + 600, "All 6 issues fixed and verified with axe.")
    a11y.save()

    # 3. Hermes hands two investigations to its own sub-agents
    hermes(hdb, "20261118_0700_h0", "cli", ago(300), "Why did yesterday's settlement report miss 14 transactions?",
           "Two causes: a timezone cut-off at UTC midnight and a filter that drops partial captures. Patch drafted.", ago(260),
           delegate=["Compare settlement rows with provider export", "Check the report's date filter"], title="Settlement report gap")
    hermes(hdb, "20261118_0702_h1", "subagent", ago(298), "Compare settlement rows with provider export",
           "14 missing rows are all partial captures.", ago(285), parent="20261118_0700_h0")
    hermes(hdb, "20261118_0702_h2", "subagent", ago(298), "Check the report's date filter",
           "The filter uses local midnight; the export uses UTC.", ago(280), parent="20261118_0700_h0")

    # 4. someone using Codex by hand (not part of any collaboration)
    codex(home, ago(90), "Add a unit test for currency rounding", "Added tests/rounding.test.ts (8 cases).", ago(82), interactive=True)

    hdb.commit()


def keep_alive(home: Path) -> None:
    """The sessions that are working right now keep writing, as real ones do, so they stay 'in progress'."""
    now = time.time()
    for rollout in (home / ".codex" / "sessions").rglob("*.jsonl"):
        lines = rollout.read_text().splitlines()
        last = json.loads(lines[-1])
        if last.get("payload", {}).get("type") == "custom_tool_call":  # not finished
            with rollout.open("a") as fh:
                fh.write(json.dumps({"timestamp": iso(now), "type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "exec", "call_id": f"k{int(now)}",
                    "input": 'const r = await tools.exec_command({cmd:"npm test -- retry"})'}}) + "\n")
    db = sqlite3.connect(home / ".hermes" / "state.db")
    db.execute("UPDATE messages SET timestamp = ? WHERE session_id IN (SELECT id FROM sessions WHERE ended_at IS NULL) AND role = 'assistant'", (now,))
    db.commit()
    for log in (home / ".claude" / "projects").glob("*/*.jsonl"):
        if "Payment retry redesign" in log.read_text():
            sid = json.loads(log.read_text().splitlines()[1])["sessionId"]
            with log.open("a") as fh:
                fh.write(json.dumps({"type": "assistant", "sessionId": sid, "cwd": CWD, "timestamp": iso(now),
                                     "message": {"role": "assistant", "content": [{"type": "text", "text": "Waiting for Codex and Hermes to finish verifying PR #42…"}]}}) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: demo_home.py <empty directory to use as HOME>")
    target = Path(sys.argv[1])
    if target.exists() and any(target.iterdir()):
        sys.exit(f"{target} is not empty")
    build(target)
    print(f"demo home ready: {target}")
