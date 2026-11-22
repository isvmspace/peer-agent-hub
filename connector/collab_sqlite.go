package main

// Hermes (~/.hermes/state.db) and OpenCode (~/.local/share/opencode/opencode.db) keep their sessions in SQLite.
// They are read with the system's sqlite3 command in read-only mode rather than a SQLite library in the connector;
// where sqlite3 is missing or the database can't be read, that agent is simply absent.

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

var (
	collabHermesDB = func() string {
		return filepath.Join(homeDir("HERMES_HOME", ".hermes"), "state.db")
	}
	collabOpenCodeDB = func() string {
		home, _ := os.UserHomeDir()
		return filepath.Join(home, ".local", "share", "opencode", "opencode.db")
	}
	hermesOrigins = map[string]string{"cli": "终端", "oneshot": "非交互（oneshot）", "subagent": "子代理", "curator": "后台整理"}
)

func sqlQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// sqliteRows runs one read-only query; rows come back as JSON objects.
func sqliteRows(db, query string) ([]map[string]any, error) {
	if _, err := os.Stat(db); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "sqlite3", "-readonly", "-json", db, query).Output()
	if err != nil {
		return nil, err
	}
	var rows []map[string]any
	if len(strings.TrimSpace(string(out))) == 0 {
		return rows, nil
	}
	return rows, json.Unmarshal(out, &rows)
}

// dbVersion changes whenever the agent writes (its WAL file moves first).
func dbVersion(db string) float64 {
	v := 0.0
	for _, p := range []string{db, db + "-wal"} {
		if info, err := os.Stat(p); err == nil && float64(info.ModTime().UnixNano())/1e9 > v {
			v = float64(info.ModTime().UnixNano()) / 1e9
		}
	}
	return v
}

func str(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case nil:
		return ""
	}
	return fmt.Sprint(v)
}

func num(v any) float64 {
	f, _ := v.(float64)
	return f
}

// ---- Hermes ------------------------------------------------------------------------------

func hermesSession(r map[string]any) *CollabSession {
	origin, ok := hermesOrigins[str(r["source"])]
	if !ok {
		origin = str(r["source"])
	}
	s := &CollabSession{Agent: "hermes", ID: str(r["id"]), Started: num(r["started_at"]), Ended: num(r["ended_at"]),
		Cwd: str(r["cwd"]), Title: str(r["display_name"]), Origin: origin, Tools: int(num(r["tool_call_count"])),
		Source: collabHermesDB()}
	if s.Ended == 0 {
		s.Ended = s.Started
	}
	if p := str(r["parent_session_id"]); p != "" {
		s.NativeParent = "hermes:" + p
	}
	return s
}

func hermesCalls(r map[string]any, detail bool) []CollabCall {
	var items []struct {
		ID       string `json:"id"`
		CallID   string `json:"call_id"`
		Name     string `json:"name"`
		Function struct {
			Name      string `json:"name"`
			Arguments string `json:"arguments"`
		} `json:"function"`
	}
	if json.Unmarshal([]byte(str(r["tool_calls"])), &items) != nil {
		return nil
	}
	var out []CollabCall
	for _, it := range items {
		name := it.Function.Name
		if name == "" {
			name = it.Name
		}
		var args map[string]any
		_ = json.Unmarshal([]byte(it.Function.Arguments), &args)
		command := str(firstNonNil(args["command"], args["cmd"], args["code"]))
		launches := launchedAgents(command)
		if name == "delegate_task" {
			command = str(firstNonNil(args["task"], args["goal"], args["prompt"]))
			if tasks, ok := args["tasks"].([]any); ok { // several tasks in one call: their goals
				var goals []string
				for _, t := range tasks {
					if m, ok := t.(map[string]any); ok {
						if g := str(firstNonNil(m["goal"], m["task"], m["prompt"])); g != "" {
							goals = append(goals, g)
						}
					}
				}
				command = strings.Join(goals, "；")
			}
			launches = []string{"hermes"}
		}
		if detail || len(launches) > 0 {
			id := it.ID
			if id == "" {
				id = it.CallID
			}
			summary := command
			if summary == "" && args != nil {
				b, _ := json.Marshal(args)
				summary = string(b)
			}
			out = append(out, CollabCall{ID: id, Tool: name, TS: num(r["timestamp"]), Summary: oneLine(redactTool(summary), 120),
				Command: collabClip(command, collabMaxCommand), Launches: launches})
		}
	}
	return out
}

func quotedIDs(sessions map[string]*CollabSession) string {
	ids := make([]string, 0, len(sessions))
	for id := range sessions {
		ids = append(ids, sqlQuote(id))
	}
	return strings.Join(ids, ",")
}

// hermesMessages reads messages of the given sessions matching `cond`; the overview leaves tool output out (it can be
// huge and only the detail view shows it).
func hermesMessages(sessions map[string]*CollabSession, cond string, detail bool) ([]map[string]any, error) {
	content := "CASE WHEN role = 'tool' THEN '' ELSE content END"
	if detail {
		content = "content"
	}
	return sqliteRows(collabHermesDB(), "SELECT id, session_id, role, "+content+" AS content, tool_calls, tool_call_id, timestamp "+
		"FROM messages WHERE session_id IN ("+quotedIDs(sessions)+") AND active = 1 AND "+cond+" ORDER BY id")
}

func hermesFill(sessions map[string]*CollabSession, detail bool) {
	rows, err := hermesMessages(sessions, "1 = 1", detail)
	if err == nil {
		hermesFeed(sessions, rows, detail)
	}
}

func findCallPtr(s *CollabSession, id string) *CollabCall {
	for i := len(s.Calls) - 1; i >= 0; i-- {
		if s.Calls[i].ID == id {
			return &s.Calls[i]
		}
	}
	return nil
}

// hermesFeed adds messages, in order, to the sessions they belong to.
func hermesFeed(sessions map[string]*CollabSession, rows []map[string]any, detail bool) {
	for _, r := range rows {
		s := sessions[str(r["session_id"])]
		if s == nil {
			continue
		}
		when := num(r["timestamp"])
		if when > s.Ended {
			s.Ended = when
		}
		content := str(r["content"])
		switch str(r["role"]) {
		case "user":
			if strings.TrimSpace(content) != "" {
				s.Turns = append(s.Turns, CollabTurn{TS: when, Prompt: collabClip(content, collabMaxText)})
			}
		case "assistant":
			s.Calls = append(s.Calls, hermesCalls(r, detail)...)
			if strings.TrimSpace(content) != "" && len(s.Turns) > 0 {
				s.Turns[len(s.Turns)-1].Reply = collabClip(content, collabMaxText)
			}
		case "tool":
			if c := findCallPtr(s, str(r["tool_call_id"])); c != nil {
				end := when
				c.End = &end
				if detail {
					c.Result = collabClip(content, collabMaxResult)
				}
			}
		}
	}
	for _, s := range sessions {
		if s.Title == "" && len(s.Turns) > 0 {
			s.Title = oneLine(s.Turns[0].Prompt, 80)
		}
	}
}

func loadHermes(since float64) []*CollabSession { return (&hermesSource{}).load(since) }

func hermesDetail(id string) *CollabSession {
	rows, err := sqliteRows(collabHermesDB(), "SELECT * FROM sessions WHERE id = "+sqlQuote(id))
	if err != nil || len(rows) == 0 {
		return nil
	}
	s := hermesSession(rows[0])
	hermesFill(map[string]*CollabSession{s.ID: s}, true)
	return s
}

// ---- OpenCode ----------------------------------------------------------------------------

func opencodeSession(r map[string]any) *CollabSession {
	s := &CollabSession{Agent: "opencode", ID: str(r["id"]), Started: num(r["time_created"]) / 1000,
		Ended: num(r["time_updated"]) / 1000, Cwd: str(r["directory"]), Title: str(r["title"]), Source: collabOpenCodeDB()}
	if p := str(r["parent_id"]); p != "" {
		s.NativeParent, s.Origin = "opencode:"+p, "子代理"
	}
	return s
}

// opencodeMessages reads messages of the given sessions matching `cond`. SQLite itself picks out the few fields this
// view uses (text, tool names and commands, cut to size): the full rows carry every tool's output and are many
// megabytes, which the sqlite3 command takes seconds to print as JSON.
func opencodeMessages(sessions map[string]*CollabSession, cond string, detail bool) ([]map[string]any, error) {
	output := ""
	if detail {
		output = ", 'output', substr(json_extract(c.value, '$.state.output'), 1, 3000)"
	}
	slim := `json_object('text', substr(json_extract(m.data, '$.text'), 1, 6000),
  'time', json_object('completed', json_extract(m.data, '$.time.completed')),
  'content', (SELECT json_group_array(json_object('type', json_extract(c.value, '$.type'),
      'text', substr(json_extract(c.value, '$.text'), 1, 6000), 'id', json_extract(c.value, '$.id'), 'name', json_extract(c.value, '$.name'),
      'state', json_object('input', json_object('command', json_extract(c.value, '$.state.input.command'),
        'prompt', json_extract(c.value, '$.state.input.prompt'), 'description', json_extract(c.value, '$.state.input.description'))` + output + `)))
    FROM json_each(m.data, '$.content') c WHERE json_extract(c.value, '$.type') IN ('text', 'tool')))`
	return sqliteRows(collabOpenCodeDB(), "SELECT m.rowid AS id, m.session_id, m.type, m.time_created, "+slim+" AS data "+
		"FROM session_message m WHERE m.session_id IN ("+quotedIDs(sessions)+") AND m.type IN ('user', 'assistant') AND "+
		strings.ReplaceAll(cond, "id", "m.rowid")+" ORDER BY m.session_id, m.seq")
}

func opencodeFill(sessions map[string]*CollabSession, detail bool) {
	rows, err := opencodeMessages(sessions, "1 = 1", detail)
	if err == nil {
		opencodeFeed(sessions, rows, detail)
	}
}

func opencodeFeed(sessions map[string]*CollabSession, rows []map[string]any, detail bool) {
	for _, r := range rows {
		s := sessions[str(r["session_id"])]
		if s == nil {
			continue
		}
		when := num(r["time_created"]) / 1000
		var data struct {
			Text string `json:"text"`
			Time struct {
				Completed float64 `json:"completed"`
			} `json:"time"`
			Content []struct {
				Type  string `json:"type"`
				Text  string `json:"text"`
				ID    string `json:"id"`
				Name  string `json:"name"`
				State struct {
					Input  map[string]any `json:"input"`
					Output any            `json:"output"`
				} `json:"state"`
			} `json:"content"`
		}
		if json.Unmarshal([]byte(str(r["data"])), &data) != nil {
			continue
		}
		switch str(r["type"]) {
		case "user":
			if strings.TrimSpace(data.Text) != "" {
				s.Turns = append(s.Turns, CollabTurn{TS: when, Prompt: collabClip(data.Text, collabMaxText)})
			}
		case "assistant":
			for _, item := range data.Content {
				switch item.Type {
				case "text":
					if strings.TrimSpace(item.Text) != "" && len(s.Turns) > 0 {
						s.Turns[len(s.Turns)-1].Reply = collabClip(item.Text, collabMaxText)
					}
				case "tool":
					s.Tools++
					command := str(item.State.Input["command"])
					launches := launchedAgents(command)
					if item.Name == "task" {
						command = str(firstNonNil(item.State.Input["prompt"], item.State.Input["description"]))
						launches = []string{"opencode"}
					}
					if detail || len(launches) > 0 {
						summary := command
						if summary == "" {
							b, _ := json.Marshal(item.State.Input)
							summary = string(b)
						}
						call := CollabCall{ID: item.ID, Tool: item.Name, TS: when, Summary: oneLine(redactTool(summary), 120),
							Command: collabClip(command, collabMaxCommand), Launches: launches}
						if data.Time.Completed > 0 {
							end := data.Time.Completed / 1000
							call.End = &end
						}
						if detail {
							out, ok := item.State.Output.(string)
							if !ok && item.State.Output != nil {
								b, _ := json.Marshal(item.State.Output)
								out = string(b)
							}
							call.Result = collabClip(out, collabMaxResult)
						}
						s.Calls = append(s.Calls, call)
					}
				}
			}
		}
	}
	for _, s := range sessions {
		if s.Title == "" && len(s.Turns) > 0 {
			s.Title = oneLine(s.Turns[0].Prompt, 80)
		}
	}
}

func loadOpenCode(since float64) []*CollabSession { return (&opencodeSource{}).load(since) }

func opencodeDetail(id string) *CollabSession {
	rows, err := sqliteRows(collabOpenCodeDB(), "SELECT * FROM session_v2 WHERE id = "+sqlQuote(id))
	if err != nil || len(rows) == 0 {
		return nil
	}
	s := opencodeSession(rows[0])
	opencodeFill(map[string]*CollabSession{s.ID: s}, true)
	return s
}
