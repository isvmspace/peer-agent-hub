package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Task is the payload the hub sends for one turn of a task.
type Task struct {
	ID        int64         `json:"id"`
	Turn      int           `json:"turn"`
	AgentKey  string        `json:"agent_key"`
	Title     string        `json:"title"`
	Prompt    string        `json:"prompt"`
	From      string        `json:"from"`
	Depth     int           `json:"depth"`
	SessionID string        `json:"session_id"`
	History   []HistoryTurn `json:"history"`
	Workspace string        `json:"workspace"`
	// task-scoped token for the hub's MCP tools; revoked when the task finishes
	MCPToken string `json:"mcp_token"`
	MCPURL   string `json:"-"`
	// identifies this attempt; echoed in every report so the hub can drop a stale attempt's late reports
	ExecutionID string `json:"execution_id"`
	// set for a 「工作」session turn: runs in the colleague's container (workhost mode)
	Work *WorkContext `json:"work,omitempty"`
	// images attached to this turn's message, and where they were written for the agent
	Images     []TaskImage `json:"images,omitempty"`
	ImagePaths []string    `json:"-"`
}

// mcpTokenEnv carries the task token to the CLI so it never shows up in argv.
const mcpTokenEnv = "AGENT_HUB_TASK_TOKEN"

type HistoryTurn struct {
	Prompt string `json:"prompt"`
	Result string `json:"result"`
}

// Outcome is what a finished turn reports back.
type Outcome struct {
	Status    string // completed | failed
	Result    string
	Error     string
	SessionID string
	Files     []string          // produced files to upload, see collectFiles
	Usage     Usage             // vendor-reported tokens/cost, nil when the CLI reports none
	CodeRef   map[string]string // git baseline of the working folder after the turn
}

// Emitter receives incremental progress lines from a running agent.
type Emitter func(kind, content string)

const maxResultChars = 20000

func buildPrompt(t Task, withHistory bool) string {
	if t.Work != nil {
		return t.Prompt // a work session carries its own preamble (workPrompt) and resumes by session id
	}
	var b strings.Builder
	fmt.Fprintf(&b, "[Agent Hub 任务 #%d，来自 %s，委派层级 %d]\n", t.ID, t.From, t.Depth)
	fmt.Fprintf(&b, "这是同事通过 Agent Hub 派给你的任务，请直接在本机完成并在最后给出清晰的结果总结。"+
		"如需和其他智能体协作，可直接用 agent-hub MCP 工具（list_agents 找智能体、create_task 派子任务并 wait_seconds 等结果、"+
		"follow_up 追问），子任务会自动关联到本任务（parent_task_id=%d），不需要人工转达；把子任务结果整合进你的最终总结。\n\n", t.ID)
	if withHistory && len(t.History) > 0 {
		b.WriteString("之前的对话：\n")
		for i, h := range t.History {
			fmt.Fprintf(&b, "--- 第 %d 轮 请求 ---\n%s\n--- 第 %d 轮 你的结果 ---\n%s\n", i+1, h.Prompt, i+1, h.Result)
		}
		b.WriteString("\n本轮请求：\n")
	}
	b.WriteString(t.Prompt)
	b.WriteString(imageNote(t.ImagePaths))
	return b.String()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

// runAgent executes one turn and blocks until the CLI exits or ctx is canceled.
func runAgent(ctx context.Context, a AgentConfig, t Task, emit Emitter) Outcome {
	if a.TimeoutMin > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(a.TimeoutMin)*time.Minute)
		defer cancel()
	}
	dir, err := a.resolveWorkdir(t.Workspace)
	if err != nil {
		return Outcome{Status: "failed", Error: err.Error()}
	}
	a.Workdir = dir
	cleanup, err := writeLocalImages(&t)
	defer cleanup()
	if err != nil {
		return Outcome{Status: "failed", Error: err.Error()}
	}
	// every agent's narration and raw output is masked before it leaves this machine
	raw, outputs := emit, &outputRedactor{}
	emit = func(kind, content string) {
		if kind == "output" {
			content = outputs.mask(content)
		}
		raw(kind, content)
	}
	start := time.Now()
	var out Outcome
	switch a.Kind {
	case "claude":
		out = runClaude(ctx, a, t, emit)
	case "codex":
		out = runCodex(ctx, a, t, emit)
	case "hermes":
		out = runHermes(ctx, a, t, emit)
	default:
		out = runCommand(ctx, a, t, emit)
	}
	out.Error = redactText(out.Error) // stderr and CLI error text reach everyone who sees the task
	out.Files = collectFiles(a, dir, out, start)
	out.CodeRef = codeRef(dir)
	return out
}

// agentCmd carries the prompt separately: it is written through our own stdin
// pipe after Start, since exec only copies a Reader-backed Stdin during Wait.
type agentCmd struct {
	*exec.Cmd
	stdin    string
	turnDone chan struct{}
	doneOnce sync.Once
	reaped   atomic.Bool
}

// lingerAfterTurn is how long a CLI may keep running after it reported the
// turn finished (e.g. Codex waiting on a stuck Windows sandbox helper) before
// its process tree is ended and the turn is closed with the answer it gave.
var lingerAfterTurn = 30 * time.Second

func (ac *agentCmd) markTurnDone() { ac.doneOnce.Do(func() { close(ac.turnDone) }) }

func newCmd(ctx context.Context, a AgentConfig, args []string, stdin string) *agentCmd {
	cmd := exec.Command(a.Command, args...)
	if len(a.Wrap) > 0 {
		// e.g. `docker exec … <container>` in workhost mode: the CLI runs inside the colleague's container
		cmd = exec.Command(a.Wrap[0], append(append(append([]string{}, a.Wrap[1:]...), a.Command), args...)...)
	}
	if a.Workdir != "" {
		cmd.Dir = a.Workdir
	}
	cmd.Env = os.Environ()
	for k, v := range a.Env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.Env = append(cmd.Env, "AGENT_HUB_TASK_ID="+fmt.Sprint(ctx.Value(taskIDKey{})))
	if token, _ := ctx.Value(mcpTokenKey{}).(string); token != "" {
		cmd.Env = append(cmd.Env, mcpTokenEnv+"="+token)
	}
	prepareProcess(cmd)
	return &agentCmd{Cmd: cmd, stdin: stdin, turnDone: make(chan struct{})}
}

type taskIDKey struct{}
type mcpTokenKey struct{}

// claudeMCPArgs gives Claude the hub's tools for this task only, alongside the user's own MCP servers.
func claudeMCPArgs(t Task) []string {
	if t.MCPToken == "" {
		return nil
	}
	cfg, _ := json.Marshal(map[string]any{"mcpServers": map[string]any{"agent-hub": map[string]any{
		"type": "http", "url": t.MCPURL, "headers": map[string]string{"Authorization": "Bearer ${" + mcpTokenEnv + "}"},
	}}})
	return []string{"--mcp-config", string(cfg), "--allowedTools", "mcp__agent-hub"}
}

func codexMCPArgs(t Task) []string {
	if t.MCPToken == "" {
		return nil
	}
	return []string{"-c", fmt.Sprintf("mcp_servers.agent-hub.url=%q", t.MCPURL),
		"-c", fmt.Sprintf("mcp_servers.agent-hub.bearer_token_env_var=%q", mcpTokenEnv),
		// exec runs with approval_policy=never, which otherwise refuses every MCP call
		"-c", `mcp_servers.agent-hub.default_tools_approval_mode="approve"`}
}

// streamProcess starts cmd, feeds every stdout line to onLine, and collects stderr.
func streamProcess(ctx context.Context, ac *agentCmd, onLine func(string)) (stderr string, err error) {
	cmd := ac.Cmd
	// Our own pipe plus WaitDelay: background children that inherit stdout can't
	// keep the scanner (and this agent's queue) blocked after the CLI exits.
	pr, pw := io.Pipe()
	cmd.Stdout = pw
	var errBuf limitedBuffer
	cmd.Stderr = &errBuf
	cmd.WaitDelay = 5 * time.Second
	stdinPipe, err := cmd.StdinPipe()
	if err != nil {
		return "", err
	}
	if err := cmd.Start(); err != nil {
		return "", fmt.Errorf("启动 %s 失败: %w", cmd.Path, err)
	}
	go func() {
		_, _ = io.WriteString(stdinPipe, ac.stdin)
		_ = stdinPipe.Close()
	}()
	done := make(chan struct{})
	monitored := make(chan struct{})
	linger := lingerAfterTurn // read once here, never by the monitor after this call returns
	go func() {
		defer close(monitored)
		select {
		case <-ctx.Done():
			killProcessTree(cmd)
		case <-done:
		case <-ac.turnDone:
			select {
			case <-ctx.Done():
				killProcessTree(cmd)
			case <-done:
			case <-time.After(linger):
				ac.reaped.Store(true)
				killProcessTree(cmd)
			}
		}
	}()
	scanned := make(chan struct{})
	go func() {
		defer close(scanned)
		readLines(pr, 16*1024*1024, onLine)
		_, _ = io.Copy(io.Discard, pr)
	}()
	err = cmd.Wait()
	_ = pw.Close()
	<-scanned
	close(done)
	<-monitored // no watcher outlives the process it watched
	cleanupGroup(cmd)
	if errors.Is(err, exec.ErrWaitDelay) || (ac.reaped.Load() && ctx.Err() == nil) {
		err = nil
	}
	if ctx.Err() != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return errBuf.String(), fmt.Errorf("执行超时")
		}
		return errBuf.String(), fmt.Errorf("已取消")
	}
	return errBuf.String(), err
}

// readLines feeds each line to onLine. A line longer than max is skipped rather
// than ending the read, so a huge tool dump cannot swallow the result after it.
func readLines(r io.Reader, max int, onLine func(string)) {
	br := bufio.NewReaderSize(r, 64*1024)
	var line []byte
	skipping := false
	for {
		chunk, err := br.ReadSlice('\n')
		if !skipping {
			line = append(line, chunk...)
			if len(line) > max {
				line, skipping = line[:0], true
			}
		}
		if err == bufio.ErrBufferFull {
			continue
		}
		if !skipping && (len(line) > 0 || err == nil) {
			onLine(strings.TrimRight(string(line), "\r\n"))
		}
		line, skipping = line[:0], false
		if err != nil {
			return
		}
	}
}

func failed(err error, stderr string) Outcome {
	msg := err.Error()
	if s := strings.TrimSpace(stderr); s != "" {
		msg += "\n" + truncate(redactText(s), 4000) // mask first: trimming can cut a value off its key
	}
	return Outcome{Status: "failed", Error: msg}
}

// ---- Claude Code --------------------------------------------------------

// claudeStartupTimeout bounds the wait for Claude's first reply. A request that
// stalls on the network (e.g. a proxy dropping the connection) otherwise hangs
// forever; before the first reply no tool has run, so restarting is safe.
var claudeStartupTimeout = 4 * time.Minute

// claudeInitTimeout bounds the wait for any output at all. A CLI that has not even
// printed its init event is stuck in its own startup (seen with startup network
// calls through a proxy), so it is restarted much sooner than a slow first reply.
var claudeInitTimeout = 90 * time.Second

const claudeMaxAttempts = 3

// folderIssue explains a startup hang caused by blocked folders; swapped out in tests
// so they never depend on this machine's privacy settings.
var folderIssue = blockedFolders

func runClaude(ctx context.Context, a AgentConfig, t Task, emit Emitter) Outcome {
	// read the limits once: the watchdog goroutine must not see them change mid-run
	initTimeout, replyTimeout := claudeInitTimeout, claudeStartupTimeout
	for attempt := 1; ; attempt++ {
		runCtx, cancel := context.WithCancel(ctx)
		started, replied := make(chan struct{}), make(chan struct{})
		var startOnce, replyOnce sync.Once
		var stalledIn atomic.Value // "" | "init" | "reply"
		var gotReply atomic.Bool
		watchdog := make(chan struct{})
		go func() {
			defer close(watchdog)
			select {
			case <-started:
			case <-replied:
				return
			case <-runCtx.Done():
				return
			case <-time.After(initTimeout):
				stalledIn.Store("init")
				cancel()
				return
			}
			select {
			case <-replied:
			case <-runCtx.Done():
			case <-time.After(replyTimeout):
				stalledIn.Store("reply")
				cancel()
			}
		}()
		out, stderr := runClaudeOnce(runCtx, a, t, emit,
			func() { startOnce.Do(func() { close(started) }) },
			func() { gotReply.Store(true); replyOnce.Do(func() { close(replied) }) })
		replyOnce.Do(func() { close(replied) })
		cancel()
		<-watchdog
		stage, _ := stalledIn.Load().(string)
		// the watchdog can fire in the same instant a reply arrives; once Claude replied, never rerun the task
		if stage == "" || gotReply.Load() || ctx.Err() != nil {
			return out
		}
		msg := fmt.Sprintf("Claude 启动后 %s 没有任何回复（多半是网络或代理卡住）", replyTimeout)
		advice := "请检查本机网络后重新发送"
		if stage == "init" {
			msg = fmt.Sprintf("Claude 命令行 %s 内没有完成初始化（卡在自身启动阶段）", initTimeout)
			advice = "请按上面的提示处理后重新发送"
			if issue := folderIssue(); issue != "" {
				msg += "。可能原因：" + issue
			} else {
				msg += "，可用 claude --debug-file 在本机排查启动卡在哪一步"
			}
		}
		if tail := strings.TrimSpace(stderr); tail != "" {
			msg += "\nstderr 末尾：" + truncate(redactText(tail), 800)
		}
		if attempt >= claudeMaxAttempts {
			return Outcome{Status: "failed", Error: msg + fmt.Sprintf("\n已重试 %d 次仍然如此，%s", attempt-1, advice), SessionID: t.SessionID}
		}
		emit("output", msg+"，已自动重试")
	}
}

func runClaudeOnce(ctx context.Context, a AgentConfig, t Task, emit Emitter, started, replied func()) (Outcome, string) {
	args := append([]string{"-p", "--output-format", "stream-json", "--verbose"}, claudePermissionArgs(a)...)
	args = append(args, claudeMCPArgs(t)...)
	if t.SessionID != "" {
		args = append(args, "--resume", t.SessionID)
	}
	args = append(args, a.Args...)
	cmd := newCmd(ctx, a, args, buildPrompt(t, t.SessionID == ""))
	var out Outcome
	gotResult := false
	tools := newToolReporter(ctx, emit)
	model := ""
	stderr, err := streamProcess(ctx, cmd, func(line string) {
		started()
		var ev struct {
			Type         string   `json:"type"`
			Subtype      string   `json:"subtype"`
			SessionID    string   `json:"session_id"`
			Model        string   `json:"model"`
			Result       string   `json:"result"`
			IsError      bool     `json:"is_error"`
			TotalCostUSD *float64 `json:"total_cost_usd"`
			DurationMS   *int64   `json:"duration_ms"`
			Usage        *struct {
				InputTokens         int64 `json:"input_tokens"`
				OutputTokens        int64 `json:"output_tokens"`
				CacheReadTokens     int64 `json:"cache_read_input_tokens"`
				CacheCreationTokens int64 `json:"cache_creation_input_tokens"`
			} `json:"usage"`
			Message struct {
				Content []struct {
					Type      string          `json:"type"`
					Text      string          `json:"text"`
					ID        string          `json:"id"`
					Name      string          `json:"name"`
					Input     json.RawMessage `json:"input"`
					ToolUseID string          `json:"tool_use_id"`
					IsError   bool            `json:"is_error"`
					Content   json.RawMessage `json:"content"`
				} `json:"content"`
			} `json:"message"`
		}
		if json.Unmarshal([]byte(line), &ev) != nil {
			emit("output", line)
			return
		}
		if ev.SessionID != "" {
			out.SessionID = ev.SessionID
		}
		switch ev.Type {
		case "system":
			if ev.Model != "" {
				model = ev.Model
			}
		case "assistant":
			replied()
			for _, c := range ev.Message.Content {
				switch c.Type {
				case "text":
					if strings.TrimSpace(c.Text) != "" {
						emit("output", c.Text)
					}
				case "tool_use":
					tools.started(c.ID, c.Name, string(c.Input), true)
				}
			}
		case "user":
			for _, c := range ev.Message.Content {
				if c.Type == "tool_result" {
					tools.finished(c.ToolUseID, "", c.IsError, toolResultText(c.Content), nil, "")
				}
			}
		case "result":
			replied()
			cmd.markTurnDone()
			gotResult = true
			out.Result = ev.Result
			if ev.Usage != nil {
				out.Usage = Usage{"input_tokens": ev.Usage.InputTokens, "output_tokens": ev.Usage.OutputTokens,
					"cache_read_tokens": ev.Usage.CacheReadTokens, "cache_creation_tokens": ev.Usage.CacheCreationTokens}
				if ev.TotalCostUSD != nil {
					out.Usage["cost_usd"] = *ev.TotalCostUSD
				}
				if ev.DurationMS != nil {
					out.Usage["duration_ms"] = *ev.DurationMS
				}
				if model != "" {
					out.Usage["model"] = model
				}
			}
			if ev.IsError {
				out.Status, out.Error = "failed", "Claude 返回错误："+strings.TrimSpace(ev.Result+" ("+ev.Subtype+")")
			} else {
				out.Status = "completed"
			}
		}
	})
	if err != nil && !gotResult {
		o := failed(err, stderr)
		o.SessionID = out.SessionID
		return o, stderr
	}
	if !gotResult {
		return Outcome{Status: "failed", Error: "Claude 未返回结果\n" + truncate(redactText(stderr), 4000), SessionID: out.SessionID}, stderr
	}
	return out, stderr
}

// ---- Codex --------------------------------------------------------------

// claudePermissionArgs: in workspace isolation, file edits are auto-accepted only
// inside the working folder, and Bash runs in Claude's OS sandbox (macOS/Linux)
// that blocks writes elsewhere. Windows has no sandbox, so only edits are confined.
func claudePermissionArgs(a AgentConfig) []string {
	switch a.isolationLevel() {
	case "none":
		return []string{"--dangerously-skip-permissions"}
	case "limited":
		return []string{"--permission-mode", "acceptEdits", "--allowedTools", "Bash", "PowerShell"}
	}
	domains := a.SandboxDomains
	if len(domains) == 0 {
		domains = []string{"*"}
	}
	settings, _ := json.Marshal(map[string]any{"sandbox": map[string]any{
		"enabled": true, "autoAllowBashIfSandboxed": true, "allowUnsandboxedCommands": false,
		"network": map[string]any{"allowedDomains": domains},
	}})
	return []string{"--permission-mode", "acceptEdits", "--settings", string(settings)}
}

// codexPermissionArgs: workspace-write sandbox (writes only in the working folder,
// network allowed). Passed via -c so it also applies to `exec resume`.
func codexPermissionArgs(a AgentConfig) []string {
	if a.isolationLevel() == "none" {
		return []string{"--dangerously-bypass-approvals-and-sandbox"}
	}
	return []string{"-c", `sandbox_mode="workspace-write"`, "-c", `approval_policy="never"`,
		"-c", "sandbox_workspace_write.network_access=true"}
}

func runCodex(ctx context.Context, a AgentConfig, t Task, emit Emitter) Outcome {
	common := append(codexImageArgs(t), "--json", "--skip-git-repo-check")
	common = append(common, codexPermissionArgs(a)...)
	common = append(common, codexMCPArgs(t)...)
	common = append(common, a.Args...)
	var args []string
	if t.SessionID != "" {
		args = append([]string{"exec", "resume"}, common...)
		args = append(args, t.SessionID, "-")
	} else {
		args = append([]string{"exec"}, common...)
		args = append(args, "-")
	}
	cmd := newCmd(ctx, a, args, buildPrompt(t, t.SessionID == ""))
	var out Outcome
	var lastMessage, turnError string
	tools := newToolReporter(ctx, emit)
	stderr, err := streamProcess(ctx, cmd, func(line string) {
		var ev struct {
			Type     string `json:"type"`
			ThreadID string `json:"thread_id"`
			Message  string `json:"message"`
			Error    struct {
				Message string `json:"message"`
			} `json:"error"`
			Usage *struct {
				InputTokens       int64 `json:"input_tokens"`
				CachedInputTokens int64 `json:"cached_input_tokens"`
				OutputTokens      int64 `json:"output_tokens"`
			} `json:"usage"`
			Item struct {
				ID               string `json:"id"`
				Type             string `json:"type"`
				Text             string `json:"text"`
				Command          string `json:"command"`
				AggregatedOutput string `json:"aggregated_output"`
				ExitCode         *int   `json:"exit_code"`
				Status           string `json:"status"`
				Server           string `json:"server"`
				Tool             string `json:"tool"`
				Query            string `json:"query"`
				Changes          []struct {
					Path string `json:"path"`
					Kind string `json:"kind"`
				} `json:"changes"`
			} `json:"item"`
		}
		if json.Unmarshal([]byte(line), &ev) != nil {
			emit("output", line)
			return
		}
		switch ev.Type {
		case "thread.started":
			out.SessionID = ev.ThreadID
		case "item.started":
			if name, summary := codexTool(ev.Item.Type, ev.Item.Command, ev.Item.Server, ev.Item.Tool, ev.Item.Query, len(ev.Item.Changes)); name != "" {
				tools.started(ev.Item.ID, name, summary, false)
			}
		case "item.completed":
			switch ev.Item.Type {
			case "agent_message":
				lastMessage = ev.Item.Text
				emit("output", ev.Item.Text)
			case "command_execution":
				code := "?"
				if ev.Item.ExitCode != nil {
					code = fmt.Sprint(*ev.Item.ExitCode)
				}
				failed := ev.Item.Status == "failed" || (ev.Item.ExitCode != nil && *ev.Item.ExitCode != 0)
				tools.finished(ev.Item.ID, "Shell", failed, ev.Item.AggregatedOutput, ev.Item.ExitCode,
					fmt.Sprintf("$ %s  (exit %s)\n%s", ev.Item.Command, code, truncate(redactText(ev.Item.AggregatedOutput), 2000)))
			case "mcp_tool_call", "file_change", "web_search":
				name, summary := codexTool(ev.Item.Type, ev.Item.Command, ev.Item.Server, ev.Item.Tool, ev.Item.Query, len(ev.Item.Changes))
				tools.finished(ev.Item.ID, name, ev.Item.Status == "failed", summary, nil, "")
			case "reasoning":
				// reasoning summaries are noisy; skip
			default:
				if ev.Item.Text != "" {
					emit("output", ev.Item.Text)
				}
			}
		case "turn.completed":
			// transient "error" events (e.g. "Reconnecting... 2/5") don't fail a turn that completed
			turnError = ""
			if ev.Usage != nil {
				out.Usage = Usage{"input_tokens": ev.Usage.InputTokens, "cache_read_tokens": ev.Usage.CachedInputTokens,
					"output_tokens": ev.Usage.OutputTokens}
			}
			cmd.markTurnDone()
		case "turn.failed":
			cmd.markTurnDone()
			turnError = ev.Error.Message
		case "error":
			turnError = ev.Message
			emit("output", "⚠ "+ev.Message)
		}
	})
	if t.SessionID != "" && out.SessionID == "" {
		out.SessionID = t.SessionID
	}
	if turnError != "" {
		return Outcome{Status: "failed", Error: turnError, Result: lastMessage, SessionID: out.SessionID}
	}
	if err != nil {
		o := failed(err, stderr)
		o.SessionID, o.Result = out.SessionID, lastMessage
		return o
	}
	out.Status, out.Result = "completed", lastMessage
	return out
}

// codexTool names a Codex item for the timeline; empty for items that are not tool calls.
func codexTool(kind, command, server, tool, query string, changes int) (string, string) {
	switch kind {
	case "command_execution":
		return "Shell", command
	case "mcp_tool_call":
		return strings.Trim(server+"."+tool, "."), ""
	case "file_change":
		return "Edit", fmt.Sprintf("%d 个文件", changes)
	case "web_search":
		return "WebSearch", query
	}
	return "", ""
}

// toolResultText flattens a Claude tool_result content (string or blocks) to one short line.
func toolResultText(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return strings.TrimSpace(s) // the reporter masks, then trims
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &blocks) == nil {
		var b strings.Builder
		for _, x := range blocks {
			if x.Type == "text" {
				b.WriteString(x.Text)
			}
		}
		return strings.TrimSpace(b.String())
	}
	return ""
}

// ---- Hermes -------------------------------------------------------------

func runHermes(ctx context.Context, a AgentConfig, t Task, emit Emitter) Outcome {
	streams := hermesStreams(ctx, a)
	if ctx.Err() != nil { // canceled while probing: don't start the turn
		return Outcome{Status: "failed", Error: "已取消"}
	}
	if streams {
		return runHermesStream(ctx, a, t, emit)
	}
	// Long multi-line prompts are unsafe on a command line (Windows .cmd shims,
	// 8191-char limit), so hand Hermes a file to read instead.
	file, err := writePromptFile(t.ID, buildPrompt(t, true))
	if err != nil {
		return failed(err, "")
	}
	defer os.Remove(file)
	var args []string
	if a.isolationLevel() == "none" {
		args = append(args, "--yolo") // otherwise dangerous commands are refused in one-shot mode
	}
	args = append(args, a.Args...)
	args = append(args, "-z", fmt.Sprintf("请读取 UTF-8 文本文件 %s ，其中是同事派给你的完整任务说明，按说明执行并在最后给出结果总结。", file))
	return runPlain(ctx, a, args, "", emit)
}

var hermesStreamSupport sync.Map // command path -> bool

// hermesStreams reports whether this Hermes can stream progress as JSONL
// (`chat --format stream-json`); older installs only print the final answer.
func hermesStreams(ctx context.Context, a AgentConfig) bool {
	if v, ok := hermesStreamSupport.Load(a.Command); ok {
		return v.(bool)
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second) // a canceled task stops the probe too
	defer cancel()
	cmd := exec.CommandContext(ctx, a.Command, "chat", "--help")
	prepareProcess(cmd)
	// on cancel end the whole tree and stop waiting for children still holding the pipe
	cmd.Cancel = func() error { killProcessTree(cmd); return nil }
	cmd.WaitDelay = time.Second
	out, _ := cmd.CombinedOutput()
	ok := strings.Contains(string(out), "stream-json") && strings.Contains(string(out), "--query-file")
	if ctx.Err() != nil {
		return ok // interrupted: probe again next time
	}
	hermesStreamSupport.Store(a.Command, ok)
	return ok
}

// runHermesStream shows each tool call while Hermes works instead of staying
// silent until the final answer.
func runHermesStream(ctx context.Context, a AgentConfig, t Task, emit Emitter) Outcome {
	file, err := writePromptFile(t.ID, buildPrompt(t, true))
	if err != nil {
		return failed(err, "")
	}
	defer os.Remove(file)
	var args []string
	if a.isolationLevel() == "none" {
		args = append(args, "--yolo")
	}
	args = append(args, a.Args...)
	args = append(args, "chat", "--query-file", file, "--format", "stream-json")
	cmd := newCmd(ctx, a, args, "")
	tools := newToolReporter(ctx, emit)
	var out Outcome
	var text, pending strings.Builder // all answer text / text not yet shown
	shown := false
	flushText := func() {
		if p := strings.TrimSpace(pending.String()); p != "" {
			emit("output", p) // runAgent masks it, keeping track of a key split across chunks
			shown = true
		}
		pending.Reset()
	}
	var resultText, resultErr string
	gotResult := false
	stderr, err := streamProcess(ctx, cmd, func(line string) {
		var ev struct {
			Type       string          `json:"type"`
			Name       string          `json:"name"`
			ToolCallID string          `json:"tool_call_id"`
			Input      json.RawMessage `json:"input"`
			Output     string          `json:"output"`
			IsError    bool            `json:"is_error"`
			Text       string          `json:"text"`
			SessionID  string          `json:"session_id"`
			ExitCode   int             `json:"exit_code"`
			Error      string          `json:"error"`
			Tokens     *struct {
				Input     int64 `json:"input"`
				Output    int64 `json:"output"`
				CacheRead int64 `json:"cache_read"`
			} `json:"tokens"`
		}
		if json.Unmarshal([]byte(line), &ev) != nil || ev.Type == "" {
			if strings.TrimSpace(line) != "" {
				emit("output", line) // masked by runAgent, which also hides a key split across lines
			}
			return
		}
		switch ev.Type {
		case "system":
			if ev.SessionID != "" {
				out.SessionID = ev.SessionID
			}
		case "text":
			text.WriteString(ev.Text)
			pending.WriteString(ev.Text)
		case "tool_use":
			flushText() // narration before a tool call ("先拉代码…") shows up as it happens
			id := ev.ToolCallID
			if id == "" {
				id = ev.Name
			}
			tools.started(id, ev.Name, hermesToolSummary(ev.Input), true)
		case "tool_result":
			id := ev.ToolCallID
			if id == "" {
				id = ev.Name
			}
			tools.finished(id, ev.Name, ev.IsError, hermesToolOutput(ev.Output), nil, "")
		case "result":
			gotResult = true
			resultText, resultErr = ev.Text, ev.Error
			if ev.SessionID != "" {
				out.SessionID = ev.SessionID
			}
			if ev.ExitCode != 0 && resultErr == "" {
				resultErr = fmt.Sprintf("Hermes 退出码 %d", ev.ExitCode)
			}
			if ev.Tokens != nil {
				out.Usage = Usage{"input_tokens": ev.Tokens.Input, "output_tokens": ev.Tokens.Output,
					"cache_read_tokens": ev.Tokens.CacheRead}
			}
			cmd.markTurnDone()
		}
	})
	answer := strings.TrimSpace(resultText)
	if answer == "" {
		answer = strings.TrimSpace(text.String())
	}
	if shown {
		flushText()
	} else if answer != "" {
		emit("output", answer) // whole: masked before any trimming
	}
	answer = truncate(answer, maxResultChars)
	if resultErr != "" {
		return Outcome{Status: "failed", Error: resultErr, Result: answer, SessionID: out.SessionID}
	}
	if err != nil && !gotResult {
		o := failed(err, stderr)
		o.Result, o.SessionID = answer, out.SessionID
		return o
	}
	if !gotResult {
		o := failed(fmt.Errorf("Hermes 没有返回结束事件，无法确认任务完成"), stderr)
		o.Result, o.SessionID = answer, out.SessionID
		return o
	}
	out.Status, out.Result = "completed", answer
	return out
}

// hermesToolOutput unwraps the terminal tool's {"output": …} envelope.
func hermesToolOutput(s string) string {
	var m struct {
		Output *string `json:"output"`
		Error  *string `json:"error"`
	}
	if json.Unmarshal([]byte(s), &m) != nil || m.Output == nil {
		return s
	}
	if m.Error != nil && *m.Error != "" {
		return strings.TrimSpace(*m.Output + "\n" + *m.Error)
	}
	return *m.Output
}

// hermesToolSummary picks the readable part of a tool's arguments.
func hermesToolSummary(raw json.RawMessage) string {
	var m map[string]any
	if json.Unmarshal(raw, &m) != nil {
		return strings.TrimSpace(string(raw)) // full text: the reporter judges secrets before trimming
	}
	for _, k := range []string{"command", "cmd", "path", "file_path", "url", "query", "pattern", "goal", "task", "name"} {
		if v, ok := m[k].(string); ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	b, _ := json.Marshal(m)
	return string(b)
}

func writePromptFile(id int64, prompt string) (string, error) {
	f, err := os.CreateTemp("", fmt.Sprintf("agent-hub-task-%d-*.md", id))
	if err != nil {
		return "", err
	}
	defer f.Close()
	_, err = f.WriteString(prompt)
	return f.Name(), err
}

// ---- generic command ----------------------------------------------------

func runCommand(ctx context.Context, a AgentConfig, t Task, emit Emitter) Outcome {
	prompt := buildPrompt(t, true)
	var args []string
	usesPlaceholder := false
	for _, arg := range a.Args {
		if strings.Contains(arg, "{prompt_file}") {
			file, err := writePromptFile(t.ID, prompt)
			if err != nil {
				return failed(err, "")
			}
			defer os.Remove(file)
			usesPlaceholder = true
			arg = strings.ReplaceAll(arg, "{prompt_file}", file)
		}
		if strings.Contains(arg, "{prompt}") {
			usesPlaceholder = true
			arg = strings.ReplaceAll(arg, "{prompt}", prompt)
		}
		args = append(args, arg)
	}
	stdin := prompt
	if usesPlaceholder {
		stdin = ""
	}
	return runPlain(ctx, a, args, stdin, emit)
}

func runPlain(ctx context.Context, a AgentConfig, args []string, stdin string, emit Emitter) Outcome {
	cmd := newCmd(ctx, a, args, stdin)
	var all strings.Builder
	batch := newLineBatcher(func(s string) { emit("output", s) })
	stderr, err := streamProcess(ctx, cmd, func(line string) {
		if all.Len() < 4*maxResultChars {
			all.WriteString(line)
			all.WriteByte('\n')
		}
		batch.add(line)
	})
	batch.flush()
	result := truncate(strings.TrimSpace(all.String()), maxResultChars)
	if err != nil {
		o := failed(err, stderr)
		o.Result = result
		return o
	}
	return Outcome{Status: "completed", Result: result}
}

// lineBatcher groups raw output lines so chatty CLIs don't flood the hub.
type lineBatcher struct {
	mu    sync.Mutex
	lines []string
	last  time.Time
	out   func(string)
}

func newLineBatcher(out func(string)) *lineBatcher {
	return &lineBatcher{out: out, last: time.Now()}
}

func (b *lineBatcher) add(line string) {
	b.mu.Lock()
	b.lines = append(b.lines, line)
	ready := len(b.lines) >= 50 || time.Since(b.last) > time.Second
	b.mu.Unlock()
	if ready {
		b.flush()
	}
}

func (b *lineBatcher) flush() {
	b.mu.Lock()
	lines := b.lines
	b.lines = nil
	b.last = time.Now()
	b.mu.Unlock()
	if len(lines) > 0 {
		b.out(strings.Join(lines, "\n"))
	}
}

// limitedBuffer keeps the tail of stderr without unbounded growth.
type limitedBuffer struct {
	mu      sync.Mutex
	buf     []byte
	dropped bool
}

func (l *limitedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.buf = append(l.buf, p...)
	if len(l.buf) > 64*1024 {
		l.buf = l.buf[len(l.buf)-64*1024:]
		l.dropped = true
	}
	return len(p), nil
}

func (l *limitedBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.dropped { // the first line lost its start (maybe a key name): drop it rather than show a bare value
		if i := strings.IndexByte(string(l.buf), '\n'); i >= 0 {
			return "…\n" + string(l.buf[i+1:])
		}
		return "…"
	}
	return string(l.buf)
}
