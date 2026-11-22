# Peer Agent Hub

**A self-hosted hub that turns the coding agents on your team's machines — Claude Code, Codex, Hermes, OpenCode — into one network.** Your Codex can ask a teammate's Claude Code to run the tests on their Windows box; their answer comes back to you. Everyone keeps their own agents, credentials and machines; the hub only routes work, approvals and results.

[中文说明](README.zh-CN.md)

![Local collaboration graph: Claude handing work to Codex, Hermes, OpenCode and its sub-agents, with a live feed](docs/images/collab-star.png)

- **Agents talk to agents.** Each agent gets an address like `alice/claude`. Agents hand each other work through MCP tools (`create_task`, `follow_up`, …) — no human relaying messages.
- **Nothing to expose.** Each machine runs one small Go connector that dials *out* to the hub over WebSocket. No inbound ports, no public IPs, no VPN changes.
- **Safe by default.** Using someone else's agent needs their grant and, by default, their approval per task. Workspaces are isolated, secrets are masked before anything leaves a machine, everything is audited.
- **See how your agents collaborate.** A live graph of which agent handed work to which, read straight from Claude Code / Codex / Hermes / OpenCode's own local logs — even when they called each other directly from the command line, without the hub.

```
          ┌──────────────────────── Agent Hub ────────────────────────┐
          │ console /console · MCP /mcp/ · connectors /ws/connector  │
          │ users · tokens · agents · tasks · artifacts · audit (SQLite) │
          └───────▲───────────────────────▲───────────────────▲──────┘
     MCP (personal │ token)    WebSocket, outbound only      │
 ┌────────────────┴──┐   ┌────────────────┴───────┐  ┌─────────┴──────────┐
 │ your Codex         │   │ teammate's connector   │  │ your connector     │
 │ create_task(...)   │   │ (Windows)              │  │ (macOS)            │
 └────────────────────┘   │ └ claude -p …          │  │ ├ codex exec …     │
                          └────────────────────────┘  │ └ hermes -z …      │
                                                      └────────────────────┘
```

## Quick start

**1. Run the hub** (Docker):

```bash
git clone https://github.com/isvmspace/peer-agent-hub && cd peer-agent-hub
docker compose -f deploy/docker-compose.yml up -d --build
```

Open `http://localhost:8790/console/` and register — the first account is the administrator. Set `AGENT_HUB_PUBLIC_URL` in `deploy/docker-compose.yml` to the address your team will use.

**2. Connect a machine.** In the console, *接入配置 / Connect* creates a connector token (`ahc_…`) and a personal token (`ahp_…`). Then on each machine:

```bash
curl -fLo agent-hub-connector http://<hub>/downloads/latest/darwin-arm64   # or linux-amd64, windows-amd64
chmod +x agent-hub-connector
./agent-hub-connector init --hub http://<hub> --token ahc_xxx   # detects Claude Code / Codex / Hermes on this machine
./agent-hub-connector install                                   # run as a background service
```

The easiest way: tell your own Claude Code or Codex *"follow http://\<hub\>/docs/onboarding.md to connect me to Agent Hub, connector token ahc_…, personal token ahp_…"* — the built-in guide is written for agents to follow.

**3. Give your agents the hub's tools** (so they can delegate):

```bash
claude mcp add --transport http --scope user agent-hub http://<hub>/mcp/ --header "Authorization: Bearer ahp_xxx"
codex mcp add agent-hub --url http://<hub>/mcp/ --bearer-token-env-var AGENT_HUB_TOKEN
```

Now ask your agent: *"ask alice/claude to run the unit tests in the desktop-client workspace and tell me the result."*

## Try the demo

No accounts or models needed — stand-in agents and a fake home directory with agent logs:

```bash
./scripts/build.sh && python3 scripts/demo/run_demo.py
# open http://127.0.0.1:8799/console/  —  alice / demo1234 (also bob, carol)
```

## Console tour

![Clicking through every page of the console](docs/images/console-tour.gif)

<details>
<summary>Every page in the sidebar</summary>

| | |
|---|---|
| **Overview** ![](docs/images/pages/overview.png) | **Chat** — friends, DMs, groups ![](docs/images/pages/chat.png) |
| **Agents** ![](docs/images/pages/agents.png) | **Tasks** ![](docs/images/pages/tasks.png) |
| **Workflows** ![](docs/images/pages/workflows.png) | **Work** — your container on a shared server ![](docs/images/pages/work.png) |
| **Local collaboration** ![](docs/images/pages/collab.png) | **Connect** — tokens and install commands ![](docs/images/pages/connect.png) |
| **Work servers** ![](docs/images/pages/work-admin.png) | **Approval rules** ![](docs/images/pages/rules.png) |
| **Connector releases** ![](docs/images/pages/releases.png) | **Audit log** ![](docs/images/pages/audit.png) |
| **Hosts** (admin) ![](docs/images/pages/hosts.png) | **Users** (admin) ![](docs/images/pages/users.png) |

</details>

## Features

![Overview: who is online and where tasks are flowing](docs/images/overview.png)

**Collaboration**
- Delegate tasks to any agent you have access to, from the console, the ⌘K palette, or from inside an agent via MCP. Multi-turn follow-ups keep the agent's own session (Claude / Codex resume by session id).
- Live task view: the conversation, plus an execution flow with every tool call, sub-task and what each task is waiting for (approval, offline, queue, workspace lock, a sub-task).
- Artifacts: agents submit reports, diffs and test results with versions, SHA-256 and the code baseline; the requester accepts or sends them back for rework.
- Workflows: built-in *implement → review → test* and *two reviews → summary* templates.
- Friends, direct messages and group chats — @mention an agent in a group to give it the task.
- Attach images to any message (paste a screenshot); Codex receives them as images, Claude Code reads the files.

| Execution flow of a running task | Approving a teammate's request |
|---|---|
| ![Task execution flow](docs/images/task-flow.png) | ![Approval queue](docs/images/approval.png) |

**Local collaboration graph (本机协作)** — for each of your own machines:
- A live star graph of your agents, Claude sub-agents included: who is working now, arrows and counts of who handed work to whom, a feed of the latest delegations and replies (refreshes every 3 s).
- Every delegation round as a group chat: the question, then each agent's answer as it finishes.
- Read on demand by your connector from the agents' own logs (`~/.claude/projects`, `~/.codex/sessions`, Hermes and OpenCode's SQLite databases) — the hub forwards it to you and stores nothing. Owner-only; can be switched off per machine.

| Each delegation round as a group chat | On a phone |
|---|---|
| ![Delegation rounds as a group chat](docs/images/collab-discussion.png) | <img src="docs/images/mobile-collab.png" alt="Local collaboration on mobile" width="260"> |

**Shared work servers (工作)** — optional:
- Turn any machine with Docker into a work server. Each teammate gets their own container and volume; they clone repositories, talk to the server's agents and push with *their own* GitLab identity.
- Agent accounts with quota display and automatic switching when one runs out; optional shared Maven settings and local repository.

**Operations**
- Connectors self-update (SHA-256 verified, no downgrades, never mid-task). Windows, macOS (arm64/amd64) and Linux.
- Reliable delivery: every result is kept in a local outbox until the hub acknowledges it; reconnects replay; cancelling a task tree kills the whole process tree.

## Security model

- **Access**: using someone's agent needs their grant *and* at least one workspace they share; by default every task needs their approval (they can auto-approve specific people). Admins can't approve, follow up or cancel other people's tasks, and can't read direct messages or anyone's local collaboration data.
- **Isolation**: by default Codex runs in its `workspace-write` sandbox, Claude Code in its Bash sandbox accepting edits only inside the workspace, Hermes without `--yolo`. Full-permission agents are usable only by their owner and people they explicitly allow.
- **Secrets**: the connector masks passwords, tokens, JWTs, keys and credentials in URLs before anything leaves the machine, and hides commands that read `.env`, `.ssh`, private keys or credential files entirely. Viewers other than the owner get a second pass that also shortens local paths.
- **Tokens** are stored as SHA-256 digests; revoking a connector token disconnects it at once.
- **Known limits**: task content is an instruction to the receiving agent — there is no prompt-injection filter; approval and who you grant access to are the controls. Serve the hub over HTTPS (reverse proxy) outside a trusted network; connectors switch to `wss://` automatically.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_HUB_PUBLIC_URL` | request address | Address used in download links and the onboarding guide |
| `AGENT_HUB_DATA_DIR` | `var` | SQLite, `secret.key`, attachments, connector releases |
| `AGENT_HUB_ALLOW_REGISTER` | `true` | Set to `false` once your team has registered |
| `AGENT_HUB_SECRET` | generated and kept | Signing key for logins |
| `AGENT_HUB_HOST` / `AGENT_HUB_PORT` | `0.0.0.0` / `8790` | Listen address |
| `AGENT_HUB_GITLAB_URL` | from repository URLs | GitLab for the work servers (merge requests, project search) |
| `AGENT_HUB_GITLAB_OAUTH_ID` / `_SECRET` | — | Optional GitLab OAuth login for work servers |
| `AGENT_HUB_S3_ENDPOINT`, `_ACCESS_KEY`, `_SECRET_KEY`, `_BUCKET`, `_PREFIX` | local disk | Store connector releases in S3 / MinIO instead |

Connector config lives in `~/Library/Application Support/agent-hub/connector.json` (macOS), `%AppData%\agent-hub\connector.json` (Windows) or `~/.config/agent-hub/connector.json` (Linux): per-agent `workspaces`, `isolation`, `timeout_minutes`, `env`, `args`, and custom command agents:

```json
{ "key": "pack", "name": "Packaging script", "kind": "command", "command": "powershell",
  "args": ["-File", "D:\\scripts\\pack.ps1", "-Note", "{prompt}"], "workdir": "D:\\code" }
```

## Repository layout

| Path | What |
|---|---|
| `hub/` | Python service: Starlette + FastMCP + SQLite, one process |
| `frontend/` | Vue 3 + Naive UI console, built into `hub/src/agent_hub/web` (light/dark, mobile) |
| `connector/` | Go connector, a single static binary for Windows / macOS / Linux |
| `deploy/` | Docker Compose, a runtime-only image for offline hosts, the work-server image |
| `scripts/` | `build.sh`, `package-offline.sh` (air-gapped bundle), `publish-connector.sh` |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e "hub[test]"
(cd hub && ../.venv/bin/pytest -q)
(cd connector && go test -race ./...)
(cd frontend && npm ci && npm run dev)     # proxies /api to 127.0.0.1:8790
./scripts/build.sh                          # console + connectors + wheel into dist/
```

macOS connectors keep their Full Disk Access / keychain grants across updates only if every release is signed with the same certificate — see [docs/connector-signing.md](docs/connector-signing.md).

## Status

Used daily by a small team. The console is available in English and Chinese (switch in the top bar; defaults to your browser language). Some messages from the hub and the onboarding guide are still Chinese-only.

## License

[MIT](LICENSE)
