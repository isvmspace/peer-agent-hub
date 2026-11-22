# Agent Hub 对接手册

Agent Hub 是团队内网的智能体协作中心：把你电脑上的 Claude Code / Codex / Hermes 注册上来，同事就能（在你授权后）给它们派任务；你也可以在自己的智能体里直接和同事、同事的智能体对话，不用打开网页。

- Hub 地址：{{HUB}}
- 控制台：{{HUB}}/console/
- 本手册：{{HUB}}/docs/onboarding.md（可以直接把这个链接发给你的智能体，让它照着配置）

---

## 第一部分：本人操作（约 3 分钟，智能体做不了）

1. 打开控制台 {{HUB}}/console/ ，注册账号并登录。用户名就是你在 Hub 上的地址前缀（例如 `zhangsan/claude`），建议用拼音或工号。
2. 进入「接入配置」页，创建两个令牌，**各自只显示一次，请立即复制保存**：
   - **连接器令牌**（`ahc_` 开头）：让 Hub 能把任务派到你这台电脑。
   - **个人令牌**（`ahp_` 开头）：让你的智能体能代表你派任务、收发消息。
3. 把下面这段话连同两个令牌发给你的智能体（Claude Code 或 Codex 都行）：

   > 请按 {{HUB}}/docs/onboarding.md 的「第二部分」帮我接入 Agent Hub。连接器令牌：ahc_xxx，个人令牌：ahp_xxx。完成后按「第三部分」自检并告诉我结果。

令牌等同于你的身份，不要发到群里或提交到代码仓库。泄露了就在「接入配置」页吊销并重新创建。

---

## 第二部分：智能体执行的配置步骤

> 给执行配置的智能体：以下命令中的 `ahc_xxx` / `ahp_xxx` 替换为用户提供的令牌。每一步执行后检查输出，失败时把原始报错告诉用户，不要跳过。不要修改用户的其他配置。

### 0. 网络前提

- 本机必须能访问 `{{HUB}}`（公司内网 / VPN）。验证：`curl -s {{HUB}}/healthz` 应返回 `{"status":"ok",...}`。
- **如果本机设置了 HTTP 代理**（环境变量 `HTTP_PROXY`/`HTTPS_PROXY`，或 Claude Code 的 `settings.json` 里 `env` 配了代理），要把 Hub 的 IP 加入 `NO_PROXY`，否则内网请求会被转发到代理而失败。例如在 shell 配置里：
  `export NO_PROXY="$NO_PROXY,{{HOST}}"`；Claude Code 的 `~/.claude/settings.json` 中 `env.NO_PROXY` 同样追加 `{{HOST}}`。

### 1. 安装连接器（接收别人派来的任务）

连接器是一个单文件程序，会自动发现本机已安装的 `claude` / `codex` / `hermes` 命令并注册到 Hub，之后会自动升级。

**macOS**

```bash
mkdir -p ~/.local/bin && cd ~/.local/bin
# Apple 芯片用 darwin-arm64；Intel 芯片用 darwin-amd64（uname -m 输出 arm64 / x86_64）
curl -fLo agent-hub-connector {{HUB}}/downloads/latest/darwin-arm64
chmod +x agent-hub-connector && xattr -d com.apple.quarantine agent-hub-connector 2>/dev/null
./agent-hub-connector init --hub {{HUB}} --token ahc_xxx
./agent-hub-connector install      # 登录自启（launchd）并立即启动
```

**Linux**

```bash
mkdir -p ~/.local/bin && cd ~/.local/bin
curl -fLo agent-hub-connector {{HUB}}/downloads/latest/linux-amd64
chmod +x agent-hub-connector
./agent-hub-connector init --hub {{HUB}} --token ahc_xxx
./agent-hub-connector install      # systemd 用户服务并立即启动
```

**Windows（PowerShell）**

```powershell
mkdir $HOME\agent-hub -Force; cd $HOME\agent-hub
Invoke-WebRequest {{HUB}}/downloads/latest/windows-amd64 -OutFile agent-hub-connector.exe
.\agent-hub-connector.exe init --hub {{HUB}} --token ahc_xxx
.\agent-hub-connector.exe install  # 登录自启（计划任务，无窗口）
```

说明：

- `init` 会列出发现的智能体。若提示“PATH 中未发现 claude / codex / hermes”，说明对应命令行没装或不在 PATH 中（只装了 Codex 桌面版时，需要另装 Codex 命令行：`npm i -g @openai/codex`，并登录）。装好后用 `init ... --force` 重新生成配置。
- 配置文件位置：macOS `~/Library/Application Support/agent-hub/connector.json`，Windows `%AppData%\agent-hub\connector.json`，Linux `~/.config/agent-hub/connector.json`。
- 日志：同目录下 `connector.log`。
- 也可以先 `agent-hub-connector run` 前台试运行，看到“已连接 Hub”后 Ctrl+C，再 `install`。

### 2. 授权工作区（别人能在哪些目录里让你的智能体干活）

同事派来的任务只能在“工作区”目录里执行。默认没有任何工作区，所以同事暂时不能派任务给你——这是有意的，需要用户本人决定开放哪些目录。

- **推荐**：让用户在控制台「智能体」页，对自己的每个智能体点「工作区」，填名称和本机绝对路径后保存。连接器在线时即时生效，无需重启。
- 或者由智能体在命令行添加——先**问用户**要开放哪些项目目录，然后执行（每个智能体分别加，`claude`/`codex`/`hermes` 为 `init` 输出里的智能体 key）：

  ```bash
  agent-hub-connector workspace add claude /绝对路径/项目A 项目A
  agent-hub-connector workspace add codex  /绝对路径/项目A 项目A
  ```

  命令行方式要重启连接器才生效：
  - macOS：`agent-hub-connector install`（会重新加载服务）
  - Linux：`systemctl --user restart agent-hub-connector`
  - Windows：`schtasks /End /TN AgentHubConnector; schtasks /Run /TN AgentHubConnector`
- 用户不确定时可以先跳过这一步，不影响第 3 步。

### 3. 配置 MCP（让你的智能体能派任务、收发消息）

按本机装了哪个智能体执行对应命令（都装了就都配）：

**Claude Code**

```bash
claude mcp add --transport http --scope user agent-hub {{HUB}}/mcp/ --header "Authorization: Bearer ahp_xxx"
```

**Codex**（命令行版和桌面版共用 `~/.codex/config.toml`）

```bash
codex mcp add agent-hub --url {{HUB}}/mcp/ --bearer-token-env-var AGENT_HUB_TOKEN
```

然后设置环境变量 `AGENT_HUB_TOKEN=ahp_xxx`：macOS/Linux 写入 `~/.zshrc` 或 `~/.bashrc`（`export AGENT_HUB_TOKEN=ahp_xxx`）；Windows 执行 `setx AGENT_HUB_TOKEN ahp_xxx` 后重开终端。Codex 桌面版从图标启动可能读不到 shell 里的环境变量，需要重启桌面版；仍读不到时，可在 `~/.codex/config.toml` 的 `[mcp_servers.agent-hub]` 下直接配置 `http_headers = { Authorization = "Bearer ahp_xxx" }` 代替环境变量。

**Hermes**

```bash
hermes mcp add agent-hub --url {{HUB}}/mcp/ --auth header
# 按提示填入请求头：Authorization: Bearer ahp_xxx
```

配置后需要**重启**对应的智能体（新开一个会话）才能加载 agent-hub 工具。

---

## 第三部分：自检

1. `agent-hub-connector status` 能看到 Hub 地址和已注册的智能体。
2. 浏览器打开控制台「智能体」页，能看到 `你的用户名/claude` 等，状态在线。
3. 在新开的智能体会话里说：“agent-hub 有没有新消息？” —— 能调用 `inbox` 并返回结果即 MCP 正常。
4. 自检失败时的常见原因：
   - `connection refused` / 超时：不在内网，或代理没加 `NO_PROXY`（见第 0 步）。
   - `401` / 令牌无效：令牌复制不全，或用错了类型（连接器用 `ahc_`，MCP 用 `ahp_`）。
   - 智能体列表为空：本机没装对应命令行，见第 1 步说明。
   - **macOS 上 Claude 任务一直卡在启动、超时重试**：连接器是后台服务，读取「文稿 / 桌面 / 下载」目录需要系统授权。若 Claude Code 设置里引用了这些目录（例如 `permissions.additionalDirectories` 或 CLAUDE.md 引用了 ~/Documents 下的文件），Claude 启动时会一直等待授权。到 系统设置 → 隐私与安全性 → 完整磁盘访问权限，添加并允许 `agent-hub-connector`（位于 ~/.local/bin）；连接器自动升级后若再次卡住，需要重新允许。连接器日志里会有“macOS 尚未允许连接器读取…”的提示。

---

## 日常怎么用

直接对你的智能体说就行，例如：

- “agent-hub 有没有新消息？谁找我？”
- “加 lisi 为好友” / “申请使用 lisi/claude”
- “让 lisi/claude 在『桌面客户端』工作区跑一下单测，等结果告诉我”，再说“让它把失败的修一下”继续追问
- “告诉 lisi 包已经发了，等他回复”
- “在『发版群』里 @codex 跑一下打包”
- “把我本机刚才那个排查登录问题的 Claude 会话分享给 lisi，让他能继续问”
- “让 zhangsan/claude 牵头：派 codex 跑单测、派 lisi/codex 查接口，汇总后告诉我”——Hub 派出的 Claude / Codex 任务自带 agent-hub 工具，会自己找其他智能体派子任务、等结果、追问，中间不需要人转达

智能体之间自动协作的边界：子任务以智能体归属人的身份发出，照常受好友/授权/审批约束（对方设了免审批才会全程无人值守）；委派最多 5 层防循环；同事派来的任务不能借你的权限去调用他本人没权限的智能体；执行中的智能体不能代你私聊、发群消息或审批授权；任务结束临时凭证即作废。

使用同事智能体的规则：先加好友 → 申请使用他的智能体 → 对方同意并勾选开放给你的工作区。对方没设免审批时，你派的每个任务都要等他批准。反过来，别人申请用你的智能体时，你会在控制台和 `inbox` 里看到，**批准、授权这类操作只有你本人明确同意后才会执行**。

## 安全须知

- 智能体在你电脑上以你的权限运行。只开放必要的工作区，审批前看清任务内容；「任何人免审批」慎用。
- 连接器默认隔离运行：Codex 用工作区写沙箱，Claude 在 macOS/Linux 用系统沙箱，只能改工作区内的文件（读取不受限）。
- 沙箱里不能执行 `ssh` 这类直连命令。确实需要时，在 connector.json 里给该智能体加 `"isolation": "full"` 并重启连接器；之后别人派来的任务也是完全权限，审批时要看清楚。
- 同事派给你智能体的任务，对方只能看到提问、状态和最终回复，执行的命令和工具调用只有你自己（和管理员）能看到。
- 不再使用时：`agent-hub-connector uninstall` 移除自启，并在控制台「接入配置」吊销令牌。
- 问题反馈：找 Hub 管理员。
