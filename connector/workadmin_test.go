package main

import (
	"encoding/json"
	"flag"
	"strings"
	"testing"
)

func TestParseCodexQuotaReplies(t *testing.T) {
	var info accountInfo
	parseCodexReply(2, json.RawMessage(`{"ordinaryUsageAllowed":false,"rateLimits":{"primary":{"usedPercent":100,"windowDurationMins":10080,"resetsAt":1794880321},"secondary":null,"planType":"pro"}}`), &info)
	parseCodexReply(3, json.RawMessage(`{"account":{"type":"chatgpt","email":"dev.user@example.com","planType":"pro"}}`), &info)
	if info.Plan != "pro" || info.Usable || info.Primary == nil || info.Primary.UsedPercent != 100 || info.Primary.WindowMins != 10080 {
		t.Fatalf("unexpected quota: %+v", info)
	}
	if info.Secondary != nil {
		t.Fatalf("missing secondary window must stay nil")
	}
	if info.Email != "de***@example.com" {
		t.Fatalf("email must be masked, got %q", info.Email)
	}
}

func TestAccountChoiceAndLegacyAuth(t *testing.T) {
	h := newWorkhost(WorkhostConfig{CodexAuth: "/data/acct/auth.json"})
	a, ok := h.account("", "codex")
	if !ok || a.ID != "codex-default" || a.Home != "/data/acct" {
		t.Fatalf("legacy codex_auth must become the default account: %+v", a)
	}
	if _, ok := h.account("other", "codex"); ok {
		t.Fatalf("an unknown account id must not fall back to another account")
	}
	h = newWorkhost(WorkhostConfig{Accounts: []WorkAccount{{ID: "c1", Kind: "codex", Home: "/a"}, {ID: "c2", Kind: "codex", Home: "/b"}}})
	if a, _ := h.account("c2", "codex"); a.Home != "/b" {
		t.Fatalf("hub's choice must be honoured: %+v", a)
	}
}

func TestWorkContextRejectsUnsafeNames(t *testing.T) {
	bad := []WorkContext{
		{User: "u1;rm -rf /"},
		{User: "u1", Session: "../x"},
		{User: "u1", Branch: "agent/a/../b"},
		{User: "u1", Repos: []WorkRepo{{Name: "r", URL: "ssh://git@x/y.git", Branch: "main"}}},
	}
	for _, w := range bad {
		if w.validate() == nil {
			t.Fatalf("expected %+v to be rejected", w)
		}
	}
	ok := WorkContext{User: "u2", Session: "s-1", Branch: "agent/alice/s-1", Folder: "d-1a2b",
		Repos: []WorkRepo{{Name: "demo", URL: "file:///srv/git/demo.git", Branch: "main"}}}
	if err := ok.validate(); err != nil {
		t.Fatal(err)
	}
}

func TestWorkPromptOnlyOnFirstTurn(t *testing.T) {
	w := &WorkContext{DirName: "AI平台", DirDescription: "一起改", Branch: "agent/a/s-1", Repos: []WorkRepo{{Name: "demo"}}}
	first := workPrompt(Task{Prompt: "加函数", From: "alice", Work: w})
	if !containsAll(first, "AI平台", "一起改", "demo", "agent/a/s-1", "不要 git fetch / pull / push", "checkout -B agent/a/s-1 origin/", "merge --ff-only", "rebase origin/", "加函数") {
		t.Fatalf("first turn preamble incomplete: %s", first)
	}
	if next := workPrompt(Task{Prompt: "再补测试", SessionID: "th", Work: w}); !strings.HasSuffix(next, "\n\n再补测试") || !strings.Contains(next, "已同步到最新") || strings.Contains(next, "工作目录") {
		t.Fatalf("a resumed turn must carry only the message, got %q", next)
	}
	if got := buildPrompt(Task{Prompt: "x", Work: w}, true); got != "x" {
		t.Fatalf("work turns skip the hub preamble, got %q", got)
	}
}

func containsAll(s string, parts ...string) bool {
	for _, p := range parts {
		if !strings.Contains(s, p) {
			return false
		}
	}
	return true
}

func TestPickWorkAgents(t *testing.T) {
	cfg := &Config{Agents: []AgentConfig{{Key: "codex", Kind: "codex"}, {Key: "claude", Kind: "claude"}, {Key: "hermes", Kind: "hermes"}}}
	if cfg.isWorkAgent(cfg.Agents[0]) {
		t.Fatal("no workhost section: nothing serves the work menu")
	}
	cfg.Workhost = &WorkhostConfig{}
	if !cfg.isWorkAgent(cfg.Agents[2]) {
		t.Fatal("an old config without per-agent marks offers every agent")
	}
	if err := pickWorkAgents(cfg, "codex, claude"); err != nil {
		t.Fatal(err)
	}
	if !cfg.isWorkAgent(cfg.Agents[0]) || !cfg.isWorkAgent(cfg.Agents[1]) || cfg.isWorkAgent(cfg.Agents[2]) {
		t.Fatalf("only the picked agents serve the work menu: %+v", cfg.Agents)
	}
	if err := pickWorkAgents(cfg, "nope"); err == nil {
		t.Fatal("an unknown agent key must be rejected")
	}
	if err := pickWorkAgents(cfg, ""); err != nil || !cfg.isWorkAgent(cfg.Agents[2]) {
		t.Fatal("no keys means every agent")
	}
}

func TestFlagsAfterSubcommandAreParsed(t *testing.T) {
	fs := flag.NewFlagSet("workhost", flag.ContinueOnError)
	config := fs.String("config", "default.json", "")
	agents := fs.String("agents", "", "")
	pos := parseInterleaved(fs, []string{"enable", "--agents", "codex", "-config", "x.json", "extra"})
	if *config != "x.json" || *agents != "codex" || len(pos) != 2 || pos[0] != "enable" || pos[1] != "extra" {
		t.Fatalf("config=%q agents=%q pos=%v", *config, *agents, pos)
	}
}

func TestTurnNoteReportsFailedSync(t *testing.T) {
	w := &WorkContext{Branch: "agent/a/s-1", syncFailed: []string{"demo"}, bases: []string{"demo → feature/v1.4.0"}}
	if note := turnNote(w); !strings.Contains(note, "demo") || !strings.Contains(note, "Git 设置") || !strings.Contains(note, "demo → feature/v1.4.0") {
		t.Fatalf("a failed sync must name the repository and the fix: %s", note)
	}
}
