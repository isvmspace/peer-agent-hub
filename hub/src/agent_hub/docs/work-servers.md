# 工作服务器部署手册

工作服务器就是一台装了 Docker、跑着连接器的电脑，供同事在控制台「工作」菜单里写代码：每位同事在这台机器上有一个**只属于自己的容器**，在里面克隆仓库、和智能体对话改代码，再用**自己的 GitLab 账号**提交、推送。

- Hub 地址：{{HUB}}
- 控制台：{{HUB}}/console/ （「接入与规则 → 服务器维护」管理工作服务器）
- 本手册：{{HUB}}/docs/work-servers.md
- 连接器怎么装：{{HUB}}/docs/onboarding.md

---

## 一、它是怎么工作的

| 东西 | 在哪里 | 说明 |
|---|---|---|
| 同事的容器 | 工作服务器上，名为 `ahw-<用户名>` | 每人一个，闲置 30 分钟自动休眠，下次使用自动启动 |
| 同事的文件 | Docker 数据卷 `ahw-<用户名>`，挂在容器的 `/home/dev` | 重建容器文件不丢；依赖缓存（`~/.m2`、`~/.npm`、`~/go`）也在这里 |
| 智能体 | 在同事的容器里运行 | 容器本身就是隔离，同事碰不到服务器主人的文件，也碰不到别的同事的容器 |
| 智能体账号 | 服务器主人在这台机器上登录的 Codex / Claude Code 账号 | 同事的会话会用掉这些账号的额度；可以登记多个账号，额度不足时自动切换 |
| 同事的 GitLab 凭证 | 加密保存在 Hub | 只在克隆、拉取、推送那一条 git 命令里临时传入，不写进容器，智能体看不到 |

每个会话在每个仓库里有自己的工作副本和分支，多个会话同时进行互不影响；每位同事默认最多 3 个会话同时执行。

---

## 二、服务器主人：部署步骤

### 0. 准备

- 一台常开的电脑，推荐 Linux 服务器，macOS 也可以。按同时在用的人数估算，每人约 2–4 核、4–8 GB 内存，外加仓库和依赖缓存的磁盘空间。
- 能访问 Hub：`curl -s {{HUB}}/healthz` 返回 `{"status":"ok",...}`。
- 容器里需要能访问 GitLab 和各语言的依赖源（npm、Maven 私服等）。

### 1. 安装并启动 Docker

- Linux：安装 Docker Engine，把运行连接器的用户加入 `docker` 组（`sudo usermod -aG docker $USER`，重新登录后生效）。
- macOS：安装 Docker Desktop 并启动。

验证：`docker info` 能正常输出。

### 2. 安装连接器

按 {{HUB}}/docs/onboarding.md 的「第一部分」「第二部分」安装并运行连接器（`init` → `run` 试运行 → `install` 设为自启）。在控制台「服务器维护」里能看到这台机器在线即可。

把要开放给同事的智能体（`codex`、`claude`）在这台机器上**先登录好**：连接器会把这里的登录账号登记为工作服务器的账号。

### 3. 构建同事容器用的镜像

镜像的 Dockerfile 在 Agent Hub 源码的 `deploy/workhost/Dockerfile`。在源码目录执行：

```bash
docker build -t agent-hub-work:latest deploy/workhost
```

默认包含 git、Node.js 22、Python 3、Java 17 + Maven 3.9、Codex CLI 和 Claude Code CLI。可以按需裁剪或增加：

| 构建参数 | 作用 |
|---|---|
| `--build-arg WITH_JAVA=0` | 不装 Java 和 Maven |
| `--build-arg WITH_GO=1` | 加装 Go |
| `--build-arg WITH_CLAUDE=0` | 不装 Claude Code CLI |
| `--build-arg CODEX_VERSION=x.y.z` | 指定 Codex CLI 版本 |

这台机器拿不到源码时，可以在有源码的机器上构建后导出，再拷过来导入：

```bash
docker save agent-hub-work:latest | gzip > agent-hub-work.tar.gz   # 有源码的机器
docker load < agent-hub-work.tar.gz                                # 工作服务器
```

### 4. 开启工作服务器

```bash
agent-hub-connector workhost enable --agents codex,claude --cpus 4 --memory 8g
```

| 参数 | 说明 |
|---|---|
| `--agents` | 开放给「工作」的智能体，逗号分隔；不写就是本机全部智能体。没开放的智能体照常作为你自己的智能体使用 |
| `--cpus` / `--memory` | 每位同事容器的 CPU 和内存上限，不写就不限 |
| `--image` | 用别的镜像，默认 `agent-hub-work:latest` |

执行后会打印当前状态，可以随时用 `agent-hub-connector workhost status` 再看。

### 5. 重启连接器

```bash
agent-hub-connector install    # 已装成后台服务的，重新执行一次即可重启
```

前台试运行的，按 Ctrl+C 停掉后重新 `agent-hub-connector run`。

### 6. 在控制台设置谁能使用

打开「服务器维护」，找到这台机器：

- **开放给「工作」的智能体**：随时勾选或取消，不用回到命令行。
- **谁能使用**：「所有同事」或「只有指定的人」。服务器上的账号额度和共享出去的配置（例如 Maven 私服密码）同事都能用到，只开放给信任的人。
- **智能体账号**：查看每个账号的剩余额度，手动指定当前账号；「额度不足时自动切换账号」可以设置阈值，以及一轮执行到一半额度用完时是自动续聊还是等同事确认。
- **同事容器**：查看每个容器的状态、磁盘和最近活动，可以休眠、启动、重启。

---

## 三、可选配置

### 共享 Maven 配置和本地仓库

让同事用你的 `settings.xml`（私服地址和账号）和你已经下载好的本地仓库，免得每人重新下载一遍：

```bash
agent-hub-connector workhost maven --settings ~/.m2/settings.xml --repo ~/.m2/repository
agent-hub-connector workhost maven --settings ~/.m2/settings.xml --repo ~/.m2/repository --shared   # 同事下载的依赖也写进来
agent-hub-connector workhost maven off                                                               # 停止共享
```

- 默认**只读**：你的本地仓库作为后备，同事构建时先查自己的仓库、找不到再读你的，同事下载的新依赖只写进他们自己的数据卷，不会改动你的目录。
- 加 `--shared` 后，所有同事和你共用同一个本地仓库，新下载的依赖也写进去，磁盘最省，但同事的构建会写入你的目录。
- **注意**：`settings.xml` 里的私服账号密码，同事和他们的智能体在容器里都能读到。

改完同样要重启连接器。

### 其他配置项

以下在连接器配置文件的 `workhost` 一节里手动修改，改完重启连接器。配置文件位置：macOS 为 `~/Library/Application Support/agent-hub/connector.json`，Linux 为 `~/.config/agent-hub/connector.json`，Windows 为 `%AppData%\agent-hub\connector.json`。

| 字段 | 作用 | 默认 |
|---|---|---|
| `accounts` | 智能体账号列表：`{"id": "codex-2", "kind": "codex", "home": "/path/to/codex-home"}`。`home` 是存放该账号登录信息的目录（Codex 为含 `auth.json` 的目录）。要登记第二个 Codex 账号，就用 `CODEX_HOME=<新目录> codex login` 登录到新目录，再把它加到这里 | 开启时自动登记本机已登录的账号 |
| `max_sessions_per_user` | 每位同事同时执行的会话数 | 3 |
| `idle_minutes` | 同事容器闲置多少分钟后休眠 | 30 |
| `env` | 给容器加的环境变量，例如代理：`["HTTPS_PROXY=http://proxy:7890", "NO_PROXY=gitlab.example.com"]` | 无 |
| `mounts` | 额外挂进容器的目录，写法同 `docker -v`，例如只读的测试数据 `"/data/fixtures:/opt/fixtures:ro"` | 无 |

修改镜像、CPU、内存、挂载、环境变量或 Maven 设置后，同事的容器会在下次启动时按新设置重建，数据卷里的文件不受影响。

---

## 四、同事：开始使用

1. 打开控制台「工作」菜单，选一台工作服务器（推荐「自动选择」：在线且人数最少的一台），点「开通我的容器」。
2. **设置 GitLab 凭证**：管理员配置了 GitLab 授权时，点「前往 GitLab 授权」即可；否则在 GitLab「偏好设置 → 访问令牌」生成个人访问令牌，勾选 `api`、`read_repository`、`write_repository`，填到页面上。同时填写提交姓名和邮箱。
3. **添加仓库**：「从 GitLab 选择项目」或填写仓库地址，克隆到一个目录里。目录名只用于显示，可以写目录说明告诉智能体背景。
4. **新建会话**：选智能体、目录，写下要做的事。每个仓库从目录里显示的当前分支拉最新代码开始。
5. **推送**：默认「确认后推送」——智能体提交后你在「改动」里看过再推；也可以选「直接推送」，每轮完成后自动推到会话分支。

---

## 五、常见问题

| 现象 | 处理 |
|---|---|
| `workhost enable` 提示没有检测到 Docker | Docker 没装或没启动；Linux 上确认当前用户在 `docker` 组里 |
| 开通容器时报找不到镜像 | 第 3 步的镜像没构建，或名字和 `--image` 不一致；`docker images` 里应该能看到 `agent-hub-work` |
| 「服务器维护」里没有出现账号 | 开启时本机还没登录对应智能体；登录后重新执行 `workhost enable`，或在配置文件 `accounts` 里手动登记 |
| 同事克隆、推送失败 | 同事没设置 GitLab 凭证，或令牌权限不够（需要 `read_repository`、`write_repository`，建合并请求还需要 `api`） |
| 容器里装依赖很慢或连不上 | 在配置的 `env` 里加代理；Maven 项目可以按「共享 Maven 配置」使用私服 |
| 不想再当工作服务器 | `agent-hub-connector workhost disable`，重启连接器；同事的容器和数据卷不会自动删除，需要时用 `docker rm -f ahw-<用户名>` 和 `docker volume rm ahw-<用户名>` 清理（数据卷删除后同事的文件就没了） |
