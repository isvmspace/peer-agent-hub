//go:build !windows

package main

import (
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fakeCLI(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "fake")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func collect() (Emitter, *[]string) {
	var lines []string
	return func(kind, content string) { lines = append(lines, content) }, &lines
}

func TestClaudeStreamParsing(t *testing.T) {
	args := filepath.Join(t.TempDir(), "args")
	cli := fakeCLI(t, `echo "$@" > `+args+`
cat > /dev/null
echo '{"type":"system","subtype":"init","session_id":"sess-9"}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"正在打包"},{"type":"tool_use","name":"Bash","input":{"command":"npm run build"}}]}}'
echo '{"type":"result","subtype":"success","is_error":false,"result":"打包完成","session_id":"sess-9"}'
`)
	emit, lines := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "claude", Command: cli}, Task{ID: 1, Prompt: "build", SessionID: "old"}, emit)
	if out.Status != "completed" || out.Result != "打包完成" || out.SessionID != "sess-9" {
		t.Fatalf("unexpected outcome %+v", out)
	}
	if len(*lines) != 2 || !strings.Contains((*lines)[1], "npm run build") {
		t.Fatalf("unexpected lines %q", *lines)
	}
	got, _ := os.ReadFile(args)
	if !strings.Contains(string(got), "--resume old") || !strings.Contains(string(got), `"sandbox":{`) ||
		strings.Contains(string(got), "--dangerously-skip-permissions") {
		t.Fatalf("missing flags: %s", got)
	}
}

func TestCodexStreamParsingAndResume(t *testing.T) {
	args := filepath.Join(t.TempDir(), "args")
	stdin := filepath.Join(t.TempDir(), "stdin")
	cli := fakeCLI(t, `echo "$@" > `+args+`
cat > `+stdin+`
echo '{"type":"thread.started","thread_id":"th-1"}'
echo '{"type":"item.completed","item":{"type":"command_execution","command":"ls","aggregated_output":"a","exit_code":0}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
`)
	emit, _ := collect()
	task := Task{ID: 7, Turn: 2, Prompt: "继续", SessionID: "th-1", History: []HistoryTurn{{Prompt: "p", Result: "r"}}}
	out := runAgent(context.Background(), AgentConfig{Kind: "codex", Command: cli}, task, emit)
	if out.Status != "completed" || out.Result != "done" || out.SessionID != "th-1" {
		t.Fatalf("unexpected outcome %+v", out)
	}
	got, _ := os.ReadFile(args)
	if !strings.HasPrefix(string(got), "exec resume --json") || !strings.Contains(string(got), "th-1 -") {
		t.Fatalf("bad args: %s", got)
	}
	prompt, _ := os.ReadFile(stdin)
	if strings.Contains(string(prompt), "之前的对话") {
		t.Fatalf("resumed session should not resend history: %s", prompt)
	}
}

func TestCodexTurnFailed(t *testing.T) {
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"turn.failed","error":{"message":"quota"}}'
exit 1
`)
	emit, _ := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "codex", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "failed" || out.Error != "quota" {
		t.Fatalf("unexpected outcome %+v", out)
	}
}

func TestCommandWithHistoryAndCancel(t *testing.T) {
	cli := fakeCLI(t, "cat; echo END; sleep 30 & wait")
	emit, lines := collect()
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(2 * time.Second); cancel() }()
	start := time.Now()
	task := Task{ID: 3, Prompt: "现在", History: []HistoryTurn{{Prompt: "之前", Result: "好的"}}}
	out := runAgent(ctx, AgentConfig{Kind: "command", Command: cli}, task, emit)
	if time.Since(start) > 8*time.Second {
		t.Fatal("cancel did not kill the process tree")
	}
	if out.Status != "failed" || !strings.Contains(out.Error, "已取消") {
		t.Fatalf("unexpected outcome %+v", out)
	}
	if !strings.Contains(strings.Join(*lines, "\n"), "之前的对话") {
		t.Fatalf("history missing from prompt: %q out=%+v", *lines, out)
	}
}

func TestWsURL(t *testing.T) {
	cases := map[string]string{
		"http://10.0.0.51:8790":    "ws://10.0.0.51:8790/ws/connector",
		"https://hub.local/agent/": "wss://hub.local/agent/ws/connector",
	}
	for in, want := range cases {
		if got, _ := wsURL(in); got != want {
			t.Errorf("wsURL(%q)=%q want %q", in, got, want)
		}
	}
}

func TestCancelKillsDetachedGrandchildren(t *testing.T) {
	marker := "sleep 37"
	// the grandchild escapes the process group the way Codex tool calls do
	cli := fakeCLI(t, "cat >/dev/null; perl -e 'setpgrp(0,0); exec \"sleep\", \"37\"' & wait")
	emit, _ := collect()
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(2 * time.Second); cancel() }()
	runAgent(ctx, AgentConfig{Kind: "command", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	time.Sleep(300 * time.Millisecond)
	if out, _ := exec.Command("pgrep", "-f", marker).Output(); len(strings.TrimSpace(string(out))) > 0 {
		_ = exec.Command("pkill", "-f", marker).Run()
		t.Fatalf("detached grandchild survived cancel: %s", out)
	}
}

func TestLeftoverBackgroundChildDoesNotHang(t *testing.T) {
	cli := fakeCLI(t, "cat >/dev/null; perl -e 'setpgrp(0,0); exec \"sleep\", \"41\"' & echo hi")
	emit, _ := collect()
	start := time.Now()
	out := runAgent(context.Background(), AgentConfig{Kind: "command", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	_ = exec.Command("pkill", "-f", "sleep 41").Run()
	if time.Since(start) > 15*time.Second {
		t.Fatalf("worker blocked by leftover child: %v", time.Since(start))
	}
	if out.Status != "completed" || out.Result != "hi" {
		t.Fatalf("unexpected outcome %+v", out)
	}
}

func TestResolveWorkdir(t *testing.T) {
	dir := t.TempDir()
	ws, rejected := cleanWorkspaces([]Workspace{{Path: dir}, {Path: dir}, {Path: "relative"}, {Path: dir + "/missing"}})
	if len(ws) != 1 || ws[0].Name == "" || len(rejected) != 2 {
		t.Fatalf("clean = %v rejected = %v", ws, rejected)
	}
	a := AgentConfig{Key: "x", Workdir: "/default", Workspaces: ws}
	if got, err := a.resolveWorkdir(""); err != nil || got != "/default" {
		t.Fatalf("default = %q %v", got, err)
	}
	if got, err := a.resolveWorkdir(dir); err != nil || got != dir {
		t.Fatalf("workspace = %q %v", got, err)
	}
	if _, err := a.resolveWorkdir("/etc"); err == nil {
		t.Fatal("unauthorized workspace accepted")
	}
}

func TestIsolationArgs(t *testing.T) {
	full := AgentConfig{Kind: "claude", Isolation: "full"}
	if got := claudePermissionArgs(full); got[0] != "--dangerously-skip-permissions" {
		t.Fatalf("full claude = %v", got)
	}
	cx := codexPermissionArgs(AgentConfig{Kind: "codex"})
	if strings.Join(cx, " ") != `-c sandbox_mode="workspace-write" -c approval_policy="never" -c sandbox_workspace_write.network_access=true` {
		t.Fatalf("codex = %v", cx)
	}
	if (AgentConfig{Kind: "command"}).isolationLevel() != "none" || (AgentConfig{Kind: "hermes"}).isolationLevel() != "limited" {
		t.Fatal("unexpected isolation levels")
	}
}

func TestClaudeRetriesWhenStartupStalls(t *testing.T) {
	oldIssue := folderIssue
	folderIssue = func() string { return "测试用的目录提示" }
	defer func() { folderIssue = oldIssue }()
	old := claudeStartupTimeout
	claudeStartupTimeout = 2 * time.Second
	defer func() { claudeStartupTimeout = old }()
	marker := filepath.Join(t.TempDir(), "started")
	// first launch hangs before any reply (like a stalled API request), the retry answers
	cli := fakeCLI(t, `cat > /dev/null
if [ ! -f `+marker+` ]; then touch `+marker+`; echo '{"type":"system","subtype":"init","session_id":"s1"}'; sleep 30; fi
echo '{"type":"result","subtype":"success","is_error":false,"result":"pong","session_id":"s1"}'
`)
	emit, lines := collect()
	start := time.Now()
	out := runAgent(context.Background(), AgentConfig{Kind: "claude", Command: cli}, Task{ID: 1, Prompt: "ping"}, emit)
	if out.Status != "completed" || out.Result != "pong" || time.Since(start) > 10*time.Second {
		t.Fatalf("unexpected outcome %+v after %s lines=%q", out, time.Since(start), *lines)
	}
	if len(*lines) != 1 || !strings.Contains((*lines)[0], "已自动重试") {
		t.Fatalf("unexpected lines %q", *lines)
	}

	// hanging every time gives up after the last attempt, with the stage and stderr in the error
	hang := fakeCLI(t, "cat > /dev/null\necho 'syncing skills' >&2\nsleep 30\n")
	oldInit := claudeInitTimeout
	claudeInitTimeout = time.Second
	defer func() { claudeInitTimeout = oldInit }()
	start = time.Now()
	out = runAgent(context.Background(), AgentConfig{Kind: "claude", Command: hang}, Task{ID: 2, Prompt: "ping", SessionID: "keep"}, emit)
	if out.Status != "failed" || !strings.Contains(out.Error, "没有完成初始化") || !strings.Contains(out.Error, "syncing skills") ||
		!strings.Contains(out.Error, "已重试 2 次") || !strings.Contains(out.Error, "测试用的目录提示") ||
		strings.Contains(out.Error, "检查本机网络") || out.SessionID != "keep" || time.Since(start) > 12*time.Second {
		t.Fatalf("unexpected outcome %+v after %s", out, time.Since(start))
	}
}

func TestLingeringCLIIsReapedAfterTurnCompletes(t *testing.T) {
	old := lingerAfterTurn
	lingerAfterTurn = time.Second
	defer func() { lingerAfterTurn = old }()
	// answers, reports the turn complete, then never exits (like a stuck sandbox helper)
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"thread.started","thread_id":"th-1"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"做完了"}}'
echo '{"type":"turn.completed"}'
sleep 60
`)
	emit, _ := collect()
	start := time.Now()
	out := runAgent(context.Background(), AgentConfig{Kind: "codex", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "completed" || out.Result != "做完了" || out.SessionID != "th-1" || time.Since(start) > 15*time.Second {
		t.Fatalf("unexpected outcome %+v after %s", out, time.Since(start))
	}
}

func TestClaudeErrorShowsReason(t *testing.T) {
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"result","subtype":"success","is_error":true,"result":"Not logged in · Please run /login"}'
`)
	emit, _ := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "claude", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "failed" || !strings.Contains(out.Error, "Not logged in") {
		t.Fatalf("unexpected outcome %+v", out)
	}
}

func TestCodexRecoveredReconnectIsNotAFailure(t *testing.T) {
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"thread.started","thread_id":"th-2"}'
echo '{"type":"error","message":"Reconnecting... 5/5 (request timed out)"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"pong"}}'
echo '{"type":"turn.completed"}'
`)
	emit, lines := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "codex", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "completed" || out.Result != "pong" || !strings.Contains(strings.Join(*lines, "\n"), "Reconnecting") {
		t.Fatalf("unexpected outcome %+v %q", out, *lines)
	}
}

func TestLocalIPsPutsHubRouteFirst(t *testing.T) {
	ips := localIPs("http://127.0.0.1:8790")
	for _, ip := range ips {
		if strings.HasPrefix(ip, "127.") {
			t.Fatalf("loopback listed: %v", ips)
		}
	}
}

func TestBenchmarkNetIsSkipped(t *testing.T) {
	if !benchmarkNet.Contains(net.ParseIP("198.18.0.1")) || benchmarkNet.Contains(net.ParseIP("192.168.2.11")) {
		t.Fatal("benchmark range check wrong")
	}
}
