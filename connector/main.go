package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
)

var version = "dev"

const usage = `agent-hub-connector：把本机智能体（Claude Code / Codex / Hermes …）注册到 Agent Hub

用法：
  agent-hub-connector init --hub http://<hub地址> --token <连接器令牌> [--name 名称] [--workdir 默认工作目录]
  agent-hub-connector run       前台运行（Ctrl+C 退出）
  agent-hub-connector install   安装开机/登录自启并立即启动
  agent-hub-connector uninstall 移除自启
  agent-hub-connector status    查看配置与已注册的智能体
  agent-hub-connector workspace list|add|remove <智能体key> [目录] [名称]
                                授权智能体可使用的工作区（也可在 Hub 控制台修改，需重启 run 生效）
  agent-hub-connector workhost enable [--image 镜像] [--agents codex,claude] [--cpus 4] [--memory 8g]
                                把本机注册成工作服务器，供同事在「工作」菜单使用（需要 Docker）
  agent-hub-connector workhost agents codex,claude   选择开放给「工作」的智能体（也可在 Hub「服务器维护」里改）
  agent-hub-connector workhost disable|status
  agent-hub-connector update    立即检查并安装新版本（后台运行时会每 6 小时及收到 Hub 通知时自动升级）
  agent-hub-connector version

通用参数：--config <路径>（默认 %s）
`

func main() {
	if len(os.Args) < 2 {
		fmt.Printf(usage, defaultConfigPath())
		os.Exit(2)
	}
	cmd, args := os.Args[1], os.Args[2:]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	configPath := fs.String("config", defaultConfigPath(), "配置文件路径")
	hub := fs.String("hub", "", "Hub 地址，如 http://hub.example.com:8790")
	token := fs.String("token", "", "在 Hub 控制台「连接器」页面创建的令牌（ahc_ 开头）")
	name := fs.String("name", "", "连接器显示名称，默认主机名")
	workdir := fs.String("workdir", "", "智能体默认工作目录，默认用户主目录")
	force := fs.Bool("force", false, "覆盖已有配置")
	image := fs.String("image", "", "workhost：同事容器用的镜像，默认 agent-hub-work:latest")
	agentKeys := fs.String("agents", "", "workhost：开放给「工作」的智能体 key，逗号分隔，默认全部")
	cpus := fs.String("cpus", "", "workhost：每位同事容器的 CPU 上限，如 4")
	memory := fs.String("memory", "", "workhost：每位同事容器的内存上限，如 8g")
	mvnSettings := fs.String("settings", "", "workhost maven：给同事用的 Maven settings.xml（只读挂载）")
	mvnRepo := fs.String("repo", "", "workhost maven：给同事用的 Maven 本地仓库目录")
	mvnShared := fs.Bool("shared", false, "workhost maven：本地仓库可写（同事下载的依赖也写进来）；默认只读")
	positional := parseInterleaved(fs, args)

	var err error
	switch cmd {
	case "init":
		err = cmdInit(*configPath, *hub, *token, *name, *workdir, *force)
	case "run":
		err = cmdRun(*configPath)
	case "install":
		err = installService(*configPath)
	case "uninstall":
		err = uninstallService()
	case "update":
		err = cmdUpdate(*configPath)
	case "workspace":
		err = cmdWorkspace(*configPath, positional)
	case "status":
		err = cmdStatus(*configPath)
	case "workhost":
		err = cmdWorkhost(*configPath, positional, workhostOpts{image: *image, agents: *agentKeys, cpus: *cpus, memory: *memory,
			settings: *mvnSettings, repo: *mvnRepo, shared: *mvnShared})
	case "version":
		fmt.Println(version)
	default:
		fmt.Printf(usage, defaultConfigPath())
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "错误：", err)
		os.Exit(1)
	}
}

func cmdInit(path, hub, token, name, workdir string, force bool) error {
	if hub == "" || token == "" {
		return fmt.Errorf("init 需要 --hub 和 --token")
	}
	if !strings.HasPrefix(token, "ahc_") {
		return fmt.Errorf("令牌应为连接器令牌（ahc_ 开头），个人令牌（ahp_）用于 MCP")
	}
	if _, err := os.Stat(path); err == nil && !force {
		return fmt.Errorf("配置已存在：%s（加 --force 覆盖，或直接编辑该文件）", path)
	}
	if workdir == "" {
		workdir, _ = os.UserHomeDir()
	}
	cfg := &Config{HubURL: strings.TrimRight(hub, "/"), Token: token, Name: name, Agents: detectAgents(workdir), path: path}
	if len(cfg.Agents) == 0 {
		fmt.Println("提示：PATH 中未发现 claude / codex / hermes，请手动编辑配置添加智能体")
	}
	if err := cfg.save(); err != nil {
		return err
	}
	fmt.Printf("已写入配置：%s\n", path)
	for _, a := range cfg.Agents {
		fmt.Printf("  - %s（%s）工作目录 %s\n", a.Key, a.Name, a.Workdir)
	}
	fmt.Println("下一步：agent-hub-connector run 前台试运行，确认无误后 agent-hub-connector install 设置自启")
	return nil
}

func cmdStatus(path string) error {
	cfg, err := loadConfig(path)
	if err != nil {
		return err
	}
	fmt.Printf("配置：%s\nHub：%s\n令牌：%s…\n智能体：\n", path, cfg.HubURL, cfg.Token[:min(10, len(cfg.Token))])
	for _, a := range cfg.Agents {
		fmt.Printf("  - %s  kind=%s  command=%s  workdir=%s\n", a.Key, a.Kind, a.Command, a.Workdir)
	}
	return nil
}

func cmdRun(path string) error {
	cfg, err := loadConfig(path)
	if err != nil {
		return err
	}
	logFile, err := os.OpenFile(filepath.Join(filepath.Dir(path), "connector.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err == nil {
		defer logFile.Close()
		log.SetOutput(io.MultiWriter(os.Stderr, logFile))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.Printf("agent-hub-connector %s 启动，配置 %s", version, path)
	return NewConnector(cfg).Run(ctx)
}

func cmdWorkspace(path string, args []string) error {
	cfg, err := loadConfig(path)
	if err != nil {
		return err
	}
	if len(args) == 0 || args[0] == "list" {
		for _, a := range cfg.Agents {
			fmt.Printf("%s（默认 %s）\n", a.Key, a.Workdir)
			for _, w := range a.Workspaces {
				fmt.Printf("  - %s  %s\n", w.Name, w.Path)
			}
		}
		return nil
	}
	if len(args) < 3 || (args[0] != "add" && args[0] != "remove") {
		return fmt.Errorf("用法：workspace add|remove <智能体key> <目录> [名称]")
	}
	dir, err := filepath.Abs(args[2])
	if err != nil {
		return err
	}
	for i := range cfg.Agents {
		a := &cfg.Agents[i]
		if a.Key != args[1] {
			continue
		}
		var next []Workspace
		for _, w := range a.Workspaces {
			if w.Path != dir {
				next = append(next, w)
			}
		}
		if args[0] == "add" {
			name := ""
			if len(args) > 3 {
				name = args[3]
			}
			next = append(next, Workspace{Name: name, Path: dir})
		}
		clean, rejected := cleanWorkspaces(next)
		if len(rejected) > 0 {
			return fmt.Errorf("目录不存在或不是文件夹：%v", rejected)
		}
		a.Workspaces = clean
		if err := cfg.save(); err != nil {
			return err
		}
		fmt.Printf("已更新 %s 的工作区（共 %d 个），后台运行的连接器重启后生效\n", a.Key, len(clean))
		return nil
	}
	return fmt.Errorf("没有名为 %s 的智能体", args[1])
}

type workhostOpts struct {
	image, agents, cpus, memory string
	settings, repo              string // workhost maven
	shared                      bool
}

// cmdWorkhost turns this machine into a work server (or back), and picks which agents serve the 「工作」menu.
func cmdWorkhost(path string, args []string, o workhostOpts) error {
	image, agentKeys, cpus, memory := o.image, o.agents, o.cpus, o.memory
	cfg, err := loadConfig(path)
	if err != nil {
		return err
	}
	sub := "status"
	if len(args) > 0 {
		sub = args[0]
	}
	switch sub {
	case "enable":
		if !dockerAvailable() {
			return fmt.Errorf("没有检测到可用的 Docker：工作服务器为每位同事起一个容器，请先安装并启动 Docker")
		}
		if cfg.Workhost == nil {
			cfg.Workhost = &WorkhostConfig{}
		}
		w := cfg.Workhost
		if image != "" {
			w.Image = image
		} else if w.Image == "" {
			w.Image = "agent-hub-work:latest"
		}
		if cpus != "" {
			w.CPUs = cpus
		}
		if memory != "" {
			w.Memory = memory
		}
		if err := pickWorkAgents(cfg, agentKeys); err != nil {
			return err
		}
		addLocalAccounts(cfg)
	case "agents":
		if cfg.Workhost == nil {
			return fmt.Errorf("本机还没有开启工作服务器，先执行 workhost enable")
		}
		keys := agentKeys
		if len(args) > 1 {
			keys = args[1]
		}
		if err := pickWorkAgents(cfg, keys); err != nil {
			return err
		}
		addLocalAccounts(cfg)
	case "maven":
		if cfg.Workhost == nil {
			return fmt.Errorf("本机还没有开启工作服务器，先执行 workhost enable")
		}
		if len(args) > 1 && args[1] == "off" {
			cfg.Workhost.Maven = nil
			break
		}
		m, err := newMavenShare(o.settings, o.repo, o.shared)
		if err != nil {
			return err
		}
		cfg.Workhost.Maven = m
		fmt.Println("注意：settings.xml 里的私服账号密码，同事和他们的智能体在容器里都能读到；只开放给你信任的人（Hub「服务器维护 → 谁能使用」）。")
	case "disable":
		cfg.Workhost = nil
		for i := range cfg.Agents {
			cfg.Agents[i].Workhost = false
		}
	case "status":
		if cfg.Workhost == nil {
			fmt.Println("本机未开启工作服务器。开启：agent-hub-connector workhost enable")
			return nil
		}
		w := cfg.Workhost
		fmt.Printf("工作服务器：已开启，镜像 %s，每人 CPU %s 内存 %s，同时运行 %d 个会话\n", w.Image, orDash(w.CPUs), orDash(w.Memory),
			max(w.MaxSessions, 3))
		for _, a := range cfg.Agents {
			mark := "  "
			if cfg.isWorkAgent(a) {
				mark = "✓ "
			}
			fmt.Printf("  %s%s（%s）\n", mark, a.Key, a.Name)
		}
		if m := w.Maven; m != nil {
			unset := func(p string) string {
				if p == "" {
					return "未设置"
				}
				return p
			}
			fmt.Printf("  Maven：settings %s，本地仓库 %s（%s）\n", unset(m.Settings), unset(m.Repository), map[bool]string{true: "同事可写", false: "只读"}[m.Shared])
		}
		for _, a := range w.Accounts {
			where := a.Home
			if a.Keychain != "" {
				where = "钥匙串 " + a.Keychain
			}
			fmt.Printf("  账号 %s（%s）%s\n", a.ID, a.Kind, where)
		}
		return nil
	default:
		return fmt.Errorf("用法：workhost enable|agents|maven|disable|status")
	}
	if err := cfg.save(); err != nil {
		return err
	}
	fmt.Println("已保存。后台运行的连接器重启后生效；之后可在 Hub「服务器维护」里管理开放的智能体、账号和谁能使用。")
	return cmdWorkhost(path, []string{"status"}, workhostOpts{})
}

// pickWorkAgents marks the agents offered to the 「工作」menu (all of them when keys is empty).
func pickWorkAgents(cfg *Config, keys string) error {
	want := map[string]bool{}
	for _, k := range strings.Split(keys, ",") {
		if k = strings.TrimSpace(k); k != "" {
			want[k] = true
		}
	}
	found := 0
	for i := range cfg.Agents {
		a := &cfg.Agents[i]
		a.Workhost = len(want) == 0 || want[a.Key]
		if a.Workhost {
			found++
		}
	}
	for k := range want {
		if !hasAgent(cfg, k) {
			return fmt.Errorf("本机没有名为 %s 的智能体", k)
		}
	}
	if found == 0 {
		return fmt.Errorf("至少开放一个智能体")
	}
	return nil
}

func hasAgent(cfg *Config, key string) bool {
	for _, a := range cfg.Agents {
		if a.Key == key {
			return true
		}
	}
	return false
}

// localAccounts: on a personal machine the agents' own logins are the accounts colleagues' turns run under.
// addLocalAccounts registers this machine's own logins for the offered agent kinds that have no account yet.
func addLocalAccounts(cfg *Config) {
	w := cfg.Workhost
	for _, a := range localAccounts(cfg) {
		covered := false
		for _, have := range w.Accounts {
			covered = covered || have.Kind == a.Kind
		}
		if !covered {
			w.Accounts = append(w.Accounts, a)
		}
	}
}

func localAccounts(cfg *Config) []WorkAccount {
	home, _ := os.UserHomeDir()
	host, _ := os.Hostname()
	var out []WorkAccount
	for _, a := range cfg.Agents {
		if !cfg.isWorkAgent(a) {
			continue
		}
		files, ok := credentialFiles[a.Kind]
		if !ok {
			continue
		}
		dir := filepath.Join(home, files.dir)
		if env := os.Getenv("CODEX_HOME"); a.Kind == "codex" && env != "" {
			dir = env
		}
		if _, err := os.Stat(filepath.Join(dir, files.name)); err != nil {
			if a.Kind == "claude" && keychainExists(claudeKeychain) {
				out = append(out, WorkAccount{ID: a.Kind + "-" + strings.Split(host, ".")[0], Kind: a.Kind, Home: dir, Keychain: claudeKeychain})
				continue
			}
			fmt.Printf("提示：没找到 %s 的登录凭据（%s），请在配置 workhost.accounts 里登记账号目录\n", a.Key, filepath.Join(dir, files.name))
			continue
		}
		out = append(out, WorkAccount{ID: a.Kind + "-" + strings.Split(host, ".")[0], Kind: a.Kind, Home: dir})
	}
	return out
}

func orDash(s string) string {
	if s == "" {
		return "不限"
	}
	return s
}

// parseInterleaved accepts flags before, between and after positional words
// (`workhost enable --agents codex -config x.json`); the standard flag package stops at the first word.
func parseInterleaved(fs *flag.FlagSet, args []string) []string {
	var positional []string
	for {
		_ = fs.Parse(args)
		if fs.NArg() == 0 {
			return positional
		}
		positional = append(positional, fs.Arg(0))
		args = fs.Args()[1:]
	}
}
