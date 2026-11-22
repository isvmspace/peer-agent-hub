package main

// Claude Code: one JSON-lines log per session under ~/.claude/projects/<folder>/; sub-agents under
// <session>/subagents/agent-<id>.jsonl with a .meta.json naming the Task call that started them.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var claudeSubagentTools = map[string]bool{"Task": true, "Agent": true}

func isoSeconds(v string) float64 {
	if v == "" {
		return 0
	}
	t, err := time.Parse(time.RFC3339Nano, v)
	if err != nil {
		return 0
	}
	return float64(t.UnixNano()) / 1e9
}

func claudeCollabFiles(since float64) []string {
	var out []string
	root := sessionRoot("claude")
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".jsonl") {
			return nil
		}
		if info, e := d.Info(); e == nil && float64(info.ModTime().Unix()) >= since {
			out = append(out, path)
		}
		return nil
	})
	return out
}

type claudeBlock struct {
	Type      string          `json:"type"`
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Input     map[string]any  `json:"input"`
	Text      string          `json:"text"`
	ToolUseID string          `json:"tool_use_id"`
	Content   json.RawMessage `json:"content"`
}

type claudeLine struct {
	Type        string `json:"type"`
	AITitle     string `json:"aiTitle"`
	CustomTitle string `json:"customTitle"` // the name the user gave it (/rename)
	SessionID   string `json:"sessionId"`
	Cwd         string `json:"cwd"`
	Timestamp   string `json:"timestamp"`
	Entrypoint  string `json:"entrypoint"`
	IsMeta      bool   `json:"isMeta"`
	Message     struct {
		Content json.RawMessage `json:"content"`
	} `json:"message"`
}

// claudeContent is a message's text, and its blocks when it has any.
func claudeContent(raw json.RawMessage) (string, []claudeBlock) {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s, nil
	}
	var blocks []claudeBlock
	_ = json.Unmarshal(raw, &blocks)
	var texts []string
	for _, b := range blocks {
		if b.Type == "text" {
			texts = append(texts, b.Text)
		}
	}
	return strings.Join(texts, "\n"), blocks
}

func claudeCallSummary(name string, args map[string]any) (summary, command string) {
	str := func(k string) string { s, _ := args[k].(string); return s }
	switch {
	case name == "Bash":
		command = str("command")
		summary = str("description")
		if summary == "" {
			summary = command
		}
	case claudeSubagentTools[name]:
		command = str("prompt")
		summary = str("description")
		if summary == "" {
			summary = command
		}
	default:
		for _, k := range []string{"file_path", "path", "pattern", "url", "query", "skill"} {
			if v := str(k); v != "" {
				summary = v
				break
			}
		}
	}
	return oneLine(redactTool(summary), 120), command // masked before cutting, so a cut token can't slip through
}

// claudeParser reads a log line by line, so a growing log can be read on from where it stopped.
// Without detail only what the overview and linking need is kept.
type claudeParser struct {
	path, stem  string
	sub, detail bool
	metaTitle   string
	metaCall    string
	aiTitle     string
	customTitle string
	s           *CollabSession
	calls       map[string]int
}

func newClaudeParser(path string, detail bool) *claudeParser {
	p := &claudeParser{path: path, detail: detail, calls: map[string]int{},
		sub: filepath.Base(filepath.Dir(path)) == "subagents", stem: strings.TrimSuffix(filepath.Base(path), ".jsonl")}
	if p.sub {
		var meta struct {
			Description string `json:"description"`
			ToolUseID   string `json:"toolUseId"`
		}
		if raw, err := os.ReadFile(strings.TrimSuffix(path, ".jsonl") + ".meta.json"); err == nil {
			_ = json.Unmarshal(raw, &meta)
		}
		p.metaTitle, p.metaCall = meta.Description, meta.ToolUseID
	}
	return p
}

func (p *claudeParser) feed(raw []byte) {
	var line claudeLine
	if json.Unmarshal(raw, &line) != nil {
		return
	}
	if line.Type == "custom-title" {
		if t := strings.TrimSpace(line.CustomTitle); t != "" {
			p.customTitle = t
		}
		return
	}
	if line.Type == "ai-title" {
		if line.AITitle != "" {
			p.aiTitle = line.AITitle
		}
		return
	}
	if line.Type != "user" && line.Type != "assistant" {
		return
	}
	when := isoSeconds(line.Timestamp)
	s := p.s
	if s == nil {
		sid := line.SessionID
		if sid == "" {
			sid = p.stem
		}
		s = &CollabSession{Agent: "claude", ID: sid, Started: when, Ended: when, Cwd: line.Cwd, Source: p.path, Origin: "终端"}
		if line.Entrypoint == "sdk-cli" {
			s.Origin = "非交互（-p）"
		}
		if p.sub {
			s.ID = sid + "/" + p.stem
			s.Origin = "子代理"
			s.NativeParent, s.NativeCall = "claude:"+sid, p.metaCall
		}
		p.s = s
	}
	if when > s.Ended {
		s.Ended = when
	}
	text, blocks := claudeContent(line.Message.Content)
	if line.Type == "user" && !line.IsMeta && !hasToolResult(blocks) {
		if t := strings.TrimSpace(text); t != "" && !strings.HasPrefix(t, "<command-") && !strings.HasPrefix(t, "<local-command") &&
			!strings.HasPrefix(t, "<system-reminder") && !strings.HasPrefix(t, "Caveat:") &&
			!strings.HasPrefix(t, "<task-notification") { // Claude Code's own notices, not the person's questions
			s.Turns = append(s.Turns, CollabTurn{TS: when, Prompt: collabClip(text, collabMaxText)})
			return
		}
	}
	for _, b := range blocks {
		switch {
		case b.Type == "tool_use":
			s.Tools++
			summary, command := claudeCallSummary(b.Name, b.Input)
			var launches []string
			if b.Name == "Bash" {
				launches = launchedAgents(command)
			} else if claudeSubagentTools[b.Name] {
				launches = []string{"claude"}
			}
			if p.detail || len(launches) > 0 {
				p.calls[b.ID] = len(s.Calls)
				s.Calls = append(s.Calls, CollabCall{ID: b.ID, Tool: b.Name, TS: when, Summary: summary,
					Command: collabClip(command, collabMaxCommand), Launches: launches})
			}
		case b.Type == "tool_result":
			if i, ok := p.calls[b.ToolUseID]; ok {
				end := when
				s.Calls[i].End = &end
				if p.detail {
					result, _ := claudeContent(b.Content)
					if result == "" {
						result = string(b.Content)
					}
					s.Calls[i].Result = collabClip(result, collabMaxResult)
				}
			}
		case b.Type == "text" && line.Type == "assistant" && len(s.Turns) > 0:
			s.Turns[len(s.Turns)-1].Reply = collabClip(b.Text, collabMaxText) // the last text of a turn is the reply
		}
	}
}

// session is what has been read so far (nil before the first message).
func (p *claudeParser) session() *CollabSession {
	if p.s == nil {
		return nil
	}
	p.s.Title = p.customTitle // the user's own name first, then the sub-agent's description, then the automatic one
	if p.s.Title == "" {
		p.s.Title = p.metaTitle
	}
	if p.s.Title == "" {
		p.s.Title = p.aiTitle
	}
	if p.s.Title == "" && len(p.s.Turns) > 0 {
		p.s.Title = oneLine(p.s.Turns[0].Prompt, 80)
	}
	return p.s
}

// parseClaude reads a whole log.
func parseClaude(path string, detail bool) (*CollabSession, error) {
	p := newClaudeParser(path, detail)
	err := eachLine(path, func(raw []byte) bool { p.feed(raw); return true })
	if err != nil {
		return nil, err
	}
	return p.session(), nil
}

func hasToolResult(blocks []claudeBlock) bool {
	for _, b := range blocks {
		if b.Type == "tool_result" {
			return true
		}
	}
	return false
}
