package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"
)

// Local Claude Code / Codex conversations the owner can import into the hub
// and share. Only sessions started inside one of the agent's workspaces are
// listed, and nothing is read until the owner asks for it from the console.

const (
	maxListedSessions = 50
	maxScannedFiles   = 400
	maxSessionTurns   = 40
	maxTurnChars      = 6000
)

type SessionInfo struct {
	ID        string `json:"id"`
	Cwd       string `json:"cwd"`
	Title     string `json:"title"`
	UpdatedAt int64  `json:"updated_at"`
	path      string
	sub       bool // a CLI-internal sub-session (e.g. Codex guardian review)
}

type SessionTurn struct {
	Prompt string `json:"prompt"`
	Result string `json:"result"`
}

type SessionDetail struct {
	SessionInfo
	Turns []SessionTurn `json:"turns"`
}

// clip keeps the first n runes; transcripts are shown from the start.
func clip(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

// userText drops the context blocks CLIs inject into user turns (<environment_context>, <system-reminder>...).
// injected matches context the CLIs add to a user turn (slash commands, reminders, environment), not what a
// person typed: a prompt that merely starts with HTML or XML is kept.
var injected = regexp.MustCompile(`^<(command-|local-command-|system-reminder|environment_context|user_instructions|` +
	`bash-|task-notification|user-prompt-submit-hook|ide_|turn_aborted|permissions)`)

func userText(parts []string) string {
	var kept []string
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" && !injected.MatchString(p) && !strings.HasPrefix(p, "# AGENTS.md instructions") {
			kept = append(kept, p)
		}
	}
	return strings.Join(kept, "\n")
}

func homeDir(env, fallback string) string {
	if v := os.Getenv(env); v != "" {
		return v
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, fallback)
}

func sessionRoot(kind string) string {
	switch kind {
	case "claude":
		return filepath.Join(homeDir("CLAUDE_CONFIG_DIR", ".claude"), "projects")
	case "codex":
		return filepath.Join(homeDir("CODEX_HOME", ".codex"), "sessions")
	}
	return ""
}

func sameDir(a, b string) bool {
	a, b = filepath.Clean(a), filepath.Clean(b)
	if runtime.GOOS == "windows" {
		return strings.EqualFold(a, b)
	}
	return a == b
}

func (a AgentConfig) workspaceFor(cwd string) string {
	for _, w := range a.Workspaces {
		if cwd != "" && sameDir(w.Path, cwd) {
			return w.Path
		}
	}
	return ""
}

// sessionFiles returns transcript files, newest first.
func sessionFiles(kind string) []string {
	root := sessionRoot(kind)
	if root == "" {
		return nil
	}
	type item struct {
		path string
		mod  time.Time
	}
	var items []item
	add := func(path string, d fs.DirEntry) {
		if info, err := d.Info(); err == nil {
			items = append(items, item{path, info.ModTime()})
		}
	}
	if kind == "claude" {
		// <root>/<project>/<session>.jsonl; subagent transcripts live deeper and are skipped
		dirs, _ := os.ReadDir(root)
		for _, d := range dirs {
			if !d.IsDir() {
				continue
			}
			files, _ := os.ReadDir(filepath.Join(root, d.Name()))
			for _, f := range files {
				if !f.IsDir() && strings.HasSuffix(f.Name(), ".jsonl") {
					add(filepath.Join(root, d.Name(), f.Name()), f)
				}
			}
		}
	} else {
		_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if err == nil && !d.IsDir() && strings.HasPrefix(d.Name(), "rollout-") && strings.HasSuffix(d.Name(), ".jsonl") {
				add(path, d)
			}
			return nil
		})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].mod.After(items[j].mod) })
	paths := make([]string, 0, len(items))
	for _, it := range items {
		paths = append(paths, it.path)
	}
	return paths
}

// eachLine calls fn for every JSON line until it returns false.
func eachLine(path string, fn func(line []byte) bool) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	r := bufio.NewReaderSize(f, 1<<20)
	for {
		line, err := r.ReadBytes('\n')
		if len(line) > 0 && !fn(line) {
			return nil
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// parseSession reads a transcript. With head=true it stops at the first prompt.
func parseSession(kind, path string, head bool) (SessionDetail, error) {
	var s SessionDetail
	s.path = path
	if info, err := os.Stat(path); err == nil {
		s.UpdatedAt = info.ModTime().Unix()
	}
	var turns []SessionTurn
	codexItems := false // newer Codex logs item_completed events; older ones only response_item
	named := ""         // a Claude session renamed with /rename
	prompt := func(parts []string) bool {
		text := userText(parts)
		if text == "" {
			return true
		}
		if s.Title == "" {
			s.Title = clip(strings.Join(strings.Fields(text), " "), 60)
		}
		turns = append(turns, SessionTurn{Prompt: clip(text, maxTurnChars)})
		return !head
	}
	reply := func(text string) {
		if text = strings.TrimSpace(text); text != "" && len(turns) > 0 {
			turns[len(turns)-1].Result = clip(text, maxTurnChars)
		}
	}
	err := eachLine(path, func(line []byte) bool {
		if kind == "claude" {
			var ev struct {
				Type        string          `json:"type"`
				Cwd         string          `json:"cwd"`
				SessionID   string          `json:"sessionId"`
				IsMeta      bool            `json:"isMeta"`
				IsSidechain bool            `json:"isSidechain"`
				CustomTitle string          `json:"customTitle"`
				Message     json.RawMessage `json:"message"`
			}
			if json.Unmarshal(line, &ev) != nil || ev.IsSidechain {
				return true
			}
			if ev.Type == "custom-title" && strings.TrimSpace(ev.CustomTitle) != "" {
				named = strings.TrimSpace(ev.CustomTitle)
			}
			if s.Cwd == "" && ev.Cwd != "" {
				s.Cwd, s.ID = ev.Cwd, ev.SessionID
			}
			if (ev.Type != "user" && ev.Type != "assistant") || ev.IsMeta {
				return true
			}
			var msg struct {
				Content json.RawMessage `json:"content"`
			}
			if json.Unmarshal(ev.Message, &msg) != nil {
				return true
			}
			parts, isTool := claudeText(msg.Content)
			if ev.Type == "user" {
				if isTool {
					return true
				}
				return prompt(parts)
			}
			reply(strings.Join(parts, "\n"))
			return true
		}
		var ev struct {
			Type    string `json:"type"`
			Payload struct {
				Type    string `json:"type"`
				ID      string `json:"id"`
				Cwd     string `json:"cwd"`
				Parent  string `json:"parent_thread_id"`
				Role    string `json:"role"`
				Content []struct {
					Text string `json:"text"`
				} `json:"content"`
				Item struct {
					Type    string `json:"type"`
					Content []struct {
						Text string `json:"text"`
					} `json:"content"`
				} `json:"item"`
			} `json:"payload"`
		}
		if json.Unmarshal(line, &ev) != nil {
			return true
		}
		p := ev.Payload
		texts := func(parts []struct {
			Text string `json:"text"`
		}) []string {
			out := make([]string, 0, len(parts))
			for _, c := range parts {
				out = append(out, c.Text)
			}
			return out
		}
		switch {
		case ev.Type == "session_meta":
			s.ID, s.Cwd, s.sub = p.ID, p.Cwd, p.Parent != ""
		case ev.Type == "event_msg" && p.Type == "item_completed":
			if p.Item.Type == "UserMessage" && !codexItems {
				// item events are authoritative; drop turns guessed from raw response items
				turns, s.Title = nil, ""
			}
			codexItems = true
			if p.Item.Type == "UserMessage" {
				return prompt(texts(p.Item.Content))
			}
			if p.Item.Type == "AgentMessage" {
				reply(strings.Join(texts(p.Item.Content), ""))
			}
		case ev.Type == "response_item" && p.Type == "message" && !codexItems:
			if p.Role == "user" {
				return prompt(texts(p.Content))
			}
			if p.Role == "assistant" {
				reply(strings.Join(texts(p.Content), ""))
			}
		}
		return true
	})
	if len(turns) > maxSessionTurns {
		turns = turns[len(turns)-maxSessionTurns:]
	}
	s.Turns = turns
	if kind == "claude" && head && s.Title != "" {
		if t := lastCustomTitle(path); t != "" {
			named = t
		}
	}
	if named != "" && s.Title != "" {
		s.Title = clip(named, 60)
	}
	return s, err
}

// lastCustomTitle returns the latest /rename title in a Claude transcript. A listing stops at the first prompt,
// but a rename can come anywhere later, so the rest of the file is scanned for that one record type only.
func lastCustomTitle(path string) string {
	title := ""
	marker := []byte(`"custom-title"`)
	_ = eachLine(path, func(line []byte) bool {
		if !bytes.Contains(line, marker) {
			return true
		}
		var ev struct {
			Type        string `json:"type"`
			CustomTitle string `json:"customTitle"`
		}
		if json.Unmarshal(line, &ev) == nil && ev.Type == "custom-title" && strings.TrimSpace(ev.CustomTitle) != "" {
			title = strings.TrimSpace(ev.CustomTitle)
		}
		return true
	})
	return title
}

// claudeText returns the text parts of a message and whether it is a tool result.
func claudeText(raw json.RawMessage) ([]string, bool) {
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return []string{text}, false
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &blocks) != nil {
		return nil, false
	}
	var parts []string
	for _, bl := range blocks {
		switch bl.Type {
		case "tool_result":
			return nil, true
		case "text":
			parts = append(parts, bl.Text)
		}
	}
	return parts, false
}

// listSessions returns the agent's recent local conversations inside its workspaces.
func listSessions(a AgentConfig) ([]SessionInfo, error) {
	if sessionRoot(a.Kind) == "" {
		return nil, errors.New("只有 Claude Code 和 Codex 智能体支持分享本机会话")
	}
	out := []SessionInfo{}
	for i, path := range sessionFiles(a.Kind) {
		if i >= maxScannedFiles || len(out) >= maxListedSessions {
			break
		}
		s, err := parseSession(a.Kind, path, true)
		// runs started by the hub are already hub conversations
		if err != nil || s.ID == "" || s.Title == "" || s.sub || strings.HasPrefix(s.Title, "[Agent Hub 任务") ||
			a.workspaceFor(s.Cwd) == "" {
			continue
		}
		out = append(out, s.SessionInfo)
	}
	return out, nil
}

// readSession loads one conversation, refusing anything outside the agent's workspaces.
func readSession(a AgentConfig, id string) (SessionDetail, error) {
	if id == "" || strings.ContainsAny(id, `/\`) {
		return SessionDetail{}, errors.New("会话 ID 无效")
	}
	for i, path := range sessionFiles(a.Kind) {
		if i >= maxScannedFiles*4 {
			break
		}
		if !strings.Contains(filepath.Base(path), id) {
			continue
		}
		s, err := parseSession(a.Kind, path, false)
		if err != nil {
			return s, err
		}
		if s.ID != id {
			continue
		}
		if a.workspaceFor(s.Cwd) == "" {
			return SessionDetail{}, errors.New("该会话不在智能体的授权工作区内")
		}
		return s, nil
	}
	return SessionDetail{}, errors.New("本机找不到该会话")
}
