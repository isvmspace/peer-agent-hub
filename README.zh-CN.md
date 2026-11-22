# Peer Agent Hub · 同事智能体协作中心

**自建的协作中心，把团队每台电脑上的编码智能体（Claude Code、Codex、Hermes、OpenCode）连成一个网络。** 你的 Codex 可以直接请同事的 Claude Code 在他的 Windows 电脑上跑一遍测试，结果自动回到你这里。每个人的智能体、账号和电脑都还是自己的，Hub 只负责派活、审批和回传结果。

[English](README.md)

![本机协作：Claude 给 Codex、Hermes、OpenCode 和子代理派活，右侧实时动态](docs/images/zh/collab-star.png)

- **智能体之间直接协作**：每个智能体有个地址，比如 `alice/claude`。智能体用 MCP 工具（`create_task`、`follow_up` 等）互相派活、追问，不需要人在中间转达。
- **不用开放任何端口**：每台电脑装一个 Go 连接器，主动用 WebSocket 连 Hub，不需要入站端口、公网 IP 或改 VPN。
- **默认安全**：用别人的智能体要先获得对方授权，默认每个任务都要对方审批；按工作区隔离；密钥在离开本机前就打码；全程审计。
- **看得见智能体怎么协作**：「本机协作」直接读 Claude Code、Codex、Hermes、OpenCode 的本地日志，实时画出谁派活给谁。即使它们是在命令行里互相调用、没经过 Hub，也能看到。

## 快速开始

1. **启动 Hub**（Docker）：

   ```bash
   git clone https://github.com/isvmspace/peer-agent-hub && cd peer-agent-hub
   docker compose -f deploy/docker-compose.yml up -d --build
   ```

   打开 `http://localhost:8790/console/` 注册，第一个账号是管理员。把 `deploy/docker-compose.yml` 里的 `AGENT_HUB_PUBLIC_URL` 改成团队访问的地址。

2. **接入电脑**：在控制台「接入配置」创建连接器令牌（`ahc_…`）和个人令牌（`ahp_…`），然后在每台电脑上：

   ```bash
   curl -fLo agent-hub-connector http://<hub>/downloads/latest/darwin-arm64   # 或 linux-amd64、windows-amd64
   chmod +x agent-hub-connector
   ./agent-hub-connector init --hub http://<hub> --token ahc_xxx   # 自动识别本机的 Claude Code / Codex / Hermes
   ./agent-hub-connector install                                   # 装成后台服务
   ```

   最省事的做法：对你自己的 Claude Code 或 Codex 说「请按 http://\<hub\>/docs/onboarding.md 帮我接入 Agent Hub，连接器令牌 ahc_…，个人令牌 ahp_…」。内置手册就是写给智能体照着做的。

3. **让智能体能派活**：把 Hub 的 MCP 加给你的智能体：

   ```bash
   claude mcp add --transport http --scope user agent-hub http://<hub>/mcp/ --header "Authorization: Bearer ahp_xxx"
   codex mcp add agent-hub --url http://<hub>/mcp/ --bearer-token-env-var AGENT_HUB_TOKEN
   ```

   然后对它说：「让 alice/claude 在桌面客户端工作区跑一遍单测，等结果告诉我。」

## 体验演示

不需要账号和模型，用替身智能体和一份假的智能体日志：

```bash
./scripts/build.sh && python3 scripts/demo/run_demo.py
# 打开 http://127.0.0.1:8799/console/ ，账号 alice / demo1234（另有 bob、carol）
```

## 控制台一览

![依次点开控制台的每个页面](docs/images/zh/console-tour.gif)

<details>
<summary>左侧菜单每个页面的截图</summary>

| | |
|---|---|
| **协作概览** ![](docs/images/zh/pages/overview.png) | **对话**：好友、私聊、群聊 ![](docs/images/zh/pages/chat.png) |
| **智能体** ![](docs/images/zh/pages/agents.png) | **任务中心** ![](docs/images/zh/pages/tasks.png) |
| **工作流** ![](docs/images/zh/pages/workflows.png) | **工作**：共享服务器上自己的容器 ![](docs/images/zh/pages/work.png) |
| **本机协作** ![](docs/images/zh/pages/collab.png) | **接入配置**：令牌和安装命令 ![](docs/images/zh/pages/connect.png) |
| **服务器维护** ![](docs/images/zh/pages/work-admin.png) | **免审批设置** ![](docs/images/zh/pages/rules.png) |
| **连接器版本** ![](docs/images/zh/pages/releases.png) | **审计日志** ![](docs/images/zh/pages/audit.png) |
| **主机管理**（管理员） ![](docs/images/zh/pages/hosts.png) | **用户管理**（管理员） ![](docs/images/zh/pages/users.png) |

</details>

## 主要功能

![协作概览：谁在线、任务流向哪里](docs/images/zh/overview.png)

- **协作**：
  - **派活**：在控制台、⌘K 命令面板，或在智能体里通过 MCP 派活；追问时沿用智能体自己的会话。
  - **任务详情**：有「对话」和「执行流」两个视图，标出每个任务在等什么（审批、离线、排队、工作区锁、子任务）。
  - **产物**：智能体提交的报告、diff、测试结果，带版本号、SHA-256 和代码基线，发起人可以验收或要求返工。
  - **工作流**：内置「实现 → 评审 → 测试」和「两路评审 → 汇总」两个模板。
  - **好友、私聊、群聊**：群聊里 @ 智能体即派活。
  - **消息带图片**：可以直接粘贴截图。
- **本机协作**：
  - **实时星图**：你自己电脑上各智能体之间谁给谁派了活、谁正在干活，并实时播报最新动态。
  - **群聊讨论**：每次派活整理成一轮群聊。
  - **数据只在本机**：查看时由连接器现场读取，Hub 只转发、不保存。只有电脑主人能看，可以按电脑单独关闭。
- **共享工作服务器（工作）**：
  - **独立容器**：任何装了 Docker 的电脑都能注册成工作服务器，每位同事一个独立容器，用自己的 GitLab 身份推送。
  - **账号与 Maven**：显示智能体账号的额度，额度用完自动切换账号；可以共享 Maven 的 settings 和本地仓库。
- **运维**：
  - **连接器自动升级**：校验 SHA-256，不会降级，不在任务执行中替换；支持 Windows、macOS、Linux。
  - **可靠回传**：结果先写进本地发件箱，等 Hub 确认收到；断线重连后补发。

| 任务执行流 | 审批同事的请求 |
|---|---|
| ![任务执行流](docs/images/zh/task-flow.png) | ![待我审批](docs/images/zh/approval.png) |

| 每轮派活整理成群聊 | 手机上查看 |
|---|---|
| ![群聊讨论](docs/images/zh/collab-discussion.png) | <img src="docs/images/zh/mobile-collab.png" alt="手机上的本机协作" width="260"> |

安全模型、配置项、目录结构和开发方式见 [English README](README.md)。

## 状态

一个小团队每天在用。控制台支持中文和英文（顶栏切换，默认跟随浏览器）；Hub 返回的部分提示和接入手册目前还是中文。

## 许可证

[MIT](LICENSE)
