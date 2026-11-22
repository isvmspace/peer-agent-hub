//go:build !windows

package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

func sinkCollector() (context.Context, *[]ToolEvent) {
	var events []ToolEvent
	var mu sync.Mutex
	ctx := withToolSink(context.Background(), func(ev ToolEvent) {
		mu.Lock()
		events = append(events, ev)
		mu.Unlock()
	})
	return ctx, &events
}

func TestClaudeToolLifecycleAndUsage(t *testing.T) {
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"system","subtype":"init","session_id":"s","model":"claude-x"}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Bash","input":{"command":"ls"}}]}}'
echo '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tu1","is_error":true,"content":[{"type":"text","text":"boom"}]}]}}'
echo '{"type":"result","subtype":"success","is_error":false,"result":"ok","session_id":"s","total_cost_usd":0.12,"duration_ms":900,"usage":{"input_tokens":30,"output_tokens":7,"cache_read_input_tokens":2}}'
`)
	ctx, events := sinkCollector()
	emit, lines := collect()
	out := runAgent(ctx, AgentConfig{Kind: "claude", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "completed" {
		t.Fatalf("outcome %+v", out)
	}
	if len(*events) != 2 || (*events)[0].Phase != "started" || (*events)[0].CallID != "tu1" || !strings.Contains((*events)[0].Summary, "ls") {
		t.Fatalf("events %+v", *events)
	}
	if (*events)[1].Phase != "failed" || (*events)[1].Name != "Bash" || (*events)[1].Summary != "boom" {
		t.Fatalf("finish %+v", (*events)[1])
	}
	if len(*lines) != 0 {
		t.Fatalf("structured mode must not duplicate tools as text: %q", *lines)
	}
	if out.Usage["input_tokens"] != int64(30) || out.Usage["cost_usd"] != 0.12 || out.Usage["model"] != "claude-x" {
		t.Fatalf("usage %+v", out.Usage)
	}
}

func TestCodexToolLifecycleAndUsage(t *testing.T) {
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"thread.started","thread_id":"th"}'
echo '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"go test","status":"in_progress"}}'
echo '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"go test","aggregated_output":"FAIL","exit_code":1,"status":"failed"}}'
echo '{"type":"item.completed","item":{"id":"i2","type":"file_change","changes":[{"path":"a","kind":"update"}],"status":"completed"}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":9}}'
`)
	ctx, events := sinkCollector()
	emit, lines := collect()
	out := runAgent(ctx, AgentConfig{Kind: "codex", Command: cli}, Task{ID: 2, Prompt: "x"}, emit)
	if out.Status != "completed" || out.Result != "done" {
		t.Fatalf("outcome %+v", out)
	}
	ev := *events
	if len(ev) != 3 || ev[0].Phase != "started" || ev[1].Phase != "failed" || *ev[1].ExitCode != 1 || ev[1].Name != "Shell" {
		t.Fatalf("events %+v", ev)
	}
	// a call seen only finishing keeps no invented start
	if ev[2].CallID != "i2" || ev[2].Phase != "completed" || ev[2].Name != "Edit" {
		t.Fatalf("file change %+v", ev[2])
	}
	if len(*lines) != 1 || (*lines)[0] != "done" {
		t.Fatalf("lines %q", *lines)
	}
	if out.Usage["output_tokens"] != int64(9) || out.Usage["cache_read_tokens"] != int64(40) {
		t.Fatalf("usage %+v", out.Usage)
	}
}

func TestOldHubGetsTextLines(t *testing.T) {
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"ls"}}'
echo '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls","aggregated_output":"a","exit_code":0}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
`)
	emit, lines := collect()
	runAgent(context.Background(), AgentConfig{Kind: "codex", Command: cli}, Task{ID: 3, Prompt: "x"}, emit)
	if len(*lines) != 2 || !strings.HasPrefix((*lines)[0], "$ ls  (exit 0)") {
		t.Fatalf("legacy lines changed: %q", *lines)
	}
}

func TestOutboxPersistsUntilAcked(t *testing.T) {
	path := filepath.Join(t.TempDir(), "outbox.json")
	o := newOutbox(path)
	a := o.add(map[string]any{"type": "task_done", "task_id": 5})
	o.add(map[string]any{"type": "task_attachment", "task_id": 5})
	reloaded := newOutbox(path)
	if len(reloaded.pending()) != 2 || len(reloaded.taskIDs()) != 1 {
		t.Fatalf("not persisted: %+v", reloaded.pending())
	}
	reloaded.ack(a["msg_id"].(string))
	if again := newOutbox(path); len(again.pending()) != 1 || again.pending()[0]["type"] != "task_attachment" {
		t.Fatalf("ack not persisted: %+v", again.pending())
	}
}

func TestWorkspaceLockAcrossAgentsAndNestedFolders(t *testing.T) {
	root := t.TempDir()
	sub := filepath.Join(root, "pkg")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(root, link); err != nil {
		t.Fatal(err)
	}
	l := newWorkspaceLocks()
	release, err := l.acquire(context.Background(), root, 1, func(int64) { t.Fatal("first lock must not wait") })
	if err != nil {
		t.Fatal(err)
	}
	waited := make(chan int64, 2)
	got := make(chan func(), 1)
	go func() {
		// a nested folder reached through a symlink is the same workspace
		r, _ := l.acquire(context.Background(), filepath.Join(link, "pkg"), 2, func(h int64) { waited <- h })
		got <- r
	}()
	if h := <-waited; h != 1 {
		t.Fatalf("waited for %d", h)
	}
	select {
	case <-got:
		t.Fatal("acquired while held")
	case <-time.After(100 * time.Millisecond):
	}
	release()
	select {
	case r := <-got:
		r()
	case <-time.After(2 * time.Second):
		t.Fatal("not released")
	}
	// cancel while waiting
	release, _ = l.acquire(context.Background(), root, 3, func(int64) {})
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(50 * time.Millisecond); cancel() }()
	if _, err := l.acquire(ctx, sub, 4, func(int64) {}); err == nil {
		t.Fatal("expected cancel")
	}
	release()
	// unrelated folders never block each other
	other := t.TempDir()
	r1, _ := l.acquire(context.Background(), root, 5, func(int64) { t.Fatal("blocked") })
	r2, _ := l.acquire(context.Background(), other, 6, func(int64) { t.Fatal("blocked") })
	r1()
	r2()
}

// fakeHubWS accepts one connector session at a time and exposes what it received.
type fakeHubWS struct {
	t        *testing.T
	features []string
	ack      bool
	mu       sync.Mutex
	got      []map[string]any
	toSend   chan map[string]any
	sessions chan struct{}
}

func (h *fakeHubWS) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer conn.CloseNow()
	ctx := r.Context()
	var hello map[string]any
	if wsjson.Read(ctx, conn, &hello) != nil {
		return
	}
	h.record(hello)
	_ = wsjson.Write(ctx, conn, map[string]any{"type": "welcome", "connector_id": 1, "features": h.features})
	h.sessions <- struct{}{}
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case m := <-h.toSend:
				_ = wsjson.Write(ctx, conn, m)
			}
		}
	}()
	for {
		var m map[string]any
		if wsjson.Read(ctx, conn, &m) != nil {
			return
		}
		h.record(m)
		h.mu.Lock()
		acking := h.ack
		h.mu.Unlock()
		if id, ok := m["msg_id"].(string); ok && acking {
			_ = wsjson.Write(ctx, conn, map[string]any{"type": "ack", "msg_id": id})
		}
	}
}

func (h *fakeHubWS) setAck(on bool) {
	h.mu.Lock()
	h.ack = on
	h.mu.Unlock()
}

func (h *fakeHubWS) record(m map[string]any) {
	h.mu.Lock()
	h.got = append(h.got, m)
	h.mu.Unlock()
}

func (h *fakeHubWS) find(typ string) []map[string]any {
	h.mu.Lock()
	defer h.mu.Unlock()
	var out []map[string]any
	for _, m := range h.got {
		if m["type"] == typ {
			out = append(out, m)
		}
	}
	return out
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestConnectorReportsWithExecutionIDAndReplaysUnacked(t *testing.T) {
	hub := &fakeHubWS{t: t, features: []string{"events_v2", "ack", "telemetry", "execution_id"}, ack: true, toSend: make(chan map[string]any, 10),
		sessions: make(chan struct{}, 10)}
	srv := httptest.NewServer(hub)
	defer srv.Close()
	dir := t.TempDir()
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"ls"}}'
echo '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls","aggregated_output":"a","exit_code":0}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
`)
	cfg := &Config{HubURL: srv.URL, Token: "t", path: filepath.Join(dir, "connector.json"),
		Agents: []AgentConfig{{Key: "codex", Name: "Codex", Kind: "codex", Command: cli, Workdir: dir}}}
	c := NewConnector(cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go c.Run(ctx)
	<-hub.sessions
	hello := hub.find("hello")[0]
	agent := hello["agents"].([]any)[0].(map[string]any)
	if agent["capabilities"].(map[string]any)["tool_trace"] != "lifecycle" {
		t.Fatalf("capabilities %+v", agent)
	}
	hub.toSend <- map[string]any{"type": "task", "task": map[string]any{"id": 9, "turn": 1, "agent_key": "codex", "prompt": "p",
		"execution_id": "ex-1"}}
	waitFor(t, "task_done", func() bool { return len(hub.find("task_done")) == 1 })
	done := hub.find("task_done")[0]
	if done["execution_id"] != "ex-1" || done["msg_id"] == nil || done["status"] != "completed" {
		t.Fatalf("done %+v", done)
	}
	tools := []map[string]any{}
	for _, e := range hub.find("task_event") {
		if e["kind"] == "tool" {
			tools = append(tools, e)
		}
		if e["execution_id"] != "ex-1" {
			t.Fatalf("event without execution id %+v", e)
		}
	}
	if len(tools) != 2 || tools[0]["payload"].(map[string]any)["phase"] != "started" {
		t.Fatalf("tool events %+v", tools)
	}
	waitFor(t, "telemetry", func() bool { return len(hub.find("telemetry")) > 0 })
	waitFor(t, "acked", func() bool { return len(c.outbox.pending()) == 0 })

	// the hub stops acking: the result stays in the outbox and is replayed on the next session
	hub.setAck(false)
	hub.toSend <- map[string]any{"type": "task", "task": map[string]any{"id": 10, "turn": 1, "agent_key": "codex", "prompt": "p",
		"execution_id": "ex-2"}}
	waitFor(t, "second done", func() bool { return len(hub.find("task_done")) == 2 })
	if len(c.outbox.pending()) != 1 {
		t.Fatalf("unacked result not kept: %+v", c.outbox.pending())
	}
	if again := newOutbox(filepath.Join(dir, "outbox.json")); len(again.pending()) != 1 {
		t.Fatal("unacked result not on disk")
	}
	hub.setAck(true)
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()
	conn.Close(websocket.StatusGoingAway, "test reconnect")
	<-hub.sessions
	waitFor(t, "replay", func() bool { return len(hub.find("task_done")) == 3 })
	hellos := hub.find("hello")
	if finished := hellos[len(hellos)-1]["finished"].([]any); len(finished) != 1 || finished[0].(float64) != 10 {
		t.Fatalf("hello finished %+v", finished)
	}
	replayed := hub.find("task_done")[2]
	if replayed["msg_id"] != hub.find("task_done")[1]["msg_id"] {
		t.Fatal("replay must reuse the msg_id so the hub applies it once")
	}
	waitFor(t, "acked after replay", func() bool { return len(c.outbox.pending()) == 0 })
}

func TestOldHubWithoutFeaturesGetsLegacyMessages(t *testing.T) {
	hub := &fakeHubWS{t: t, toSend: make(chan map[string]any, 10), sessions: make(chan struct{}, 10)}
	srv := httptest.NewServer(hub)
	defer srv.Close()
	dir := t.TempDir()
	cli := fakeCLI(t, `cat > /dev/null
echo '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls","aggregated_output":"a","exit_code":0}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
`)
	cfg := &Config{HubURL: srv.URL, Token: "t", path: filepath.Join(dir, "connector.json"),
		Agents: []AgentConfig{{Key: "codex", Name: "Codex", Kind: "codex", Command: cli, Workdir: dir}}}
	c := NewConnector(cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go c.Run(ctx)
	<-hub.sessions
	hub.toSend <- map[string]any{"type": "task", "task": map[string]any{"id": 1, "turn": 1, "agent_key": "codex", "prompt": "p"}}
	waitFor(t, "done", func() bool { return len(hub.find("task_done")) == 1 })
	for _, e := range hub.find("task_event") {
		if e["kind"] != "output" {
			t.Fatalf("old hub got a structured event %+v", e)
		}
	}
	if len(hub.find("telemetry")) != 0 {
		t.Fatal("old hub got telemetry")
	}
	// never acked by an old hub, but delivered: not replayed forever
	waitFor(t, "outbox drained", func() bool { return len(c.outbox.pending()) == 0 })
}

// QA080-05: agents with no folder configured run in the connector's own working
// directory, so they share it and must take turns there too.
func TestAgentsWithoutWorkdirShareTheConnectorDirectoryLock(t *testing.T) {
	hub := &fakeHubWS{t: t, features: []string{"events_v2", "ack", "telemetry", "execution_id"}, ack: true,
		toSend: make(chan map[string]any, 10), sessions: make(chan struct{}, 10)}
	srv := httptest.NewServer(hub)
	defer srv.Close()
	dir := t.TempDir()
	log := filepath.Join(dir, "log")
	cli := fakeCLI(t, `cat > /dev/null
echo "start $1" >> `+log+`
sleep 0.6
echo "end $1" >> `+log+`
`)
	cfg := &Config{HubURL: srv.URL, Token: "t", path: filepath.Join(dir, "connector.json"), Agents: []AgentConfig{
		{Key: "a", Name: "A", Kind: "command", Command: cli, Args: []string{"a"}},
		{Key: "b", Name: "B", Kind: "command", Command: cli, Args: []string{"b"}},
	}}
	c := NewConnector(cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go c.Run(ctx)
	<-hub.sessions
	hub.toSend <- map[string]any{"type": "task", "task": map[string]any{"id": 1, "turn": 1, "agent_key": "a", "prompt": "p", "execution_id": "e1"}}
	time.Sleep(100 * time.Millisecond)
	hub.toSend <- map[string]any{"type": "task", "task": map[string]any{"id": 2, "turn": 1, "agent_key": "b", "prompt": "p", "execution_id": "e2"}}
	waitFor(t, "both done", func() bool { return len(hub.find("task_done")) == 2 })
	got, _ := os.ReadFile(log)
	if lines := strings.Fields(strings.ReplaceAll(string(got), "\n", " ")); strings.Join(lines, " ") != "start a end a start b end b" {
		t.Fatalf("ran concurrently in the shared directory: %q", got)
	}
	waited := false
	for _, m := range hub.find("telemetry") {
		for _, a := range m["agents"].([]any) {
			if lw, ok := a.(map[string]any)["lock_wait"].(map[string]any); ok && lw["holder_task_id"].(float64) == 1 {
				waited = true
			}
		}
	}
	if !waited {
		t.Fatal("the hub was never told task 2 waited for task 1's folder")
	}
}
