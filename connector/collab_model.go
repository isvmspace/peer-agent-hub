package main

// 本机协作: the sessions of the agents on this machine (Claude Code, Codex, Hermes, OpenCode) read from their own
// logs, and who handed work to whom. Read only when the owner opens the page on the Hub; nothing is kept there.

import (
	"regexp"
	"strings"
	"unicode/utf8"
)

const (
	collabMaxText    = 6000
	collabMaxCommand = 4000
	collabMaxResult  = 3000
)

type CollabTurn struct {
	TS     float64 `json:"ts"`
	Prompt string  `json:"prompt"`
	Reply  string  `json:"reply"`
}

// CollabCall is a tool call; the ones that start another agent link sessions together.
type CollabCall struct {
	ID       string   `json:"id"`
	Tool     string   `json:"tool"`
	TS       float64  `json:"ts"`
	Summary  string   `json:"summary"`
	Command  string   `json:"command"`
	End      *float64 `json:"end"`
	Result   string   `json:"result"`
	Launches []string `json:"launches"`
}

type CollabSession struct {
	Agent, ID, Title, Cwd, Origin string
	Started, Ended                float64
	Turns                         []CollabTurn
	Calls                         []CollabCall
	Tools                         int
	Parent, ParentCall, Link      string // set by link(): who started it, through which call, native | inferred
	NativeParent, NativeCall      string // a parent the agent recorded itself
	Source                        string // file it came from, to read the detail again
}

func (s *CollabSession) Key() string { return s.Agent + ":" + s.ID }

func (s *CollabSession) Brief() map[string]any {
	title := s.Title
	if title == "" {
		title = "（无标题）"
	}
	return map[string]any{
		"key": s.Key(), "agent": s.Agent, "id": s.ID, "title": title, "started": s.Started, "ended": s.Ended,
		"cwd": s.Cwd, "origin": s.Origin, "turns": len(s.Turns), "tools": s.Tools, "parent": s.Parent, "link": s.Link,
	}
}

// a shell command that starts another agent non-interactively, by the agent it starts
var collabLaunchers = []struct {
	agent string
	re    *regexp.Regexp
}{
	{"codex", regexp.MustCompile(`\bcodex\s+(?:exec|e)\b`)},
	{"claude", regexp.MustCompile(`\bclaude\b[^\n|;&]*?\s(?:-p|--print)\b`)},
	{"hermes", regexp.MustCompile(`\bhermes\s+(?:chat|-z|-q|--query|--oneshot|run)\b`)},
	{"opencode", regexp.MustCompile(`\bopencode\s+run\b`)},
}

func launchedAgents(command string) []string {
	var out []string
	for _, l := range collabLaunchers {
		for _, loc := range l.re.FindAllStringIndex(command, -1) {
			rest := command[loc[1]:]
			if i := strings.IndexByte(rest, '\n'); i >= 0 {
				rest = rest[:i]
			}
			if !strings.Contains(rest, "--help") { // asking for help starts nothing
				out = append(out, l.agent)
				break
			}
		}
	}
	return out
}

func collabClip(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	cut := limit
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "\n…（已截断）"
}

func oneLine(s string, limit int) string {
	s = strings.Join(strings.Fields(s), " ")
	if utf8.RuneCountInString(s) <= limit {
		return s
	}
	return string([]rune(s)[:limit-1]) + "…"
}
