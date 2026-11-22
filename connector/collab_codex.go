package main

// Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. The first line (session_meta) says how it was started:
// originator `codex_exec` is a non-interactive `codex exec`, which is how other agents hand Codex work.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var (
	codexOrigins = map[string]string{"codex_exec": "exec", "codex-tui": "终端", "Codex Desktop": "桌面版", "codex_vscode": "VS Code"}
	// what Codex puts in front of the person's message: project instructions and environment, not the request
	codexWrappers = []string{"# AGENTS.md", "<environment_context>", "<user_instructions>", "<permissions", "<INSTRUCTIONS>", "<turn_aborted>"}
	codexShellCmd = regexp.MustCompile(`\bcmd\s*:\s*"((?:[^"\\]|\\.)*)"`)
	hubTaskPrompt = regexp.MustCompile(`^\[Agent Hub 任务 #(\d+)`)
)

func codexCollabFiles(since float64) []string {
	matches, _ := filepath.Glob(filepath.Join(sessionRoot("codex"), "*", "*", "*", "*.jsonl"))
	var out []string
	for _, p := range matches {
		if info, err := os.Stat(p); err == nil && float64(info.ModTime().Unix()) >= since {
			out = append(out, p)
		}
	}
	return out
}

// codexShell is the shell command inside a Codex tool call (its tools wrap commands in small scripts).
func codexShell(text string) string {
	if found := codexShellCmd.FindAllStringSubmatch(text, -1); len(found) > 0 {
		var cmds []string
		for _, m := range found {
			var s string
			if json.Unmarshal([]byte(`"`+m[1]+`"`), &s) != nil {
				s = m[1]
			}
			cmds = append(cmds, s)
		}
		return strings.Join(cmds, "\n")
	}
	var args map[string]any
	if json.Unmarshal([]byte(text), &args) == nil {
		switch c := firstNonNil(args["cmd"], args["command"]).(type) {
		case string:
			return c
		case []any:
			parts := make([]string, 0, len(c))
			for _, p := range c {
				if s, ok := p.(string); ok {
					parts = append(parts, s)
				}
			}
			return strings.Join(parts, " ")
		}
	}
	return text
}

func firstNonNil(vals ...any) any {
	for _, v := range vals {
		if v != nil {
			return v
		}
	}
	return nil
}

func codexText(content []map[string]any) string {
	var texts []string
	for _, c := range content {
		if t, ok := c["text"].(string); ok {
			texts = append(texts, t)
		}
	}
	return strings.Join(texts, "\n")
}

// codexParser reads a rollout line by line, so a growing one can be read on from where it stopped.
type codexParser struct {
	path   string
	detail bool
	s      *CollabSession
	calls  map[string]int
}

func newCodexParser(path string, detail bool) *codexParser {
	return &codexParser{path: path, detail: detail, calls: map[string]int{}}
}

func (c *codexParser) feed(raw []byte) {
	s := c.s
	var line struct {
		Timestamp string `json:"timestamp"`
		Type      string `json:"type"`
		Payload   struct {
			Type             string           `json:"type"`
			ID               string           `json:"id"`
			Timestamp        string           `json:"timestamp"`
			Cwd              string           `json:"cwd"`
			Originator       string           `json:"originator"`
			Source           json.RawMessage  `json:"source"`
			Role             string           `json:"role"`
			Content          []map[string]any `json:"content"`
			Name             string           `json:"name"`
			CallID           string           `json:"call_id"`
			Input            string           `json:"input"`
			Arguments        string           `json:"arguments"`
			Output           json.RawMessage  `json:"output"`
			LastAgentMessage string           `json:"last_agent_message"`
		} `json:"payload"`
	}
	if json.Unmarshal(raw, &line) != nil {
		return
	}
	p := line.Payload
	when := isoSeconds(line.Timestamp)
	if line.Type == "session_meta" {
		origin, ok := codexOrigins[p.Originator]
		if !ok {
			origin = p.Originator
		}
		if strings.Contains(string(p.Source), `"subagent"`) {
			origin = "子代理"
		}
		started := isoSeconds(p.Timestamp)
		if started == 0 {
			started = when
		}
		s = &CollabSession{Agent: "codex", ID: p.ID, Started: started, Ended: when, Cwd: p.Cwd, Origin: origin, Source: c.path}
		c.s = s
		return
	}
	if s == nil {
		return
	}
	if when > s.Ended {
		s.Ended = when
	}
	switch {
	case line.Type == "response_item" && p.Type == "message" && p.Role == "user":
		text := codexText(p.Content)
		trimmed := strings.TrimSpace(text)
		wrapped := false
		for _, w := range codexWrappers {
			wrapped = wrapped || strings.HasPrefix(trimmed, w)
		}
		if trimmed != "" && !wrapped {
			s.Turns = append(s.Turns, CollabTurn{TS: when, Prompt: collabClip(text, collabMaxText)})
			if m := hubTaskPrompt.FindStringSubmatch(trimmed); m != nil && len(s.Turns) == 1 {
				s.Origin = "Agent Hub #" + m[1]
			}
		}
	case line.Type == "response_item" && (p.Type == "function_call" || p.Type == "custom_tool_call" || p.Type == "local_shell_call"):
		s.Tools++
		args := p.Input
		if args == "" {
			args = p.Arguments
		}
		command := codexShell(args)
		launches := launchedAgents(command)
		if c.detail || len(launches) > 0 {
			tool := p.Name
			if tool == "" {
				tool = p.Type
			}
			c.calls[p.CallID] = len(s.Calls)
			s.Calls = append(s.Calls, CollabCall{ID: p.CallID, Tool: tool, TS: when, Summary: oneLine(redactTool(command), 120),
				Command: collabClip(command, collabMaxCommand), Launches: launches})
		}
	case line.Type == "response_item" && (p.Type == "function_call_output" || p.Type == "custom_tool_call_output"):
		if i, ok := c.calls[p.CallID]; ok {
			end := when
			s.Calls[i].End = &end
			if c.detail {
				var out string
				if json.Unmarshal(p.Output, &out) != nil {
					out = string(p.Output)
				}
				s.Calls[i].Result = collabClip(out, collabMaxResult)
			}
		}
	case line.Type == "event_msg" && p.Type == "task_complete" && len(s.Turns) > 0:
		s.Turns[len(s.Turns)-1].Reply = collabClip(p.LastAgentMessage, collabMaxText)
	case line.Type == "response_item" && p.Type == "message" && p.Role == "assistant" && len(s.Turns) > 0:
		if text := codexText(p.Content); strings.TrimSpace(text) != "" {
			s.Turns[len(s.Turns)-1].Reply = collabClip(text, collabMaxText)
		}
	}
}

func (c *codexParser) session() *CollabSession {
	if c.s != nil && len(c.s.Turns) > 0 {
		c.s.Title = oneLine(c.s.Turns[0].Prompt, 80)
	}
	return c.s
}

func parseCodex(path string, detail bool) (*CollabSession, error) {
	c := newCodexParser(path, detail)
	if err := eachLine(path, func(raw []byte) bool { c.feed(raw); return true }); err != nil {
		return nil, err
	}
	return c.session(), nil
}

// codexThreadNames reads the names Codex gives its threads, renames included (the latest line for an id wins).
func codexThreadNames() map[string]string {
	names := map[string]string{}
	_ = eachLine(filepath.Join(homeDir("CODEX_HOME", ".codex"), "session_index.jsonl"), func(raw []byte) bool {
		var e struct {
			ID         string `json:"id"`
			ThreadName string `json:"thread_name"`
		}
		if json.Unmarshal(raw, &e) == nil && e.ID != "" && strings.TrimSpace(e.ThreadName) != "" {
			names[e.ID] = strings.TrimSpace(e.ThreadName)
		}
		return true
	})
	return names
}
