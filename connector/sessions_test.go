package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReadClaudeAndCodexSessions(t *testing.T) {
	home := t.TempDir()
	ws := filepath.Join(home, "proj")
	os.MkdirAll(ws, 0o755)
	t.Setenv("CLAUDE_CONFIG_DIR", filepath.Join(home, "claude"))
	t.Setenv("CODEX_HOME", filepath.Join(home, "codex"))
	cdir := filepath.Join(home, "claude", "projects", "-proj")
	os.MkdirAll(cdir, 0o755)
	os.WriteFile(filepath.Join(cdir, "s1.jsonl"), []byte(
		`{"type":"user","cwd":"`+ws+`","sessionId":"s1","message":{"content":"<command-name>/x</command-name>"}}
{"type":"user","cwd":"`+ws+`","sessionId":"s1","message":{"content":"帮我看下构建"}}
{"type":"assistant","cwd":"`+ws+`","sessionId":"s1","message":{"content":[{"type":"text","text":"我先看看"}]}}
{"type":"user","cwd":"`+ws+`","sessionId":"s1","message":{"content":[{"type":"tool_result","content":"ok"}]}}
{"type":"assistant","cwd":"`+ws+`","sessionId":"s1","message":{"content":[{"type":"text","text":"构建正常"}]}}
`), 0o644)
	xdir := filepath.Join(home, "codex", "sessions", "2026", "10", "31")
	os.MkdirAll(xdir, 0o755)
	os.WriteFile(filepath.Join(xdir, "rollout-2026-x-c1.jsonl"), []byte(
		`{"type":"session_meta","payload":{"id":"c1","cwd":"`+ws+`"}}
{"type":"response_item","payload":{"type":"message","role":"user","content":[{"text":"<env>"}]}}
{"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"text":"<environment_context>x</environment_context>"},{"text":"打包"}]}}}
{"type":"event_msg","payload":{"type":"item_completed","item":{"type":"AgentMessage","content":[{"text":"好了"}]}}}
`), 0o644)

	claude := AgentConfig{Key: "claude", Kind: "claude", Workspaces: []Workspace{{Name: "proj", Path: ws}}}
	list, err := listSessions(claude)
	if err != nil || len(list) != 1 || list[0].ID != "s1" || list[0].Title != "帮我看下构建" {
		t.Fatalf("claude list = %+v %v", list, err)
	}
	s, err := readSession(claude, "s1")
	if err != nil || len(s.Turns) != 1 || s.Turns[0].Result != "构建正常" {
		t.Fatalf("claude read = %+v %v", s, err)
	}
	codex := AgentConfig{Key: "codex", Kind: "codex", Workspaces: []Workspace{{Name: "proj", Path: ws}}}
	s, err = readSession(codex, "c1")
	if err != nil || len(s.Turns) != 1 || s.Turns[0].Prompt != "打包" || s.Turns[0].Result != "好了" {
		t.Fatalf("codex read = %+v %v", s, err)
	}
	// outside the workspaces nothing is listed or readable
	other := AgentConfig{Key: "claude", Kind: "claude", Workspaces: []Workspace{{Name: "x", Path: home}}}
	if list, _ := listSessions(other); len(list) != 0 {
		t.Fatalf("leaked %+v", list)
	}
	if _, err := readSession(other, "s1"); err == nil {
		t.Fatal("read outside workspace")
	}
}

func TestClaudeSessionUsesRenamedTitle(t *testing.T) {
	home := t.TempDir()
	ws := filepath.Join(home, "proj")
	os.MkdirAll(ws, 0o755)
	t.Setenv("CLAUDE_CONFIG_DIR", filepath.Join(home, "claude"))
	cdir := filepath.Join(home, "claude", "projects", "-proj")
	os.MkdirAll(cdir, 0o755)
	os.WriteFile(filepath.Join(cdir, "s2.jsonl"), []byte(
		`{"type":"user","cwd":"`+ws+`","sessionId":"s2","message":{"content":"读一下交接文档"}}
{"type":"custom-title","customTitle":"生产CRS处理","sessionId":"s2"}
{"type":"assistant","cwd":"`+ws+`","sessionId":"s2","message":{"content":[{"type":"text","text":"`+strings.Repeat("x", 400<<10)+`"}]}}
`), 0o644)
	os.WriteFile(filepath.Join(cdir, "s3.jsonl"), []byte(
		`{"type":"user","cwd":"`+ws+`","sessionId":"s3","message":{"content":"<div>这段 HTML 为什么不居中</div>"}}
`), 0o644)
	list, err := listSessions(AgentConfig{Key: "claude", Kind: "claude", Workspaces: []Workspace{{Name: "proj", Path: ws}}})
	titles := map[string]string{}
	for _, s := range list {
		titles[s.ID] = s.Title
	}
	// renamed after the first prompt, and a real prompt that starts with markup
	if err != nil || titles["s2"] != "生产CRS处理" || titles["s3"] != "<div>这段 HTML 为什么不居中</div>" {
		t.Fatalf("list = %+v %v", list, err)
	}
}
