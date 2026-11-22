package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

var errRevoked = errors.New("连接器令牌已被吊销或无效")

type running struct {
	ctx        context.Context
	cancel     context.CancelFunc
	agentKey   string
	enqueuedAt time.Time
	started    bool
	lockHolder int64  // task holding the workspace this task waits for
	lockPath   string // that workspace, for the hub's wait reason
}

// Connector keeps one WebSocket session to the hub and runs tasks, one at a time per agent.
type Connector struct {
	cfg    *Config
	agents map[string]AgentConfig
	collab *collabIndex // 本机协作: this machine's agent sessions, read when the owner asks

	mu       sync.Mutex
	conn     *websocket.Conn
	writeMu  sync.Mutex
	tasks    map[int64]*running
	queues   map[string]chan Task
	outbox   *outbox // finished turns until the hub acknowledges them
	locks    *workspaceLocks
	features map[string]bool // what the connected hub understands (from welcome)
	updates  chan *UpdateInfo
	work     *workhost // set in workhost mode
}

func NewConnector(cfg *Config) *Connector {
	outboxPath := ""
	if cfg.path != "" {
		outboxPath = filepath.Join(filepath.Dir(cfg.path), "outbox.json")
	}
	c := &Connector{cfg: cfg, agents: map[string]AgentConfig{}, tasks: map[int64]*running{}, queues: map[string]chan Task{},
		outbox: newOutbox(outboxPath), locks: newWorkspaceLocks(), features: map[string]bool{}, updates: make(chan *UpdateInfo, 1),
		collab: newCollabIndex()}
	if cfg.Workhost != nil {
		c.work = newWorkhost(*cfg.Workhost)
		c.work.hub = cfg.HubURL
	}
	for _, a := range cfg.Agents {
		c.agents[a.Key] = a
		q := make(chan Task, 100)
		c.queues[a.Key] = q
		go c.worker(a, q)
	}
	return c
}

func wsURL(hub string) (string, error) {
	u, err := url.Parse(strings.TrimRight(hub, "/"))
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "http", "ws":
		u.Scheme = "ws"
	case "https", "wss":
		u.Scheme = "wss"
	default:
		return "", fmt.Errorf("hub_url 需以 http:// 或 https:// 开头：%s", hub)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/ws/connector"
	return u.String(), nil
}

// Run reconnects forever until ctx ends or the token is revoked.
func (c *Connector) Run(ctx context.Context) error {
	go c.updateLoop(ctx)
	if c.work != nil {
		go c.work.idleLoop(ctx)
	}
	backoff := time.Second
	for {
		started := time.Now()
		err := c.session(ctx)
		if ctx.Err() != nil {
			return nil
		}
		if errors.Is(err, errRevoked) {
			return err
		}
		if time.Since(started) > time.Minute {
			backoff = time.Second
		}
		log.Printf("与 Hub 断开：%v，%s 后重连", err, backoff)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(backoff):
		}
		if backoff < 30*time.Second {
			backoff *= 2
		}
	}
}

func (c *Connector) session(ctx context.Context) error {
	target, err := wsURL(c.cfg.HubURL)
	if err != nil {
		return err
	}
	dialCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	conn, resp, err := websocket.Dial(dialCtx, target, &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": {"Bearer " + c.cfg.Token}},
	})
	cancel()
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusForbidden {
			return errRevoked
		}
		return err
	}
	conn.SetReadLimit(32 << 20)
	defer conn.CloseNow()

	hostname, _ := os.Hostname()
	name := c.cfg.Name
	if name == "" {
		name = hostname
	}
	agents := c.agentList()
	hello := map[string]any{
		"type": "hello",
		"connector": map[string]any{"name": name, "hostname": hostname, "os": runtime.GOOS + "/" + runtime.GOARCH, "version": version,
			"local_ips": localIPs(c.cfg.HubURL), "workhost": c.workhostInfo()},
		"agents":   agents,
		"running":  c.runningIDs(),
		"finished": c.outbox.taskIDs(),
	}
	if err := wsjson.Write(ctx, conn, hello); err != nil {
		return err
	}
	var welcome struct {
		ConnectorID any      `json:"connector_id"`
		Features    []string `json:"features"`
	}
	if err := wsjson.Read(ctx, conn, &welcome); err != nil {
		var ce websocket.CloseError
		if errors.As(err, &ce) && ce.Code == 4401 {
			return errRevoked
		}
		return err
	}
	log.Printf("已连接 Hub %s（连接器 #%v），注册智能体 %d 个", c.cfg.HubURL, welcome.ConnectorID, len(agents))
	go func() {
		if issue := blockedFolders(); issue != "" {
			log.Printf("警告：%s", issue)
		}
	}()

	features := map[string]bool{}
	for _, f := range welcome.Features {
		features[f] = true
	}
	c.mu.Lock()
	c.conn = conn
	c.features = features
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		c.conn = nil
		c.mu.Unlock()
	}()
	// replay finished turns the hub has not acknowledged; it applies each msg_id once
	for _, msg := range c.outbox.pending() {
		if c.send(msg) && !features["ack"] {
			c.outbox.ack(msg["msg_id"].(string)) // an older hub never acks: delivered is the best we know
		}
	}
	c.sendTelemetry()

	sessCtx, stop := context.WithCancel(ctx)
	defer stop()
	go func() {
		t := time.NewTicker(20 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-sessCtx.Done():
				return
			case <-t.C:
				c.send(map[string]any{"type": "ping"})
				c.sendTelemetry()
			}
		}
	}()

	for {
		var raw json.RawMessage
		if err := wsjson.Read(sessCtx, conn, &raw); err != nil {
			return err
		}
		var msg struct {
			Type       string      `json:"type"`
			Task       Task        `json:"task"`
			TaskID     int64       `json:"task_id"`
			AgentKey   string      `json:"agent_key"`
			Workspaces []Workspace `json:"workspaces"`
			RequestID  string      `json:"request_id"`
			SessionID  string      `json:"session_id"`
			MsgID      string      `json:"msg_id"`
		}
		if json.Unmarshal(raw, &msg) != nil {
			continue
		}
		switch msg.Type {
		case "task":
			if msg.Task.MCPToken != "" {
				msg.Task.MCPURL = strings.TrimRight(c.cfg.HubURL, "/") + "/mcp/"
			}
			c.enqueue(msg.Task)
		case "cancel":
			c.cancel(msg.TaskID)
		case "ack":
			c.outbox.ack(msg.MsgID)
		case "set_workspaces":
			c.setWorkspaces(msg.AgentKey, msg.Workspaces)
		case "list_sessions", "read_session":
			go c.answerSessions(msg.Type, msg.RequestID, msg.AgentKey, msg.SessionID)
		case "collab_overview", "collab_session", "collab_discussion":
			go c.answerCollab(raw)
		case "set_work_agents":
			var m struct {
				Keys []string `json:"keys"`
			}
			_ = json.Unmarshal(raw, &m)
			c.setWorkAgents(m.Keys)
		case "work_ensure", "work_clone", "work_status", "work_push", "work_remove_session", "work_admin", "work_admin_action":
			go c.answerWork(raw)
		case "revoked":
			return errRevoked
		case "update_available":
			var info UpdateInfo
			if json.Unmarshal(raw, &info) == nil {
				select {
				case c.updates <- &info:
				default:
				}
			}
		}
	}
}

func (c *Connector) agentList() []map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	agents := make([]map[string]any, 0, len(c.cfg.Agents))
	for _, a := range c.cfg.Agents {
		ws := a.Workspaces
		if ws == nil {
			ws = []Workspace{}
		}
		agents = append(agents, map[string]any{
			"key": a.Key, "name": a.Name, "kind": a.Kind, "description": a.Description, "tags": a.Tags,
			"workdir": a.Workdir, "workspaces": ws, "isolation": a.isolationLevel(), "capabilities": capabilities(a.Kind),
			"workhost": c.cfg.isWorkAgent(a),
		})
	}
	return agents
}

// answerSessions replies to the hub's request for local conversations.
func (c *Connector) answerSessions(kind, requestID, key, sessionID string) {
	reply := map[string]any{"type": "rpc_result", "request_id": requestID}
	c.mu.Lock()
	a, ok := c.agents[key]
	c.mu.Unlock()
	var err error
	switch {
	case !ok:
		err = fmt.Errorf("本机没有智能体 %s", key)
	case kind == "list_sessions":
		var list []SessionInfo
		list, err = listSessions(a)
		reply["sessions"] = list
	default:
		var s SessionDetail
		s, err = readSession(a, sessionID)
		reply["session"] = s
		reply["workspace"] = a.workspaceFor(s.Cwd)
	}
	if err != nil {
		reply = map[string]any{"type": "rpc_result", "request_id": requestID, "error": err.Error()}
	}
	c.send(reply)
}

// setWorkspaces applies a workspace list edited by the agent's owner in the
// console. Only existing absolute directories are accepted and persisted.
func (c *Connector) setWorkspaces(key string, requested []Workspace) {
	clean, rejected := cleanWorkspaces(requested)
	c.mu.Lock()
	found := false
	for i := range c.cfg.Agents {
		if c.cfg.Agents[i].Key == key {
			c.cfg.Agents[i].Workspaces = clean
			c.agents[key] = c.cfg.Agents[i]
			found = true
		}
	}
	var err error
	if found {
		err = c.cfg.save()
	}
	c.mu.Unlock()
	if !found {
		return
	}
	if err != nil {
		log.Printf("保存工作区失败：%v", err)
	}
	if len(rejected) > 0 {
		log.Printf("智能体 %s 工作区忽略了不存在或非绝对路径的目录：%v", key, rejected)
	}
	log.Printf("智能体 %s 授权工作区更新为 %d 个", key, len(clean))
	c.send(map[string]any{"type": "agents", "agents": c.agentList()})
}

func (c *Connector) runningIDs() []int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	ids := make([]int64, 0, len(c.tasks))
	for id := range c.tasks {
		ids = append(ids, id)
	}
	return ids
}

// send writes to the hub and reports whether it was written. Task results go
// through the outbox (see finish) so a failed write is retried, not lost.
func (c *Connector) send(msg map[string]any) bool {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()
	if conn == nil {
		return false
	}
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := wsjson.Write(ctx, conn, msg); err != nil {
		log.Printf("发送失败：%v", err)
		return false
	}
	return true
}

// report sends a task result through the outbox: it stays on disk until the hub acks it.
func (c *Connector) report(msg map[string]any) {
	msg = c.outbox.add(msg)
	c.mu.Lock()
	acks := c.features["ack"]
	c.mu.Unlock()
	if c.send(msg) && !acks {
		c.outbox.ack(msg["msg_id"].(string))
	}
}

func (c *Connector) hasFeature(name string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.features[name]
}

// capabilities tells the hub what this kind of agent reports, so it never assumes more.
func capabilities(kind string) map[string]string {
	switch kind {
	case "claude", "codex":
		return map[string]string{"tool_trace": "lifecycle", "usage": "final"}
	}
	return map[string]string{"tool_trace": "none", "usage": "none"}
}

// sendTelemetry reports what each agent is doing: the running task, the queue
// and the oldest wait, and a workspace lock a task is waiting for.
func (c *Connector) sendTelemetry() {
	if !c.hasFeature("telemetry") {
		return
	}
	c.mu.Lock()
	now := time.Now()
	type load struct {
		running  any
		queued   []int64
		oldest   float64
		lockWait map[string]any
	}
	loads := map[string]*load{}
	for _, a := range c.cfg.Agents {
		loads[a.Key] = &load{queued: []int64{}}
	}
	for id, r := range c.tasks {
		l := loads[r.agentKey]
		if l == nil {
			continue
		}
		if r.started {
			l.running = id
			continue
		}
		l.queued = append(l.queued, id)
		if wait := now.Sub(r.enqueuedAt).Seconds(); wait > l.oldest {
			l.oldest = wait
		}
		if r.lockHolder != 0 {
			l.lockWait = map[string]any{"task_id": id, "holder_task_id": r.lockHolder, "path": r.lockPath}
		}
	}
	c.mu.Unlock()
	agents := make([]map[string]any, 0, len(loads))
	for key, l := range loads {
		sort.Slice(l.queued, func(i, j int) bool { return l.queued[i] < l.queued[j] })
		item := map[string]any{"key": key, "running": l.running, "queued": l.queued, "oldest_wait_s": int(l.oldest)}
		if l.lockWait != nil {
			item["lock_wait"] = l.lockWait
		}
		agents = append(agents, item)
	}
	c.send(map[string]any{"type": "telemetry", "agents": agents})
}

func (c *Connector) enqueue(t Task) {
	if t.Work != nil {
		c.mu.Lock()
		a, known := c.agents[t.AgentKey]
		c.mu.Unlock()
		if c.work != nil && known && !c.cfg.isWorkAgent(a) {
			c.report(map[string]any{"type": "task_done", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
				"status": "failed", "error": "这台服务器没有把 " + t.AgentKey + " 开放给「工作」使用"})
			return
		}
		if c.work == nil {
			c.report(map[string]any{"type": "task_done", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
				"status": "failed", "error": "这个连接器没有开启工作服务器模式"})
			return
		}
		c.enqueueWork(t)
		return
	}
	q, ok := c.queues[t.AgentKey]
	if !ok {
		c.report(map[string]any{"type": "task_done", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
			"status": "failed", "error": "本机连接器未配置智能体 " + t.AgentKey})
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	c.mu.Lock()
	if _, dup := c.tasks[t.ID]; dup {
		c.mu.Unlock()
		cancel()
		return
	}
	c.tasks[t.ID] = &running{ctx: ctx, cancel: cancel, agentKey: t.AgentKey, enqueuedAt: time.Now()}
	c.mu.Unlock()
	log.Printf("收到任务 #%d 第 %d 轮 → %s（来自 %s）：%s", t.ID, t.Turn, t.AgentKey, t.From, t.Title)
	// never block here: this runs on the only goroutine reading the hub, so a full queue would stall cancels,
	// acks and every other agent's tasks
	select {
	case q <- t:
	default:
		c.mu.Lock()
		delete(c.tasks, t.ID)
		c.mu.Unlock()
		cancel()
		c.report(map[string]any{"type": "task_done", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
			"status": "failed", "error": fmt.Sprintf("%s 本机排队已满（%d 个），请稍后再派", t.AgentKey, cap(q))})
	}
	c.sendTelemetry()
}

func (c *Connector) cancel(id int64) {
	c.mu.Lock()
	r := c.tasks[id]
	c.mu.Unlock()
	if r != nil {
		log.Printf("任务 #%d 被取消", id)
		r.cancel()
	}
}

func (c *Connector) worker(a AgentConfig, q chan Task) {
	for t := range q {
		c.mu.Lock()
		r := c.tasks[t.ID]
		current := c.agents[a.Key] // picks up workspace edits made after start
		c.mu.Unlock()
		if r == nil {
			continue
		}
		ctx := r.ctx
		if ctx.Err() != nil {
			c.finish(t, Outcome{Status: "failed", Error: "已取消"})
			continue
		}
		// one task per folder across all agents here; the lock lasts until the process exits
		var release func()
		if dir, err := current.resolveWorkdir(t.Workspace); err == nil {
			if dir == "" {
				// no folder configured: the CLI runs in the connector's own working directory, which
				// other agents without one share, so that directory is what gets locked
				dir, _ = os.Getwd()
			}
			release, err = c.locks.acquire(ctx, dir, t.ID, func(holder int64) {
				log.Printf("任务 #%d 等待工作区 %s（被任务 #%d 占用）", t.ID, dir, holder)
				c.mu.Lock()
				r.lockHolder, r.lockPath = holder, dir
				c.mu.Unlock()
				c.sendTelemetry()
			})
			if err != nil {
				c.finish(t, Outcome{Status: "failed", Error: "已取消"})
				continue
			}
		}
		c.mu.Lock()
		r.started, r.lockHolder, r.lockPath = true, 0, ""
		c.mu.Unlock()
		c.send(map[string]any{"type": "task_started", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID})
		c.sendTelemetry()
		emit := func(kind, content string) {
			c.send(map[string]any{"type": "task_event", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
				"kind": kind, "content": content})
		}
		runCtx := context.WithValue(context.WithValue(ctx, taskIDKey{}, t.ID), mcpTokenKey{}, t.MCPToken)
		if c.hasFeature("events_v2") {
			runCtx = withToolSink(runCtx, func(ev ToolEvent) {
				payload := map[string]any{"call_id": ev.CallID, "name": ev.Name, "phase": ev.Phase, "summary": ev.Summary,
					"observed_at": float64(time.Now().UnixMilli()) / 1000}
				if ev.ExitCode != nil {
					payload["exit_code"] = *ev.ExitCode
				}
				c.send(map[string]any{"type": "task_event", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID,
					"kind": "tool", "content": strings.TrimSpace(ev.Name + " " + ev.Summary), "payload": payload})
			})
		}
		start := time.Now()
		out := runAgent(runCtx, current, t, emit)
		if release != nil {
			release()
		}
		log.Printf("任务 #%d 结束：%s，耗时 %s", t.ID, out.Status, time.Since(start).Round(time.Second))
		c.finish(t, out)
	}
}

func (c *Connector) finish(t Task, out Outcome) {
	c.mu.Lock()
	if r := c.tasks[t.ID]; r != nil {
		r.cancel()
		delete(c.tasks, t.ID)
	}
	c.mu.Unlock()
	for _, f := range out.Files {
		if msg := attachmentMessage(t, f); msg != nil {
			msg["execution_id"] = t.ExecutionID
			c.report(msg)
		}
	}
	done := map[string]any{
		"type": "task_done", "task_id": t.ID, "turn": t.Turn, "execution_id": t.ExecutionID, "status": out.Status,
		"result": truncate(out.Result, maxResultChars), "error": out.Error, "session_id": out.SessionID,
	}
	if out.Usage != nil {
		done["usage"] = out.Usage
	}
	if out.CodeRef != nil {
		done["code_ref"] = out.CodeRef
	}
	c.report(done)
	c.sendTelemetry()
}

// workhostInfo tells the hub whether this machine can serve the 「工作」menu.
func (c *Connector) workhostInfo() map[string]any {
	info := map[string]any{"enabled": c.work != nil, "docker": dockerAvailable()}
	if c.work != nil {
		info["image"] = c.work.cfg.Image
		if m := c.work.cfg.Maven.info(); m != nil {
			info["maven"] = m
		}
	}
	return info
}

// setWorkAgents applies the owner's choice, made in the console, of which agents serve the 「工作」menu.
func (c *Connector) setWorkAgents(keys []string) {
	if c.work == nil {
		log.Printf("忽略开放智能体的设置：本机还没有开启工作服务器（agent-hub-connector workhost enable）")
		return
	}
	want := map[string]bool{}
	for _, k := range keys {
		want[k] = true
	}
	c.mu.Lock()
	for i := range c.cfg.Agents {
		c.cfg.Agents[i].Workhost = want[c.cfg.Agents[i].Key]
		c.agents[c.cfg.Agents[i].Key] = c.cfg.Agents[i]
	}
	err := c.cfg.save()
	c.mu.Unlock()
	if err != nil {
		log.Printf("保存开放智能体设置失败：%v", err)
	}
	log.Printf("开放给「工作」的智能体：%v", keys)
	c.send(map[string]any{"type": "agents", "agents": c.agentList()})
}

func dockerAvailable() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "docker", "version", "--format", "{{.Server.Version}}").Run() == nil
}
