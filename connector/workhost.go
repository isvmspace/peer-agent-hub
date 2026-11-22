package main

// Workhost mode: this connector serves the hub's 「工作」menu. Every colleague gets a container of their own
// (ahw-<user>, home on a named volume), their repositories are cloned into folders there, and every work
// session gets one git worktree per repository on the session's branch, so sessions never touch each other's
// files. The agent CLI runs inside the container (the container is the isolation, so the CLI's own sandbox is
// off). The colleague's GitLab token only reaches git through environment variables of a single command; it
// is never written into the container.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

type WorkhostConfig struct {
	Image       string      `json:"image"`                           // container image with git and the agent CLIs
	Docker      string      `json:"docker,omitempty"`                // docker binary, default "docker"
	CPUs        string      `json:"cpus,omitempty"`                  // per-colleague limit, e.g. "4"
	Maven       *MavenShare `json:"maven,omitempty"`                 // settings.xml / local repository lent to the colleagues
	Memory      string      `json:"memory,omitempty"`                // per-colleague limit, e.g. "8g"
	MaxSessions int         `json:"max_sessions_per_user,omitempty"` // turns running at once per colleague, default 3
	CodexAuth   string      `json:"codex_auth,omitempty"`            // single-account shorthand: host path of a Codex auth.json
	Mounts      []string    `json:"mounts,omitempty"`                // extra docker -v specs (read-only test repos etc.)
	Env         []string    `json:"env,omitempty"`                   // extra KEY=VALUE for the container (proxy etc.)
	// Accounts the agents run under; the hub picks one per turn (and switches when one runs out)
	Accounts    []WorkAccount `json:"accounts,omitempty"`
	IdleMinutes int           `json:"idle_minutes,omitempty"` // stop an idle colleague's container after this, default 30
	CodexCLI    string        `json:"codex_cli,omitempty"`    // host codex binary used to read account quotas, default "codex"
}

// WorkAccount is one agent account on the work server: a folder holding its credentials (Codex: auth.json).
type WorkAccount struct {
	ID   string `json:"id"`
	Kind string `json:"kind"` // codex, claude
	Home string `json:"home"` // folder holding the login on this machine (CODEX_HOME for codex)
	// Keychain names the macOS keychain item holding the login instead (Claude Code: "Claude Code-credentials")
	Keychain string `json:"keychain,omitempty"`
}

type WorkRepo struct {
	Name   string `json:"name"`
	URL    string `json:"url"`
	Branch string `json:"branch"`
}

// WorkContext is what the hub sends with a work session's turn or a work_* request.
type WorkContext struct {
	User           string     `json:"user"`
	GitName        string     `json:"git_name"`
	GitEmail       string     `json:"git_email"`
	Session        string     `json:"session"`
	Branch         string     `json:"branch"`
	Folder         string     `json:"folder"`
	DirName        string     `json:"dir_name"`
	DirDescription string     `json:"dir_description"`
	Repos          []WorkRepo `json:"repos"`
	Token          string     `json:"token"`
	Account        string     `json:"account"` // agent account chosen by the hub for this turn

	syncFailed []string // repositories whose remote branches could not be brought up to date this turn
	bases      []string // "repo → branch" each repository works on this turn
}

const workHome = "/home/dev"

var safeName = regexp.MustCompile(`^[A-Za-z0-9._-]{1,80}$`)
var safeBranch = regexp.MustCompile(`^[A-Za-z0-9._/-]{1,120}$`)

func (w *WorkContext) validate() error {
	if !safeName.MatchString(w.User) {
		return fmt.Errorf("工作区用户标识不合法")
	}
	for _, v := range []string{w.Session, w.Folder} {
		if v != "" && !safeName.MatchString(v) {
			return fmt.Errorf("工作区目录或会话标识不合法")
		}
	}
	if w.Branch != "" && (!safeBranch.MatchString(w.Branch) || strings.Contains(w.Branch, "..")) {
		return fmt.Errorf("会话分支名不合法")
	}
	for _, r := range w.Repos {
		if err := r.validate(); err != nil {
			return err
		}
	}
	return nil
}

func (r WorkRepo) validate() error {
	if !safeName.MatchString(r.Name) || !safeBranch.MatchString(r.Branch) || strings.Contains(r.Branch, "..") {
		return fmt.Errorf("仓库 %q 的名称或分支不合法", r.Name)
	}
	if !(strings.HasPrefix(r.URL, "https://") || strings.HasPrefix(r.URL, "http://") || strings.HasPrefix(r.URL, "file:///")) {
		return fmt.Errorf("仓库 %s 的地址不合法", r.Name)
	}
	return nil
}

func (w *WorkContext) container() string { return "ahw-" + w.User }
func (w *WorkContext) srcDir(repo string) string {
	return workHome + "/work/" + w.Folder + "/" + repo
}
func (w *WorkContext) sessionDir() string { return workHome + "/sessions/" + w.Session }

type workhost struct {
	cfg    WorkhostConfig
	hub    string // the hub this machine serves; its containers and volumes carry it, so another hub's are never reused
	mu     sync.Mutex
	slots  map[string]chan struct{} // per colleague: turns running at once
	boot   map[string]*sync.Mutex   // per colleague: one container setup at a time
	active map[string]time.Time     // per colleague: last time the container was used
	busy   map[string]int           // per colleague: turns running now
}

func newWorkhost(cfg WorkhostConfig) *workhost {
	if cfg.Docker == "" {
		cfg.Docker = "docker"
	}
	if cfg.MaxSessions <= 0 {
		cfg.MaxSessions = 3
	}
	if cfg.IdleMinutes <= 0 {
		cfg.IdleMinutes = 30
	}
	if cfg.CodexCLI == "" {
		cfg.CodexCLI = "codex"
	}
	if len(cfg.Accounts) == 0 && cfg.CodexAuth != "" {
		cfg.Accounts = []WorkAccount{{ID: "codex-default", Kind: "codex", Home: filepath.Dir(cfg.CodexAuth)}}
	}
	return &workhost{cfg: cfg, slots: map[string]chan struct{}{}, boot: map[string]*sync.Mutex{},
		active: map[string]time.Time{}, busy: map[string]int{}}
}

// touch records that a colleague's container was just used (it keeps it from idling out).
func (h *workhost) touch(user string, delta int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.active[user] = time.Now()
	h.busy[user] += delta
}

// account returns the account the hub chose, or the first one of that kind.
func (h *workhost) account(id, kind string) (WorkAccount, bool) {
	for _, a := range h.cfg.Accounts {
		if a.Kind == kind && (id == "" || a.ID == id) {
			return a, true
		}
	}
	return WorkAccount{}, false
}

func (h *workhost) slot(user string) chan struct{} {
	h.mu.Lock()
	defer h.mu.Unlock()
	s, ok := h.slots[user]
	if !ok {
		s = make(chan struct{}, h.cfg.MaxSessions)
		h.slots[user] = s
	}
	return s
}

func (h *workhost) bootLock(user string) *sync.Mutex {
	h.mu.Lock()
	defer h.mu.Unlock()
	m, ok := h.boot[user]
	if !ok {
		m = &sync.Mutex{}
		h.boot[user] = m
	}
	return m
}

// docker runs one docker command; env entries are passed by name only (`-e NAME`) so values never reach argv.
func (h *workhost) docker(ctx context.Context, stdin []byte, env map[string]string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, h.cfg.Docker, args...)
	cmd.Env = os.Environ()
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	var out, errOut bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errOut
	err := cmd.Run()
	if err != nil {
		msg := strings.TrimSpace(errOut.String())
		if msg == "" {
			msg = strings.TrimSpace(out.String())
		}
		return out.String(), fmt.Errorf("%s", redactText(truncate(msg, 2000)))
	}
	return out.String(), nil
}

// inContainer runs a command in the colleague's container as the dev user.
func (h *workhost) inContainer(ctx context.Context, w *WorkContext, env map[string]string, stdin []byte, argv ...string) (string, error) {
	args := []string{"exec"}
	if stdin != nil {
		args = append(args, "-i")
	}
	for k := range env {
		args = append(args, "-e", k)
	}
	args = append(args, w.container())
	args = append(args, argv...)
	return h.docker(ctx, stdin, env, args...)
}

// gitEnv carries the GitLab token as an HTTP header for one git command; nothing is stored.
func gitEnv(token string) map[string]string {
	env := map[string]string{"GIT_TERMINAL_PROMPT": "0"}
	if token != "" {
		basic := base64.StdEncoding.EncodeToString([]byte("oauth2:" + token))
		env["GIT_CONFIG_COUNT"] = "1"
		env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
		env["GIT_CONFIG_VALUE_0"] = "Authorization: Basic " + basic
	}
	return env
}

func (h *workhost) git(ctx context.Context, w *WorkContext, withToken bool, args ...string) (string, error) {
	token := ""
	if withToken {
		token = w.Token
	}
	out, err := h.inContainer(ctx, w, gitEnv(token), nil, append([]string{"git"}, args...)...)
	if err != nil && w.Token != "" {
		err = fmt.Errorf("%s", strings.ReplaceAll(err.Error(), w.Token, "***"))
	}
	return out, err
}

// ensure starts the colleague's container (creating it and its volume the first time) and sets their git identity.
func (h *workhost) ensure(ctx context.Context, w *WorkContext) error {
	if err := w.validate(); err != nil {
		return err
	}
	lock := h.bootLock(w.User)
	lock.Lock()
	defer lock.Unlock()
	h.touch(w.User, 0)
	name := w.container()
	if err := h.ownVolume(ctx, name); err != nil {
		return err
	}
	info, err := h.docker(ctx, nil, nil, "inspect", "-f",
		`{{index .Config.Labels "agent-hub.hub"}}|{{index .Config.Labels "agent-hub.spec"}}|{{.State.Running}}`, name)
	if err == nil {
		parts := strings.SplitN(strings.TrimSpace(info), "|", 3)
		running := len(parts) == 3 && parts[2] == "true"
		switch {
		case len(parts) != 3 || parts[0] != h.hub:
			return fmt.Errorf("本机已有同名容器 %s，但不是这个 Hub 建的（可能是测试留下的），请服务器归属人删除后重试", name)
		case parts[1] != h.spec() && !running:
			// the image, limits or mounts changed while it slept: recreate it on the same volume, files stay
			if _, err := h.docker(ctx, nil, nil, "rm", "-f", name); err != nil {
				return fmt.Errorf("按新配置重建工作容器失败：%v", err)
			}
			err = fmt.Errorf("recreate")
		case !running:
			if _, err := h.docker(ctx, nil, nil, "start", name); err != nil {
				return fmt.Errorf("启动工作容器失败：%v", err)
			}
		}
	}
	if err != nil {
		args := []string{"run", "-d", "--name", name, "--hostname", "ws-" + w.User, "--label", "agent-hub.work=" + w.User,
			"--label", "agent-hub.hub=" + h.hub, "--label", "agent-hub.spec=" + h.spec(),
			"--restart", "unless-stopped", "-v", name + ":" + workHome}
		if h.cfg.CPUs != "" {
			args = append(args, "--cpus", h.cfg.CPUs)
		}
		if h.cfg.Memory != "" {
			args = append(args, "--memory", h.cfg.Memory)
		}
		for _, m := range h.cfg.Mounts {
			args = append(args, "-v", m)
		}
		for _, e := range h.cfg.Env {
			args = append(args, "-e", e)
		}
		args = append(args, h.cfg.Maven.dockerArgs()...)
		args = append(args, h.cfg.Image, "sleep", "infinity")
		if _, err := h.docker(ctx, nil, nil, args...); err != nil {
			return fmt.Errorf("创建工作容器失败：%v", err)
		}
		log.Printf("已为 %s 创建工作容器 %s", w.User, name)
	}
	if w.GitName != "" {
		if _, err := h.git(ctx, w, false, "config", "--global", "user.name", w.GitName); err != nil {
			return err
		}
		if _, err := h.git(ctx, w, false, "config", "--global", "user.email", w.GitEmail); err != nil {
			return err
		}
	}
	return nil
}

// spec sums up what a container is created with; a sleeping container whose spec differs is recreated.
func (h *workhost) spec() string {
	c := h.cfg
	sum := sha256.Sum256([]byte(strings.Join([]string{c.Image, c.CPUs, c.Memory, strings.Join(c.Mounts, "\n"), strings.Join(c.Env, "\n"), c.Maven.spec()}, "\x00")))
	return fmt.Sprintf("%x", sum[:8])
}

// ownVolume creates the colleague's volume for this hub, and refuses one another hub (or a test) left behind.
func (h *workhost) ownVolume(ctx context.Context, name string) error {
	owner, err := h.docker(ctx, nil, nil, "volume", "inspect", "-f", `{{index .Labels "agent-hub.hub"}}`, name)
	if err != nil {
		if _, err := h.docker(ctx, nil, nil, "volume", "create", "--label", "agent-hub.hub="+h.hub, name); err != nil {
			return fmt.Errorf("创建工作数据卷失败：%v", err)
		}
		return nil
	}
	if strings.TrimSpace(owner) != h.hub {
		return fmt.Errorf("本机已有同名数据卷 %s，但不是这个 Hub 建的（可能是测试留下的），请服务器归属人确认后删除再重试", name)
	}
	return nil
}

func (h *workhost) exists(ctx context.Context, w *WorkContext, path string) bool {
	_, err := h.inContainer(ctx, w, nil, nil, "test", "-e", path)
	return err == nil
}

// clone puts a repository into a directory, or refreshes it when it is already there.
func (h *workhost) clone(ctx context.Context, w *WorkContext, r WorkRepo) error {
	if err := r.validate(); err != nil {
		return err
	}
	if err := h.ensure(ctx, w); err != nil {
		return err
	}
	src := w.srcDir(r.Name)
	if h.exists(ctx, w, src+"/.git") {
		if _, err := h.git(ctx, w, true, "-C", src, "fetch", "--prune", "origin"); err != nil {
			return err
		}
		if _, err := h.git(ctx, w, false, "-C", src, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/"+r.Branch); err != nil {
			return fmt.Errorf("%s 的远端没有分支 %s", r.Name, r.Branch)
		}
		return nil
	}
	if _, err := h.inContainer(ctx, w, nil, nil, "mkdir", "-p", workHome+"/work/"+w.Folder); err != nil {
		return err
	}
	if _, err := h.git(ctx, w, true, "clone", "--branch", r.Branch, r.URL, src); err != nil {
		return fmt.Errorf("克隆 %s 失败：%v", r.Name, err)
	}
	return nil
}

// prepareSession gives the session its own worktree of every repository, on the session branch.
func (h *workhost) prepareSession(ctx context.Context, w *WorkContext) error {
	w.syncFailed, w.bases = nil, nil
	defer func() {
		for _, r := range w.Repos {
			if dst := w.sessionDir() + "/" + r.Name; h.exists(ctx, w, dst+"/.git") {
				w.bases = append(w.bases, r.Name+" → "+h.baseBranch(ctx, w, dst, r.Branch))
			}
		}
	}()
	if err := h.ensure(ctx, w); err != nil {
		return err
	}
	if _, err := h.inContainer(ctx, w, nil, nil, "mkdir", "-p", w.sessionDir()); err != nil {
		return err
	}
	for _, r := range w.Repos {
		dst := w.sessionDir() + "/" + r.Name
		if h.exists(ctx, w, dst+"/.git") {
			// every turn starts with all remote branches up to date: the agent has no token to fetch them itself
			if _, err := h.git(ctx, w, true, "-C", w.srcDir(r.Name), "fetch", "--prune", "origin"); err != nil {
				log.Printf("会话 %s 同步 %s 远端分支失败：%v", w.Session, r.Name, err)
				w.syncFailed = append(w.syncFailed, r.Name)
			}
			continue
		}
		if err := h.clone(ctx, w, r); err != nil {
			return err
		}
		src := w.srcDir(r.Name)
		// start from the latest remote state of the repository's branch
		if _, err := h.git(ctx, w, true, "-C", src, "fetch", "--prune", "origin"); err != nil {
			return fmt.Errorf("拉取 %s 最新代码失败：%v", r.Name, err)
		}
		if _, err := h.git(ctx, w, false, "-C", src, "worktree", "add", "-B", w.Branch, dst, "origin/"+r.Branch); err != nil {
			return fmt.Errorf("为会话准备 %s 失败：%v", r.Name, err)
		}
	}
	return nil
}

// copyAuth gives the container the credentials of the account chosen for this turn. It returns the account and
// what was lent, so a login the CLI refreshes during the turn can be written back.
func (h *workhost) copyAuth(ctx context.Context, w *WorkContext, kind string) (WorkAccount, []byte, error) {
	file, ok := credentialFiles[kind]
	if !ok {
		return WorkAccount{}, nil, nil
	}
	acct, found := h.account(w.Account, kind)
	if !found {
		if w.Account != "" {
			return acct, nil, fmt.Errorf("工作服务器上没有账号 %s", w.Account)
		}
		return acct, nil, nil // no account configured: the image's own login, if any
	}
	data, err := acct.readCredentials()
	if err != nil {
		return acct, nil, fmt.Errorf("读取 %s 账号凭据失败：%v", kind, err)
	}
	_, err = h.inContainer(ctx, w, nil, data, "sh", "-c",
		`umask 077 && mkdir -p "$HOME/`+file.dir+`" && cat > "$HOME/`+file.dir+`/`+file.name+`"`)
	return acct, data, err
}

// credentialFiles: where each agent CLI keeps its login, inside the account folder and under $HOME in the container.
var credentialFiles = map[string]struct{ dir, name string }{
	"codex":  {".codex", "auth.json"},
	"claude": {".claude", ".credentials.json"},
}

// workPrompt tells the agent where it is on the first turn: the directory, its repositories and the rules.
func workPrompt(t Task) string {
	w := t.Work
	if t.SessionID != "" {
		// a resumed turn: the agent has the first turn's rules in its context; it only needs this turn's sync
		return turnNote(w) + "\n\n" + t.Prompt
	}
	var b strings.Builder
	fmt.Fprintf(&b, "[Agent Hub 工作会话] 你在同事 %s 的工作目录「%s」里。", t.From, w.DirName)
	if w.DirDescription != "" {
		fmt.Fprintf(&b, "目录说明：%s。", w.DirDescription)
	}
	names := make([]string, 0, len(w.Repos))
	for _, r := range w.Repos {
		names = append(names, r.Name)
	}
	fmt.Fprintf(&b, "当前目录下的仓库：%s。每个仓库的本地分支都叫 %s，这只是工作副本的内部名字："+
		"它基于哪个远端分支（如 origin/feature/v1.4.0），提交就推到那个分支，对同事说分支时说那个远端分支的名字。", strings.Join(names, "、"), w.Branch)
	fmt.Fprintf(&b, "你没有 GitLab 凭证，不要 git fetch / pull / push：每轮开始前平台已用同事的凭证把远端分支（origin/*）同步到最新。"+
		"同事要拉最新代码时：git merge --ff-only origin/<分支>；有还没推送的本地提交、不能快进时用 git rebase origin/<分支>（有冲突就解决后继续）。"+
		"始终留在本地分支 %[1]s 上：同事要换到别的分支（如 1.4.0）上工作时，用 git checkout -B %[1]s origin/<那个分支>，"+
		"不要 checkout 别的本地分支。", w.Branch)
	b.WriteString("改完代码后在对应仓库里 git commit（提交说明写清楚改了什么），推送由同事在界面上确认。" +
		"不要修改 git 的 user.name / user.email。最后给出简明的结果总结。\n")
	b.WriteString(turnNote(w) + "\n\n")
	b.WriteString(t.Prompt)
	return b.String()
}

// turnNote tells the agent, every turn, which branch each repository works on and how fresh the remote branches are.
func turnNote(w *WorkContext) string {
	var b strings.Builder
	if len(w.bases) > 0 {
		fmt.Fprintf(&b, "[各仓库工作的分支（提交推到这里）：%s。本地分支名 %s 只是工作副本的内部名字，对同事说分支时用上面的名字，不要提它]\n",
			strings.Join(w.bases, "，"), w.Branch)
	}
	if len(w.syncFailed) == 0 {
		b.WriteString("[本轮开始前远端分支 origin/* 已同步到最新；拉最新代码就合并对应的 origin/<分支>，不要自己 fetch/pull]")
		return b.String()
	}
	b.WriteString(fmt.Sprintf("[本轮开始前 %s 的远端分支同步失败（多半是同事的 GitLab 令牌失效或没有权限，请同事在「工作 → Git 设置」里更新令牌），"+
		"这些仓库的 origin/* 可能不是最新；其余仓库已同步。不要自己 fetch/pull]", strings.Join(w.syncFailed, "、")))
	return b.String()
}

// keepSessionBranch brings each worktree back onto the session branch when the agent switched to another branch
// (or detached HEAD): the session branch moves to where the agent is, so what it did there is what gets pushed.
func (h *workhost) keepSessionBranch(ctx context.Context, w *WorkContext) {
	for _, r := range w.Repos {
		dst := w.sessionDir() + "/" + r.Name
		current, err := h.git(ctx, w, false, "-C", dst, "branch", "--show-current")
		if err != nil || strings.TrimSpace(current) == w.Branch {
			continue
		}
		upstream, _ := h.git(ctx, w, false, "-C", dst, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
		if _, err := h.git(ctx, w, false, "-C", dst, "checkout", "-B", w.Branch); err != nil {
			log.Printf("会话 %s 的 %s 回到会话分支失败：%v", w.Session, r.Name, err)
			continue
		}
		if up := strings.TrimSpace(upstream); strings.HasPrefix(up, "origin/") {
			_, _ = h.git(ctx, w, false, "-C", dst, "branch", "--set-upstream-to="+up) // keep pushing where the agent was
		}
		if other := strings.TrimSpace(current); other != "" {
			_, _ = h.git(ctx, w, false, "-C", dst, "branch", "-D", other) // the agent's stray local branch
		}
	}
}

// runTurn executes one turn of a work session inside the colleague's container.
func (h *workhost) runTurn(ctx context.Context, a AgentConfig, t Task, emit Emitter) Outcome {
	w := t.Work
	if err := w.validate(); err != nil {
		return Outcome{Status: "failed", Error: err.Error()}
	}
	setup, cancel := context.WithTimeout(ctx, 10*time.Minute)
	err := h.prepareSession(setup, w)
	var acct WorkAccount
	var lent []byte
	if err == nil {
		acct, lent, err = h.copyAuth(setup, w, a.Kind)
	}
	cleanImages := func() {}
	if err == nil {
		cleanImages, err = h.writeContainerImages(setup, &t)
	}
	defer cleanImages()
	cancel()
	if err != nil {
		return Outcome{Status: "failed", Error: err.Error()}
	}
	// the CLI runs in the container; its pid is kept so a cancel can stop it there too
	pidFile := "/tmp/ahw-" + w.Session + ".pid"
	a.Workdir = ""
	a.Isolation = "full"
	a.Wrap = []string{h.cfg.Docker, "exec", "-i", "-w", w.sessionDir(), w.container(),
		"sh", "-c", `echo $$ > ` + pidFile + `; exec "$0" "$@"`}
	t.Prompt = workPrompt(t) + imageNote(t.ImagePaths)
	t.MCPToken = ""
	done := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			stop, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			_, _ = h.inContainer(stop, w, nil, nil, "sh", "-c",
				`p=$(cat `+pidFile+` 2>/dev/null) && [ -n "$p" ] && { pkill -TERM -P "$p"; kill -TERM "$p"; } || true`)
		case <-done:
		}
	}()
	defer close(done)
	var out Outcome
	switch a.Kind {
	case "codex":
		out = runCodex(ctx, a, t, emit)
	case "claude":
		out = runClaude(ctx, a, t, emit)
	default:
		return Outcome{Status: "failed", Error: "工作服务器暂不支持智能体类型 " + a.Kind}
	}
	after, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	if lent != nil {
		h.saveRefreshed(after, w, acct, lent)
	}
	h.keepSessionBranch(after, w)
	cancel()
	out.Error = redactText(out.Error)
	return out
}

type fileChange struct {
	Path   string `json:"path"`
	Status string `json:"status"`
}

type commitInfo struct {
	SHA     string `json:"sha"`
	Subject string `json:"subject"`
	Author  string `json:"author"`
}

type repoStatus struct {
	Repo     string       `json:"repo"`
	Ready    bool         `json:"ready"` // the session has a worktree of it
	Files    []fileChange `json:"files"`
	Commits  []commitInfo `json:"commits"`  // not on the remote yet
	Unpushed int          `json:"unpushed"` // len(Commits)
	Base     string       `json:"base"`     // the branch the session works on and pushes to (closest remote one HEAD contains)
}

// baseBranch names the remote branch the session works on and pushes to. Git's upstream of the worktree's branch
// records it (set when the worktree or the agent's `checkout -B … origin/X` creates the branch). The upstream holds
// while HEAD's own work since it branched off is no more than the distance to any other remote branch HEAD contains
// (it may have moved on since: the push is then refused as non-fast-forward rather than going anywhere else). An
// upstream the code has left behind (the agent switched branches) is repaired to the closest contained branch.
func (h *workhost) baseBranch(ctx context.Context, w *WorkContext, dst, fallback string) string {
	script := `up=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null)
[ -n "$up" ] && mb=$(git merge-base HEAD "$up") && echo "up $(git rev-list --count "$mb..HEAD") $up"
for b in $(git for-each-ref --merged HEAD --format='%(refname:short)' refs/remotes/origin); do
  [ "$b" = origin/HEAD ] || echo "$(git rev-list --count "$b..HEAD") $b"; done`
	out, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", "cd \""+dst+"\" && "+script)
	if err != nil {
		return fallback
	}
	upstream, best, bestN, upN := "", "", -1, -1
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if rest, ok := strings.CutPrefix(line, "up "); ok {
			var ref string
			if _, err := fmt.Sscan(rest, &upN, &ref); err == nil && strings.HasPrefix(ref, "origin/") && ref != "origin/"+w.Branch {
				upstream = strings.TrimPrefix(ref, "origin/")
			}
			continue
		}
		var n int
		var ref string
		if _, err := fmt.Sscan(line, &n, &ref); err != nil || !strings.HasPrefix(ref, "origin/") {
			continue
		}
		name := strings.TrimPrefix(ref, "origin/")
		if name == w.Branch {
			continue // a session branch pushed by an older version is not where the work goes
		}
		if bestN < 0 || n < bestN || (n == bestN && name == fallback) {
			best, bestN = name, n
		}
	}
	switch {
	case upstream != "" && (bestN < 0 || upN <= bestN):
		return upstream // HEAD is on it, or it moved on since (the push will say so)
	case best != "":
		_, _ = h.git(ctx, w, false, "-C", dst, "branch", "--set-upstream-to=origin/"+best)
		return best
	case upstream != "":
		return upstream
	}
	return fallback
}

// status lists, per repository, uncommitted files and the session's commits.
func (h *workhost) status(ctx context.Context, w *WorkContext) ([]repoStatus, error) {
	if err := w.validate(); err != nil {
		return nil, err
	}
	out := []repoStatus{}
	for _, r := range w.Repos {
		st := repoStatus{Repo: r.Name, Files: []fileChange{}, Commits: []commitInfo{}}
		dst := w.sessionDir() + "/" + r.Name
		if !h.exists(ctx, w, dst+"/.git") {
			out = append(out, st)
			continue
		}
		st.Ready = true
		porcelain, err := h.git(ctx, w, false, "-C", dst, "status", "--porcelain")
		if err != nil {
			return nil, err
		}
		for _, line := range strings.Split(strings.TrimRight(porcelain, "\n"), "\n") {
			if len(line) > 3 {
				st.Files = append(st.Files, fileChange{Status: strings.TrimSpace(line[:2]), Path: line[3:]})
			}
		}
		// the session's commits not on the remote yet, whichever branch it works on
		logOut, err := h.git(ctx, w, false, "-C", dst, "log", "--format=%h%x09%s%x09%an", "HEAD", "--not", "--remotes=origin")
		if err != nil {
			return nil, err
		}
		for _, line := range strings.Split(strings.TrimSpace(logOut), "\n") {
			if parts := strings.SplitN(line, "\t", 3); len(parts) == 3 {
				st.Commits = append(st.Commits, commitInfo{SHA: parts[0], Subject: parts[1], Author: parts[2]})
			}
		}
		st.Base = h.baseBranch(ctx, w, dst, r.Branch)
		st.Unpushed = len(st.Commits)
		out = append(out, st)
	}
	return out, nil
}

type pushResult struct {
	Repo   string `json:"repo"`
	Pushed bool   `json:"pushed"`
	Error  string `json:"error,omitempty"`
	Note   string `json:"note,omitempty"`
	Branch string `json:"branch,omitempty"`
}

// push sends the session branch of each repository (or only the named ones) to its remote.
func (h *workhost) push(ctx context.Context, w *WorkContext, only []string) ([]pushResult, error) {
	statuses, err := h.status(ctx, w)
	if err != nil {
		return nil, err
	}
	want := map[string]bool{}
	for _, name := range only {
		want[name] = true
	}
	results := []pushResult{}
	for _, st := range statuses {
		if len(want) > 0 && !want[st.Repo] {
			continue
		}
		if !st.Ready || st.Unpushed == 0 {
			results = append(results, pushResult{Repo: st.Repo, Note: "没有新的提交"})
			continue
		}
		dst := w.sessionDir() + "/" + st.Repo
		// straight to the branch the session works on; the local branch name is only the worktree's handle
		if _, err := h.git(ctx, w, true, "-C", dst, "push", "origin", "HEAD:refs/heads/"+st.Base); err != nil {
			msg := err.Error()
			if strings.Contains(msg, "non-fast-forward") || strings.Contains(msg, "fetch first") || strings.Contains(msg, "rejected") {
				msg = st.Base + " 上有别人的新提交，先让智能体「拉取最新代码」合并后再推送"
			}
			results = append(results, pushResult{Repo: st.Repo, Branch: st.Base, Error: msg})
			continue
		}
		results = append(results, pushResult{Repo: st.Repo, Branch: st.Base, Pushed: true})
	}
	return results, nil
}

// answerWork handles the hub's work_* requests.
func (c *Connector) answerWork(raw json.RawMessage) {
	var msg struct {
		Type      string       `json:"type"`
		RequestID string       `json:"request_id"`
		Work      *WorkContext `json:"work"`
		Repo      WorkRepo     `json:"repo"`
		Only      []string     `json:"only"`
	}
	if json.Unmarshal(raw, &msg) != nil {
		return
	}
	reply := map[string]any{"type": "rpc_result", "request_id": msg.RequestID}
	var err error
	switch {
	case c.work == nil:
		err = fmt.Errorf("这个连接器没有开启工作服务器模式")
	case msg.Type == "work_admin" || msg.Type == "work_admin_action":
		c.answerWorkAdmin(raw)
		return
	case msg.Work == nil:
		err = fmt.Errorf("缺少工作区信息")
	default:
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		defer cancel()
		switch msg.Type {
		case "work_ensure":
			err = c.work.ensure(ctx, msg.Work)
		case "work_clone":
			err = c.work.clone(ctx, msg.Work, msg.Repo)
		case "work_status":
			var repos []repoStatus
			repos, err = c.work.status(ctx, msg.Work)
			reply["repos"] = repos
		case "work_push":
			var pushed []pushResult
			pushed, err = c.work.push(ctx, msg.Work, msg.Only)
			reply["pushed"] = pushed
		case "work_remove_session":
			err = c.work.removeSession(ctx, msg.Work)
		default:
			err = fmt.Errorf("未知请求 %s", msg.Type)
		}
	}
	if err != nil {
		reply = map[string]any{"type": "rpc_result", "request_id": msg.RequestID, "error": err.Error()}
	}
	c.send(reply)
}

// enqueueWork runs a work session's turn beside the agent's ordinary queue: each colleague may have
// MaxSessions turns running at once, further ones wait for a slot.
func (c *Connector) enqueueWork(t Task) {
	ctx, cancel := context.WithCancel(context.Background())
	c.mu.Lock()
	if _, dup := c.tasks[t.ID]; dup {
		c.mu.Unlock()
		cancel()
		return
	}
	r := &running{ctx: ctx, cancel: cancel, agentKey: t.AgentKey, enqueuedAt: time.Now()}
	c.tasks[t.ID] = r
	a, ok := c.agents[t.AgentKey]
	c.mu.Unlock()
	if !ok {
		c.finish(t, Outcome{Status: "failed", Error: "本机连接器未配置智能体 " + t.AgentKey})
		return
	}
	log.Printf("收到工作会话 %s 第 %d 轮（任务 #%d）→ %s", t.Work.Session, t.Turn, t.ID, t.AgentKey)
	go func() {
		slot := c.work.slot(t.Work.User)
		select {
		case slot <- struct{}{}:
		case <-ctx.Done():
			c.finish(t, Outcome{Status: "failed", Error: "已取消"})
			return
		}
		defer func() { <-slot }()
		c.work.touch(t.Work.User, 1)
		defer c.work.touch(t.Work.User, -1)
		c.mu.Lock()
		r.started = true
		c.mu.Unlock()
		c.send(map[string]any{"type": "task_started", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID})
		c.sendTelemetry()
		emit := func(kind, content string) {
			c.send(map[string]any{"type": "task_event", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
				"kind": kind, "content": content})
		}
		runCtx := context.WithValue(ctx, taskIDKey{}, t.ID)
		if c.hasFeature("events_v2") {
			runCtx = withToolSink(runCtx, func(ev ToolEvent) {
				payload := map[string]any{"call_id": ev.CallID, "name": ev.Name, "phase": ev.Phase, "summary": ev.Summary,
					"observed_at": float64(time.Now().UnixMilli()) / 1000}
				if ev.ExitCode != nil {
					payload["exit_code"] = *ev.ExitCode
				}
				c.send(map[string]any{"type": "task_event", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
					"kind": "tool", "content": strings.TrimSpace(ev.Name + " " + ev.Summary), "payload": payload})
			})
		}
		start := time.Now()
		out := c.work.runTurn(runCtx, a, t, emit)
		log.Printf("工作会话 %s 第 %d 轮结束：%s，耗时 %s", t.Work.Session, t.Turn, out.Status, time.Since(start).Round(time.Second))
		c.finish(t, out)
	}()
	c.sendTelemetry()
}
