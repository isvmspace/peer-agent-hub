package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"testing"
)

func TestLaunchedAgents(t *testing.T) {
	cases := map[string][]string{
		`codex exec -s read-only "$(cat p.md)"`:            {"codex"},
		"claude -p --allowedTools Read < q":                {"claude"},
		"hermes chat -Q -q hi > a & opencode run hi":       {"hermes", "opencode"},
		"codex exec --help | head":                         nil,
		"which codex && codex --version":                   nil,
		"cat x | claude --print 'hi'\nopencode run --help": {"claude"},
	}
	for command, want := range cases {
		if got := launchedAgents(command); !reflect.DeepEqual(got, want) {
			t.Errorf("%q: got %v want %v", command, got, want)
		}
	}
}

func TestParseClaudeSubagent(t *testing.T) {
	main, err := parseClaude("testdata/collab/claude/projects/proj/s1.jsonl", false)
	if err != nil || main == nil {
		t.Fatal(err)
	}
	if main.ID != "s1" || main.Title != "评审方案" || main.Origin != "终端" || len(main.Turns) != 1 || main.Turns[0].Reply != "评审完成" {
		t.Fatalf("main: %+v", main)
	}
	// light parse keeps only the calls that start an agent
	if len(main.Calls) != 2 || !reflect.DeepEqual(main.Calls[0].Launches, []string{"codex"}) || main.Calls[0].End == nil ||
		!reflect.DeepEqual(main.Calls[1].Launches, []string{"claude"}) || main.Tools != 2 {
		t.Fatalf("calls: %+v", main.Calls)
	}
	sub, err := parseClaude("testdata/collab/claude/projects/proj/s1/subagents/agent-x.jsonl", false)
	if err != nil || sub == nil {
		t.Fatal(err)
	}
	if sub.ID != "s1/agent-x" || sub.Origin != "子代理" || sub.NativeParent != "claude:s1" || sub.NativeCall != "toolu_task" || sub.Title != "Sub review" {
		t.Fatalf("sub: %+v", sub)
	}
}

func TestParseCodexExec(t *testing.T) {
	s, err := parseCodex("testdata/collab/codex/sessions/2026/11/18/rollout-a.jsonl", true)
	if err != nil || s == nil {
		t.Fatal(err)
	}
	if s.ID != "cx1" || s.Origin != "exec" || s.Cwd != "/w" || len(s.Turns) != 1 || s.Turns[0].Prompt != "评审" || s.Turns[0].Reply != "没问题" {
		t.Fatalf("codex: %+v", s)
	}
	if len(s.Calls) != 1 || s.Calls[0].Command != "rg foo" || s.Calls[0].Result != "found" {
		t.Fatalf("detail calls: %+v", s.Calls)
	}
	hub, _ := parseCodex("testdata/collab/codex/sessions/2026/11/18/rollout-hub.jsonl", false)
	if hub.Origin != "Agent Hub #7" {
		t.Fatalf("hub origin: %q", hub.Origin)
	}
}

// collabFixtures points every source at testdata (and a Hermes database built here with the sqlite3 CLI).
func collabFixtures(t *testing.T) string {
	t.Helper()
	t.Setenv("CLAUDE_CONFIG_DIR", "testdata/collab/claude")
	t.Setenv("CODEX_HOME", "testdata/collab/codex")
	dir := t.TempDir()
	db := dir + "/state.db"
	schema := `CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL,
cwd TEXT, display_name TEXT, tool_call_count INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT,
tool_call_id TEXT, timestamp REAL, active INTEGER DEFAULT 1);
INSERT INTO sessions VALUES ('h1','oneshot',NULL,1795014007,1795014020,'/w',NULL,0);
INSERT INTO messages (session_id, role, content, timestamp) VALUES ('h1','user','评审',1795014007),('h1','assistant','好的',1795014019);`
	if out, err := exec.Command("sqlite3", db, schema).CombinedOutput(); err != nil {
		t.Fatalf("sqlite3: %v %s", err, out)
	}
	prevH, prevO := collabHermesDB, collabOpenCodeDB
	collabHermesDB, collabOpenCodeDB = func() string { return db }, func() string { return dir + "/missing.db" }
	t.Cleanup(func() { collabHermesDB, collabOpenCodeDB = prevH, prevO })
	return dir
}

func TestCollabIndexReadsAllSources(t *testing.T) {
	collabFixtures(t)
	x := newCollabIndex()
	got := x.refresh(3650)
	for _, key := range []string{"claude:s1", "claude:s1/agent-x", "codex:cx1", "codex:cxhub", "hermes:h1"} {
		if got[key] == nil {
			t.Errorf("missing %s (have %d)", key, len(got))
		}
	}
	if h := got["hermes:h1"]; h != nil && (h.Origin != "非交互（oneshot）" || len(h.Turns) != 1 || h.Turns[0].Reply != "好的") {
		t.Fatalf("hermes: %+v", h)
	}
	if d := x.detail("hermes:h1"); d == nil || d.Turns[0].Prompt != "评审" {
		t.Fatalf("hermes detail: %+v", d)
	}
}

func TestSQLiteFailureSkipsAgent(t *testing.T) {
	dir := collabFixtures(t)
	bad := dir + "/broken.db"
	_ = os.WriteFile(bad, []byte("not a database"), 0o600)
	collabHermesDB = func() string { return bad }
	if s := loadHermes(0); s != nil {
		t.Fatalf("a broken database must give nothing: %v", s)
	}
	got := newCollabIndex().refresh(3650)
	if got["claude:s1"] == nil || got["codex:cx1"] == nil {
		t.Fatal("the other agents must still be there")
	}
}

func TestParseCacheReused(t *testing.T) {
	collabFixtures(t)
	x := newCollabIndex()
	x.refresh(3650)
	first := x.parses
	x.built = 0 // force a rebuild: unchanged files come from the cache
	x.refresh(3650)
	if x.parses != first {
		t.Fatalf("unchanged files parsed again: %d → %d", first, x.parses)
	}
}

func sess(agent, id string, started float64, cwd, origin string, calls ...CollabCall) *CollabSession {
	if cwd == "" {
		cwd = "/w"
	}
	return &CollabSession{Agent: agent, ID: id, Started: started, Ended: started + 60, Cwd: cwd, Origin: origin, Calls: calls}
}

func f(v float64) *float64 { return &v }

func linked(sessions ...*CollabSession) map[string]*CollabSession {
	m := map[string]*CollabSession{}
	for _, s := range sessions {
		m[s.Key()] = s
	}
	collabLink(m)
	return m
}

func TestBackgroundLaunch(t *testing.T) {
	// one command starts three agents in the background: its result comes back at once
	call := CollabCall{ID: "t1", Tool: "Bash", TS: 100, End: f(100.2), Command: "(codex exec x) & (hermes chat -q x) & (opencode run x) & wait",
		Launches: []string{"codex", "hermes", "opencode"}}
	parent := sess("claude", "p", 50, "", "终端", call)
	kids := []*CollabSession{sess("codex", "c", 105, "", "exec"), sess("hermes", "h", 103, "", "非交互（oneshot）"), sess("opencode", "o", 104, "", "")}
	m := linked(append(kids, parent)...)
	for _, k := range kids {
		if k.Parent != "claude:p" || k.ParentCall != "t1" || k.Link != "inferred" {
			t.Errorf("%s: %q %q %q", k.Key(), k.Parent, k.ParentCall, k.Link)
		}
	}
	if edges := collabOverview(m)["edges"].([]map[string]any); len(edges) != 3 {
		t.Fatalf("edges: %v", edges)
	}
}

func TestNotLinked(t *testing.T) {
	call := CollabCall{ID: "t1", Tool: "Bash", TS: 100, End: f(200), Command: "codex exec x", Launches: []string{"codex"}}
	parent := sess("claude", "p", 50, "", "终端", call)
	others := []*CollabSession{
		sess("codex", "tui", 120, "", "终端"),           // someone opened Codex by hand meanwhile
		sess("codex", "far", 150, "/other", "exec"),   // another folder, well after the start
		sess("codex", "late", 500, "", "exec"),        // after the command ended
		sess("codex", "hub", 110, "", "Agent Hub #7"), // run by the Agent Hub connector
	}
	linked(append(others, parent)...)
	for _, s := range others {
		if s.Parent != "" {
			t.Errorf("%s must not be linked", s.Key())
		}
	}
}

func TestOneLaunchClaimsOne(t *testing.T) {
	call := CollabCall{ID: "t1", Tool: "Bash", TS: 100, End: f(300), Command: "codex exec x", Launches: []string{"codex"}}
	first, second := sess("codex", "a", 110, "", "exec"), sess("codex", "b", 120, "", "exec")
	linked(sess("claude", "p", 50, "", "终端", call), first, second)
	if first.Parent != "claude:p" || second.Parent != "" {
		t.Fatalf("%q %q", first.Parent, second.Parent)
	}
}

func TestNativeParentAndCall(t *testing.T) {
	parent := sess("claude", "p", 50, "", "终端", CollabCall{ID: "toolu_1", Tool: "Task", TS: 100, Launches: []string{"claude"}})
	sub := sess("claude", "p/agent-x", 101, "", "子代理")
	sub.NativeParent, sub.NativeCall = "claude:p", "toolu_1"
	hp := sess("hermes", "h", 150, "", "终端", CollabCall{ID: "d1", Tool: "delegate_task", TS: 200, Launches: []string{"hermes"}})
	hc := sess("hermes", "h2", 201, "", "子代理")
	hc.NativeParent = "hermes:h"
	linked(parent, sub, hp, hc)
	if sub.Parent != "claude:p" || sub.ParentCall != "toolu_1" || sub.Link != "native" || hc.Parent != "hermes:h" || hc.ParentCall != "d1" {
		t.Fatalf("sub %+v hc %+v", sub, hc)
	}
}

func TestSessionPaging(t *testing.T) {
	s := sess("claude", "p", 0, "", "终端")
	for i := 0; i < 25; i++ {
		s.Turns = append(s.Turns, CollabTurn{TS: float64(100 + i*10), Prompt: fmt.Sprint("q", i)})
	}
	s.Calls = []CollabCall{{ID: "c1", Tool: "Bash", TS: 101, Launches: []string{"codex"}}, {ID: "c2", Tool: "Read", TS: 345}}
	child := sess("codex", "k", 102, "", "exec")
	child.Parent, child.ParentCall = s.Key(), "c1"
	all := map[string]*CollabSession{s.Key(): s, child.Key(): child}
	page := collabDetail(s, all, -1)
	if page["start"] != 5 || page["end"] != 25 || len(page["turns"].([]map[string]any)) != 20 {
		t.Fatalf("default page: %v %v", page["start"], page["end"])
	}
	if calls := page["calls"].([]map[string]any); len(calls) != 1 || calls[0]["id"] != "c2" {
		t.Fatalf("calls on the latest page: %v", calls)
	}
	first := collabDetail(s, all, 0)
	launches := first["launches"].([]map[string]any)
	if first["start"] != 0 || first["end"] != 20 || len(launches) != 1 || launches[0]["turn"] != 0 {
		t.Fatalf("first page: %v %v %v", first["start"], first["end"], launches)
	}
}

func TestCollabRedacts(t *testing.T) {
	s := sess("claude", "p", 0, "", "终端")
	s.Turns = []CollabTurn{{TS: 1, Prompt: "登录用 password=abc123def456", Reply: "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"}}
	page := collabDetail(s, map[string]*CollabSession{s.Key(): s}, -1)
	turn := page["turns"].([]map[string]any)[0]
	if strings.Contains(turn["prompt"].(string), "abc123def456") || strings.Contains(turn["reply"].(string), "ghp_abcdef") {
		t.Fatalf("not masked: %v", turn)
	}
}

func TestCollabDetailListsNeverNull(t *testing.T) {
	s := sess("claude", "p", 0, "", "终端", CollabCall{ID: "c", Tool: "Read", TS: 1})
	s.Turns = []CollabTurn{{TS: 0, Prompt: "q"}}
	b, _ := json.Marshal(collabDetail(s, map[string]*CollabSession{s.Key(): s}, -1))
	if strings.Contains(string(b), `"launches":null`) {
		t.Fatalf("a list went out as null: %s", b)
	}
}

// Review #2: a call that reads credentials goes out hidden, like the existing session viewer does.
func TestCollabHidesCredentialCalls(t *testing.T) {
	s := sess("claude", "p", 0, "", "终端", CollabCall{ID: "c", Tool: "Bash", TS: 1, Summary: "cat ~/.aws/credentials",
		Command: "cat ~/.aws/credentials", Result: "AKIAEXAMPLE\nwJalrXUtnFEMIbareSecretLine"})
	s.Turns = []CollabTurn{{TS: 0, Prompt: "q"}}
	call := collabDetail(s, map[string]*CollabSession{s.Key(): s}, -1)["calls"].([]map[string]any)[0]
	if strings.Contains(call["result"].(string), "wJalrXUtnFEMI") || call["result"] != hiddenNotice {
		t.Fatalf("credential output leaked: %v", call["result"])
	}
}

// Review #1: overview and detail requests running at once must not race on the shared sessions.
func TestCollabConcurrentRequests(t *testing.T) {
	collabFixtures(t)
	x := newCollabIndex()
	done := make(chan bool)
	for i := 0; i < 8; i++ {
		go func(i int) {
			defer func() { done <- true }()
			for j := 0; j < 20; j++ {
				x.mu.Lock()
				x.built = 0 // force rebuilds that relink while others read
				x.mu.Unlock()
				if i%2 == 0 {
					x.overview(3650)
				} else {
					x.page("claude:s1", 3650, -1)
				}
			}
		}(i)
	}
	for i := 0; i < 8; i++ {
		<-done
	}
	if p, ok := x.page("claude:s1", 3650, -1); !ok || len(p["launches"].([]map[string]any)) == 0 {
		t.Fatalf("detail after concurrent use: %v", p)
	}
}

// Review #3: paging through a session does not parse its log again while it is unchanged.
func TestCollabDetailCached(t *testing.T) {
	collabFixtures(t)
	x := newCollabIndex()
	x.page("claude:s1", 3650, -1)
	before := x.parses
	x.page("claude:s1", 3650, 0)
	if x.parses != before {
		t.Fatalf("detail parsed again: %d → %d", before, x.parses)
	}
}

// incremental fixture: a copy of the Claude fixture in a temp CLAUDE_CONFIG_DIR that tests can append to
func incrementalFixture(t *testing.T) (*collabIndex, string) {
	collabFixtures(t)
	dir := t.TempDir()
	if err := os.MkdirAll(dir+"/projects/proj", 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile("testdata/collab/claude/projects/proj/s1.jsonl")
	path := dir + "/projects/proj/s1.jsonl"
	_ = os.WriteFile(path, data, 0o600)
	t.Setenv("CLAUDE_CONFIG_DIR", dir)
	return newCollabIndex(), path
}

func appendLine(t *testing.T, path, line string) {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = f.WriteString(line)
	_ = f.Close()
}

func refreshNow(x *collabIndex) map[string]*CollabSession {
	x.mu.Lock()
	x.built = 0
	x.mu.Unlock()
	return x.refresh(3650)
}

const lateLaunch = `{"type":"assistant","sessionId":"s1","cwd":"/w","timestamp":"2026-11-18T15:02:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_late","name":"Bash","input":{"command":"hermes chat -q hi"}}]}}` + "\n"

func TestIncrementalClaude(t *testing.T) {
	x, path := incrementalFixture(t)
	if s := refreshNow(x)["claude:s1"]; s == nil || len(s.Calls) != 2 {
		t.Fatalf("first read: %+v", s)
	}
	full := x.fullParses
	appendLine(t, path, lateLaunch)
	s := refreshNow(x)["claude:s1"]
	if len(s.Calls) != 3 || s.Calls[2].ID != "toolu_late" || x.fullParses != full {
		t.Fatalf("appended call missing or file reread: calls=%d full %d→%d", len(s.Calls), full, x.fullParses)
	}
	if s.Title != "评审方案" || len(s.Turns) != 1 {
		t.Fatalf("state lost across reads: %+v", s)
	}
}

func TestIncrementalPartialLine(t *testing.T) {
	x, path := incrementalFixture(t)
	refreshNow(x)
	appendLine(t, path, lateLaunch[:60]) // the agent is half way through writing the line
	if s := refreshNow(x)["claude:s1"]; len(s.Calls) != 2 {
		t.Fatalf("half a line was parsed: %d calls", len(s.Calls))
	}
	appendLine(t, path, lateLaunch[60:])
	if s := refreshNow(x)["claude:s1"]; len(s.Calls) != 3 {
		t.Fatalf("the completed line was lost: %d calls", len(s.Calls))
	}
}

func TestIncrementalRestartsOnShrink(t *testing.T) {
	x, path := incrementalFixture(t)
	refreshNow(x)
	data, _ := os.ReadFile(path)
	lines := strings.SplitAfter(string(data), "\n")
	_ = os.WriteFile(path, []byte(strings.Join(lines[:3], "")), 0o600) // rewritten shorter: title, prompt, one call
	s := refreshNow(x)["claude:s1"]
	if s == nil || len(s.Calls) != 1 || len(s.Turns) != 1 || s.Turns[0].Reply != "" {
		t.Fatalf("stale state kept after the log shrank: %+v", s)
	}
}

func TestOverviewLiveFields(t *testing.T) {
	now := nowSeconds()
	parent := sess("claude", "p", now-600, "", "终端",
		CollabCall{ID: "t1", Tool: "Bash", TS: now - 300, Summary: "评审", Command: "codex exec x", Launches: []string{"codex"}},
		CollabCall{ID: "task", Tool: "Task", TS: now - 100, Summary: "复核", Launches: []string{"claude"}})
	parent.Ended = now - 5
	done := sess("codex", "c", now-299, "", "exec")
	done.Ended, done.Parent, done.ParentCall = now-200, parent.Key(), "t1"
	done.Turns = []CollabTurn{{TS: now - 299, Prompt: "评审", Reply: "没问题\n细节…"}}
	sub := sess("claude", "p/agent-x", now-99, "", "子代理")
	sub.Ended, sub.Parent, sub.ParentCall = now-10, parent.Key(), "task"
	o := collabOverview(map[string]*CollabSession{parent.Key(): parent, done.Key(): done, sub.Key(): sub})
	briefs := map[string]map[string]any{}
	for _, b := range o["sessions"].([]map[string]any) {
		briefs[b["key"].(string)] = b
	}
	if briefs["claude:p"]["active"] != true || briefs["codex:c"]["active"] != false || briefs["claude:p/agent-x"]["active"] != true {
		t.Fatalf("active flags: %v", briefs)
	}
	edges := map[string]bool{}
	for _, e := range o["edges"].([]map[string]any) {
		edges[e["from"].(string)+"→"+e["to"].(string)] = true
	}
	if !edges["claude→codex"] || !edges["claude→subagent"] || edges["claude→claude"] {
		t.Fatalf("edges: %v", edges)
	}
	nodes := o["nodes"].(map[string]map[string]int)
	if nodes["subagent"]["active"] != 1 || nodes["claude"]["sessions"] != 1 {
		t.Fatalf("nodes: %v", nodes)
	}
	events := o["events"].([]map[string]any)
	if len(events) != 3 || events[0]["kind"] != "launch" || events[0]["summary"] != "复核" || events[1]["kind"] != "reply" ||
		events[1]["summary"] != "没问题" || events[2]["kind"] != "launch" {
		t.Fatalf("events newest first: %v", events)
	}
}

func TestDiscussionRounds(t *testing.T) {
	parent := sess("claude", "p", 0, "", "终端",
		CollabCall{ID: "r1", Tool: "Bash", TS: 100, Launches: []string{"codex", "hermes"}},
		CollabCall{ID: "r2", Tool: "Bash", TS: 500, Launches: []string{"codex"}})
	a := sess("codex", "a", 101, "", "exec")
	a.Ended, a.Parent, a.ParentCall = 300, parent.Key(), "r1"
	a.Turns = []CollabTurn{{TS: 101, Prompt: "第一轮问题", Reply: "codex 观点"}}
	h := sess("hermes", "h", 102, "", "非交互（oneshot）")
	h.Ended, h.Parent, h.ParentCall = 200, parent.Key(), "r1"
	h.Turns = []CollabTurn{{TS: 102, Prompt: "第一轮问题", Reply: "hermes 观点"}}
	b := sess("codex", "b", 501, "", "exec")
	b.Ended, b.Parent, b.ParentCall = 600, parent.Key(), "r2"
	b.Turns = []CollabTurn{{TS: 501, Prompt: "第二轮问题"}}
	d := collabDiscussion(parent, map[string]*CollabSession{parent.Key(): parent, a.Key(): a, h.Key(): h, b.Key(): b})
	rounds := d["rounds"].([]map[string]any)
	if len(rounds) != 2 || rounds[0]["prompt"] != "第一轮问题" || rounds[1]["prompt"] != "第二轮问题" {
		t.Fatalf("rounds: %v", rounds)
	}
	replies := rounds[0]["replies"].([]map[string]any)
	if len(replies) != 2 || replies[0]["agent"] != "hermes" || replies[0]["reply"] != "hermes 观点" || replies[1]["agent"] != "codex" {
		t.Fatalf("replies by finish time: %v", replies)
	}
}

func TestDiscussionEmpty(t *testing.T) {
	s := sess("claude", "p", 0, "", "终端")
	d := collabDiscussion(s, map[string]*CollabSession{s.Key(): s})
	if rounds := d["rounds"].([]map[string]any); len(rounds) != 0 {
		t.Fatalf("rounds: %v", rounds)
	}
}

func TestHermesReadsOnIncrementally(t *testing.T) {
	collabFixtures(t)
	db := collabHermesDB()
	src := &hermesSource{}
	first := src.load(0)
	if len(first) != 1 || len(first[0].Turns) != 1 {
		t.Fatalf("first read: %+v", first)
	}
	sql := `INSERT INTO messages (session_id, role, content, timestamp) VALUES ('h1','user','第二问',1795014030),('h1','assistant','第二答',1795014031);
INSERT INTO sessions VALUES ('h2','oneshot',NULL,1795014040,1795014050,'/w',NULL,0);
INSERT INTO messages (session_id, role, content, timestamp) VALUES ('h2','user','新会话',1795014040);`
	if out, err := exec.Command("sqlite3", db, sql).CombinedOutput(); err != nil {
		t.Fatalf("%v %s", err, out)
	}
	got := map[string]*CollabSession{}
	for _, s := range src.load(0) {
		got[s.ID] = s
	}
	if h1 := got["h1"]; h1 == nil || len(h1.Turns) != 2 || h1.Turns[1].Reply != "第二答" || h1.Turns[0].Reply != "好的" {
		t.Fatalf("h1 after new messages: %+v", h1)
	}
	if h2 := got["h2"]; h2 == nil || len(h2.Turns) != 1 || h2.Turns[0].Prompt != "新会话" {
		t.Fatalf("new session: %+v", got["h2"])
	}
}

// Review (live) #1: OpenCode rewrites a turn's row as it runs; a later poll must see the finished reply and the
// launch added to it, not only rows with a higher rowid.
func TestOpenCodeRewrittenRowsAreReadAgain(t *testing.T) {
	collabFixtures(t)
	db := t.TempDir() + "/opencode.db"
	setup := `CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT);
INSERT INTO session_v2 VALUES ('o1', NULL, '/w', '评审', 1795014000000, 1795014001000);
INSERT INTO session_message VALUES ('m1','o1','user',1,1795014000000,'{"text":"评审一下"}');
INSERT INTO session_message VALUES ('m2','o1','assistant',2,1795014001000,'{"content":[{"type":"text","text":"我先看"}]}');`
	if out, err := exec.Command("sqlite3", db, setup).CombinedOutput(); err != nil {
		t.Fatalf("%v %s", err, out)
	}
	collabOpenCodeDB = func() string { return db }
	src := &opencodeSource{}
	if s := src.load(0); len(s) != 1 || s[0].Turns[0].Reply != "我先看" {
		t.Fatalf("first read: %+v", s)
	}
	update := `UPDATE session_message SET data = '{"content":[{"type":"tool","id":"t1","name":"bash","state":{"input":{"command":"codex exec x"}}},{"type":"text","text":"评审完了"}]}' WHERE id = 'm2';
UPDATE session_v2 SET time_updated = 1795014009000 WHERE id = 'o1';`
	if out, err := exec.Command("sqlite3", db, update).CombinedOutput(); err != nil {
		t.Fatalf("%v %s", err, out)
	}
	s := src.load(0)[0]
	if len(s.Turns) != 1 || s.Turns[0].Reply != "评审完了" || len(s.Calls) != 1 || s.Calls[0].Launches[0] != "codex" {
		t.Fatalf("rewritten row not read again: turns %+v calls %+v", s.Turns, s.Calls)
	}
}

// Review (live) #3: redaction runs before the reply is cut to one line, so a token across the cut is still masked.
func TestReplyEventRedactedBeforeClipping(t *testing.T) {
	now := nowSeconds()
	parent := sess("claude", "p", now-600, "", "终端", CollabCall{ID: "t", Tool: "Bash", TS: now - 500, Command: "codex exec x", Launches: []string{"codex"}})
	child := sess("codex", "c", now-499, "", "exec")
	child.Ended, child.Parent, child.ParentCall = now-400, parent.Key(), "t"
	child.Turns = []CollabTurn{{TS: now - 499, Prompt: "q", Reply: strings.Repeat("字", 108) + " sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"}}
	for _, ev := range collabOverview(map[string]*CollabSession{parent.Key(): parent, child.Key(): child})["events"].([]map[string]any) {
		if ev["kind"] == "reply" && strings.Contains(ev["summary"].(string), "sk-proj") {
			t.Fatalf("token survived clipping: %v", ev["summary"])
		}
	}
}

// Review (live) #4: edges with equal counts come out in the same order every poll.
func TestEdgeOrderStable(t *testing.T) {
	parent := sess("claude", "p", 0, "", "终端", CollabCall{ID: "t", Tool: "Bash", TS: 1, Command: "codex exec a; hermes chat -q b; opencode run c",
		Launches: []string{"codex", "hermes", "opencode"}})
	m := map[string]*CollabSession{parent.Key(): parent}
	for _, k := range []*CollabSession{sess("codex", "c", 2, "", "exec"), sess("hermes", "h", 2, "", "非交互（oneshot）"), sess("opencode", "o", 2, "", "")} {
		k.Parent, k.ParentCall = parent.Key(), "t"
		m[k.Key()] = k
	}
	first := fmt.Sprint(collabOverview(m)["edges"])
	for i := 0; i < 20; i++ {
		if got := fmt.Sprint(collabOverview(m)["edges"]); got != first {
			t.Fatalf("edge order changed: %s vs %s", first, got)
		}
	}
}

// Review (live) #5: a log replaced by another file (atomic rename) is read from the start even when it is larger.
func TestReplacedLogReadFromStart(t *testing.T) {
	x, path := incrementalFixture(t)
	refreshNow(x)
	full := x.fullParses
	data, _ := os.ReadFile(path)
	tmp := path + ".new"
	_ = os.WriteFile(tmp, append(data, []byte(lateLaunch)...), 0o600)
	if err := os.Rename(tmp, path); err != nil {
		t.Fatal(err)
	}
	s := refreshNow(x)["claude:s1"]
	if x.fullParses != full+1 || len(s.Calls) != 3 || len(s.Turns) != 1 {
		t.Fatalf("replaced log not reread: full %d→%d calls %d turns %d", full, x.fullParses, len(s.Calls), len(s.Turns))
	}
}

// Review (live) #6: when reading a new session's history fails, the next poll tries again.
func TestFailedHistoryRetried(t *testing.T) {
	d := &dbSessions{}
	list := []map[string]any{{"id": "a"}}
	build := func(r map[string]any) *CollabSession { return &CollabSession{Agent: "hermes", ID: str(r["id"])} }
	feed := func(ss map[string]*CollabSession, rows []map[string]any) {
		for _, r := range rows {
			s := ss[str(r["session_id"])]
			s.Turns = append(s.Turns, CollabTurn{Prompt: str(r["content"])})
		}
	}
	d.sync(0, list, build, func(*CollabSession, map[string]any) bool { return false },
		func(map[string]*CollabSession, string) ([]map[string]any, error) {
			return []map[string]any{{"id": 5.0, "session_id": "a", "content": "第一条"}}, nil
		}, feed)
	list = append(list, map[string]any{"id": "b"}) // b enters the window; reading its history fails once
	fail := true
	messages := func(ss map[string]*CollabSession, cond string) ([]map[string]any, error) {
		if strings.HasPrefix(cond, "id <=") {
			if fail {
				return nil, fmt.Errorf("database is locked")
			}
			return []map[string]any{{"id": 3.0, "session_id": "b", "content": "b 的旧消息"}}, nil
		}
		return nil, nil
	}
	d.sync(0, list, build, func(*CollabSession, map[string]any) bool { return false }, messages, feed)
	fail = false
	got := d.sync(0, list, build, func(*CollabSession, map[string]any) bool { return false }, messages, feed)
	for _, s := range got {
		if s.ID == "b" && (len(s.Turns) != 1 || s.Turns[0].Prompt != "b 的旧消息") {
			t.Fatalf("history of b not read on retry: %+v", s.Turns)
		}
	}
}

func TestCallSummaryMaskedBeforeCut(t *testing.T) {
	summary, _ := claudeCallSummary("Bash", map[string]any{"command": strings.Repeat("x", 110) + " sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"})
	if strings.Contains(summary, "sk-proj") {
		t.Fatalf("token survived: %s", summary)
	}
}

// A session the user renamed (/rename) shows that name, even when it is written after the automatic title.
func TestRenamedSessionTitle(t *testing.T) {
	x, path := incrementalFixture(t)
	if s := refreshNow(x)["claude:s1"]; s.Title != "评审方案" {
		t.Fatalf("before rename: %q", s.Title)
	}
	appendLine(t, path, `{"type":"custom-title","customTitle":"方案分析排查","sessionId":"s1"}`+"\n")
	appendLine(t, path, `{"type":"ai-title","aiTitle":"自动标题","sessionId":"s1"}`+"\n")
	if s := refreshNow(x)["claude:s1"]; s.Title != "方案分析排查" {
		t.Fatalf("renamed title not shown: %q", s.Title)
	}
}

// Codex keeps the names of its threads (renames included, the latest line wins) in session_index.jsonl.
func TestCodexThreadName(t *testing.T) {
	collabFixtures(t)
	home := t.TempDir()
	_ = os.MkdirAll(home+"/sessions/2026/11/18", 0o755)
	data, _ := os.ReadFile("testdata/collab/codex/sessions/2026/11/18/rollout-a.jsonl")
	_ = os.WriteFile(home+"/sessions/2026/11/18/rollout-a.jsonl", data, 0o600)
	_ = os.WriteFile(home+"/session_index.jsonl", []byte(`{"id":"cx1","thread_name":"旧名字"}`+"\n"+`{"id":"cx1","thread_name":"评审 Codex"}`+"\n"), 0o600)
	t.Setenv("CODEX_HOME", home)
	if s := newCollabIndex().refresh(3650)["codex:cx1"]; s == nil || s.Title != "评审 Codex" {
		t.Fatalf("thread name not used: %+v", s)
	}
}

// Claude Code's own notices (a background task finished) are not the person's questions.
func TestClaudeTaskNotificationNotATurn(t *testing.T) {
	x, path := incrementalFixture(t)
	refreshNow(x)
	appendLine(t, path, `{"type":"user","sessionId":"s1","cwd":"/w","timestamp":"2026-11-18T15:03:00.000Z","message":{"role":"user","content":"<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>"}}`+"\n")
	if s := refreshNow(x)["claude:s1"]; len(s.Turns) != 1 {
		t.Fatalf("a task notification became a turn: %d turns", len(s.Turns))
	}
}

// Opening a long session again after it grew reads only what was appended, not the whole log.
func TestDetailReadOnIncrementally(t *testing.T) {
	x, path := incrementalFixture(t)
	refreshNow(x)
	before := x.fullParses
	if p, ok := x.page("claude:s1", 3650, -1); !ok || len(p["calls"].([]map[string]any)) != 2 || x.fullParses != before+1 {
		t.Fatalf("first page should be one full read (%d→%d): %v", before, x.fullParses, p)
	}
	full := x.fullParses
	appendLine(t, path, lateLaunch)
	refreshNow(x)
	p, _ := x.page("claude:s1", 3650, -1)
	if len(p["calls"].([]map[string]any)) != 3 || x.fullParses != full {
		t.Fatalf("detail reread from the start (full %d→%d) or missed the new call: %d", full, x.fullParses, len(p["calls"].([]map[string]any)))
	}
}

// Hermes hands out several tasks in one delegate_task call; its summary is their goals, not raw JSON.
func TestHermesDelegateSummary(t *testing.T) {
	row := map[string]any{"timestamp": 1.0, "tool_calls": `[{"id":"d1","function":{"name":"delegate_task","arguments":"{\"tasks\":[{\"goal\":\"复核 v10 设计\",\"context\":\"文件：x\"},{\"goal\":\"查 K5 去重\"}]}"}}]`}
	calls := hermesCalls(row, false)
	if len(calls) != 1 || calls[0].Summary != "复核 v10 设计；查 K5 去重" || calls[0].Launches[0] != "hermes" {
		t.Fatalf("summary: %+v", calls)
	}
}
