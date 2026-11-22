"""Fixed multi-agent workflow templates: the hub starts each step when the steps it depends on completed.

Transitions are guarded `UPDATE … WHERE status = …` statements, so a duplicate finish notification, a retry
or a restart never starts a downstream step twice.
"""

from __future__ import annotations

import json
import logging
import time

from .db import Database
from .hub import FINISHED, Hub, HubError, workspace_id

LOGGER = logging.getLogger(__name__)

MAX_UPSTREAM_CHARS = 6000

TEMPLATES = {
    "build_review_test": {
        "name": "实现 → 评审 → 测试",
        "description": "一个智能体实现，另一个评审改动，第三个验证测试；每步都能拿到上一步的结果和产物编号。",
        "steps": [
            {"name": "实现", "role": "implement", "depends_on": [],
             "brief": "按需求完成实现。完成后用 agent-hub 的 submit_artifact 提交改动说明（kind=report）和 diff（kind=diff）。"},
            {"name": "评审", "role": "review", "depends_on": [0],
             "brief": "评审上一步的改动：正确性、风险、遗漏。只读审查，不要修改文件。用 submit_artifact 提交评审意见（kind=report），结论写明“通过”或“需修改”及理由。"},
            {"name": "测试", "role": "test", "depends_on": [1],
             "brief": "验证改动：运行相关测试或构建，记录命令与结果。用 submit_artifact 提交测试报告（kind=test）。"},
        ],
    },
    "dual_review": {
        "name": "两路评审 → 汇总",
        "description": "两个智能体独立评审同一内容，第三个汇总分歧并给出结论。",
        "steps": [
            {"name": "评审 A", "role": "review_a", "depends_on": [],
             "brief": "独立评审下述内容，只读，不修改文件。用 submit_artifact 提交评审意见（kind=report）。"},
            {"name": "评审 B", "role": "review_b", "depends_on": [],
             "brief": "独立评审下述内容，只读，不修改文件。用 submit_artifact 提交评审意见（kind=report）。"},
            {"name": "汇总", "role": "summary", "depends_on": [0, 1],
             "brief": "汇总两份评审：列出一致结论、分歧及你的裁决。用 submit_artifact 提交汇总报告（kind=report）。"},
        ],
    },
}


def templates() -> list[dict]:
    return [{"id": key, **value} for key, value in TEMPLATES.items()]


class Workflows:
    def __init__(self, db: Database, hub: Hub):
        self.db = db
        self.hub = hub
        hub.on_finished.append(self.on_task_finished)

    # ---- reading ---------------------------------------------------------

    def run(self, user: dict, run_id: int) -> dict:
        run = self.db.one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if not run:
            raise HubError(f"工作流不存在: {run_id}", 404)
        if run["from_user_id"] != user["id"] and not user.get("is_admin"):
            raise HubError("只能查看自己发起的工作流", 403)
        run["template_name"] = TEMPLATES.get(run["template"], {}).get("name", run["template"])
        run["steps"] = []
        for step in self.db.all("SELECT * FROM workflow_steps WHERE run_id = ? ORDER BY idx", (run_id,)):
            step["depends_on"] = json.loads(step["depends_on"])
            step["gate"] = bool(step["gate"])
            agent = self.db.one("SELECT a.key, a.name, u.username FROM agents a JOIN users u ON u.id = a.user_id WHERE a.id = ?", (step["agent_id"],))
            step["agent_address"] = f"{agent['username']}/{agent['key']}" if agent else "已移除"
            owner = self.db.one("SELECT user_id FROM agents WHERE id = ?", (step["agent_id"],))
            if step["workspace"] and not (owner and owner["user_id"] == user["id"]) and not user.get("is_admin"):
                # a colleague's folder layout stays private: show the workspace name only
                ws = self.hub.workspaces_of(self.hub.resolve_agent(step["agent_id"])) if agent else []
                step["workspace"] = next((w["name"] for w in ws if w["path"] == step["workspace"]), "已移除的工作区")
            if step["task_id"]:
                task = self.hub.get_task(step["task_id"])
                step["task_status"] = task["status"]
                step["wait"] = self.hub.wait_reason(task)
                step["artifacts"] = [
                    {"id": a["id"], "ref": f"A{a['id']}", "name": a["name"], "version": a["version"], "review": a["review"]}
                    for a in self.db.all("SELECT id, name, version, review FROM artifacts WHERE task_id = ? ORDER BY id", (task["id"],))
                ]
            run["steps"].append(step)
        return run

    def runs(self, user: dict) -> list[dict]:
        rows = self.db.all(
            "SELECT * FROM workflow_runs WHERE from_user_id = ? ORDER BY id DESC LIMIT 100", (user["id"],)
        )
        for row in rows:
            row["template_name"] = TEMPLATES.get(row["template"], {}).get("name", row["template"])
            row["steps"] = self.db.all(
                "SELECT idx, name, status, task_id FROM workflow_steps WHERE run_id = ? ORDER BY idx", (row["id"],)
            )
        return rows

    # ---- starting --------------------------------------------------------

    async def start(self, user: dict, template: str, title: str, text: str, assignments: list) -> dict:
        spec = TEMPLATES.get(template)
        if not spec:
            raise HubError("未知的工作流模板")
        text = (text or "").strip()
        if not text:
            raise HubError("请填写工作流的需求内容")
        if not isinstance(assignments, list) or len(assignments) != len(spec["steps"]):
            raise HubError("每个步骤都需要指定智能体")
        resolved = []
        for step, item in zip(spec["steps"], assignments):
            item = item if isinstance(item, dict) else {}
            agent = self.hub.resolve_agent(item.get("agent") or "")
            # every step is checked now and again when it starts: grants may change in between
            workspace = self.hub.check_workspace(user, agent, str(item.get("workspace") or ""))
            resolved.append((step, agent, workspace, bool(item.get("gate"))))
        now = time.time()
        run_id = self.db.execute(
            "INSERT INTO workflow_runs (template, title, from_user_id, input, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (template, (title.strip() or text.splitlines()[0])[:120], user["id"], text, now, now),
        )
        for idx, (step, agent, workspace, gate) in enumerate(resolved):
            self.db.execute(
                "INSERT INTO workflow_steps (run_id, idx, name, role, agent_id, workspace, depends_on, gate, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting')",
                (run_id, idx, step["name"], step["role"], agent["id"], workspace, json.dumps(step["depends_on"]), int(gate)),
            )
        self.db.audit(user["id"], "workflow.start", run_id=run_id, template=template)
        await self.advance(run_id)
        return self.run(user, run_id)

    async def continue_step(self, user: dict, run_id: int, idx: int) -> dict:
        run = self.run(user, run_id)
        if run["from_user_id"] != user["id"]:
            raise HubError("只有发起人可以放行步骤", 403)
        if not self.db.update(
            "UPDATE workflow_steps SET status = 'ready' WHERE run_id = ? AND idx = ? AND status = 'gated'", (run_id, idx)
        ):
            raise HubError("该步骤不在等待放行状态")
        self.db.audit(user["id"], "workflow.continue", run_id=run_id, step=idx)
        await self.advance(run_id)
        return self.run(user, run_id)

    async def cancel(self, user: dict, run_id: int) -> dict:
        run = self.run(user, run_id)
        if run["from_user_id"] != user["id"]:
            raise HubError("只有发起人可以取消工作流", 403)
        if run["status"] != "running":
            raise HubError("工作流已结束")
        self._finish_run(run_id, "canceled")
        self.db.execute(
            "UPDATE workflow_steps SET status = 'canceled' WHERE run_id = ? AND status IN ('waiting', 'gated', 'ready', 'starting')",
            (run_id,),
        )
        # running steps, and any task a step was creating while we canceled (found by its step id)
        tasks = {s["task_id"] for s in run["steps"] if s["task_id"]}
        tasks |= {r["id"] for r in self.db.all(
            "SELECT t.id FROM tasks t JOIN workflow_steps s ON s.id = t.workflow_step_id WHERE s.run_id = ?", (run_id,)
        )}
        for task_id in sorted(tasks):
            await self._stop_task(user, task_id)
        self.db.audit(user["id"], "workflow.cancel", run_id=run_id)
        return self.run(user, run_id)

    async def _stop_task(self, user: dict, task_id: int) -> None:
        try:
            await self.hub.cancel(user, task_id, tree=True)
        except HubError:
            pass  # already finished

    # ---- advancing -------------------------------------------------------

    async def on_task_finished(self, task_id: int) -> None:
        step = self.db.one("SELECT run_id FROM workflow_steps WHERE task_id = ?", (task_id,))
        if step:
            await self.advance(step["run_id"])

    async def recover(self) -> None:
        """After a hub restart: apply finishes that happened while down and start whatever became ready."""
        for run in self.db.all("SELECT id FROM workflow_runs WHERE status = 'running'"):
            try:
                await self.advance(run["id"], recovering=True)
            except Exception:  # noqa: BLE001 - one broken run must not block the others
                LOGGER.exception("workflow recover failed run=%s", run["id"])

    async def advance(self, run_id: int, recovering: bool = False) -> None:
        run = self.db.one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if not run or run["status"] != "running":
            return
        user = self.hub.get_user(run["from_user_id"])
        # after a crash, adopt a task a step created just before it, so a step is never started twice
        # (only then: in normal operation `starting` means a create is in progress right now)
        for step in [] if not recovering else self.db.all("SELECT * FROM workflow_steps WHERE run_id = ? AND status = 'starting'", (run_id,)):
            orphan = self.db.one("SELECT id FROM tasks WHERE workflow_step_id = ? ORDER BY id DESC LIMIT 1", (step["id"],))
            if orphan:
                self.db.update("UPDATE workflow_steps SET status = 'running', task_id = ? WHERE id = ? AND status = 'starting'",
                               (orphan["id"], step["id"]))
            else:
                self.db.update("UPDATE workflow_steps SET status = 'ready' WHERE id = ? AND status = 'starting'", (step["id"],))
        # 1. settle running steps whose task finished
        for step in self.db.all("SELECT * FROM workflow_steps WHERE run_id = ? AND status = 'running'", (run_id,)):
            task = self.hub.get_task(step["task_id"])
            if task["status"] in FINISHED:
                outcome = {"completed": "completed", "canceled": "canceled"}.get(task["status"], "failed")
                self.db.update("UPDATE workflow_steps SET status = ? WHERE id = ? AND status = 'running'", (outcome, step["id"]))
        steps = self.db.all("SELECT * FROM workflow_steps WHERE run_id = ? ORDER BY idx", (run_id,))
        by_idx = {s["idx"]: s for s in steps}
        broken = any(s["status"] in ("failed", "canceled") for s in steps)
        if broken:
            self.db.execute(
                "UPDATE workflow_steps SET status = 'skipped' WHERE run_id = ? AND status IN ('waiting', 'gated', 'ready')", (run_id,)
            )
            if not any(s["status"] in ("running", "starting") for s in steps):
                failed = any(s["status"] == "failed" for s in steps)
                self._finish_run(run_id, "failed" if failed else "canceled")
            return
        # 2. release steps whose dependencies all completed
        for step in steps:
            deps = json.loads(step["depends_on"])
            if step["status"] == "waiting" and all(by_idx[d]["status"] == "completed" for d in deps):
                target = "gated" if step["gate"] else "ready"
                self.db.update("UPDATE workflow_steps SET status = ? WHERE id = ? AND status = 'waiting'", (target, step["id"]))
        # 3. start ready steps
        for step in self.db.all("SELECT * FROM workflow_steps WHERE run_id = ? AND status = 'ready' ORDER BY idx", (run_id,)):
            if not self.db.update("UPDATE workflow_steps SET status = 'starting' WHERE id = ? AND status = 'ready'", (step["id"],)):
                continue  # another advance took it
            try:
                task = await self.hub.create_task(
                    user, step["agent_id"], self._prompt(run, step, by_idx), title=f"[工作流 #{run_id}·{step['name']}] {run['title']}"[:120],
                    workspace=workspace_id(step["workspace"]) if step["workspace"] else "", workflow_step_id=step["id"],
                )
            except HubError as exc:
                self.db.update("UPDATE workflow_steps SET status = 'failed' WHERE id = ?", (step["id"],))
                LOGGER.warning("workflow step start failed run=%s step=%s: %s", run_id, step["idx"], exc)
                await self.advance(run_id)
                return
            if not self.db.update("UPDATE workflow_steps SET status = 'running', task_id = ? WHERE id = ? AND status = 'starting'",
                                  (task["id"], step["id"])):
                self.db.execute("UPDATE workflow_steps SET task_id = ? WHERE id = ?", (task["id"], step["id"]))
                if self.db.one("SELECT status FROM workflow_steps WHERE id = ?", (step["id"],))["status"] == "canceled":
                    await self._stop_task(user, task["id"])  # the run was canceled while the task was being created
                return
        steps = self.db.all("SELECT status FROM workflow_steps WHERE run_id = ?", (run_id,))
        if steps and all(s["status"] == "completed" for s in steps):
            self._finish_run(run_id, "completed")
        else:
            self.db.execute("UPDATE workflow_runs SET updated_at = ? WHERE id = ?", (time.time(), run_id))
            self.hub.publish(f"user:{run['from_user_id']}")

    def _finish_run(self, run_id: int, status: str) -> None:
        now = time.time()
        if self.db.update(
            "UPDATE workflow_runs SET status = ?, updated_at = ?, finished_at = ? WHERE id = ? AND status = 'running'",
            (status, now, now, run_id),
        ):
            run = self.db.one("SELECT from_user_id FROM workflow_runs WHERE id = ?", (run_id,))
            self.hub.publish(f"user:{run['from_user_id']}")

    def _prompt(self, run: dict, step: dict, by_idx: dict) -> str:
        spec = TEMPLATES[run["template"]]["steps"][step["idx"]]
        parts = [f"这是工作流 #{run['id']}「{run['title']}」的「{step['name']}」步骤。{spec['brief']}", "", "需求：", run["input"]]
        for dep in json.loads(step["depends_on"]):
            upstream = by_idx[dep]
            task = self.hub.get_task(upstream["task_id"])
            result = task["result"] or ""
            if len(result) > MAX_UPSTREAM_CHARS:
                result = result[:MAX_UPSTREAM_CHARS] + "\n…（已截断，完整内容用 get_task 查看）"
            artifacts = self.db.all("SELECT id, name, kind, version, sha256 FROM artifacts WHERE task_id = ? ORDER BY id", (task["id"],))
            parts += ["", f"--- 上游步骤「{upstream['name']}」（任务 #{task['id']}，{task['agent_address']}）结果 ---", result or "（无文字结果）"]
            if artifacts:
                parts.append("上游产物（用 agent-hub 的 get_artifact 按编号读取，内容带哈希校验）：")
                parts += [f"- A{a['id']} {a['name']} v{a['version']}（{a['kind']}，sha256 {a['sha256'][:12]}…）" for a in artifacts]
        return "\n".join(parts)
