package main

// Work server upkeep: what the hub's 「服务器维护」page shows (containers, resources, agent account quotas),
// the admin's container actions, idle containers going to sleep, and deleted sessions giving their disk back.

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"strings"
	"time"
)

// removeSession deletes a finished session's worktrees (its branch stays in the repository).
func (h *workhost) removeSession(ctx context.Context, w *WorkContext) error {
	if err := w.validate(); err != nil {
		return err
	}
	if w.Session == "" {
		return fmt.Errorf("缺少会话标识")
	}
	if err := h.ensure(ctx, w); err != nil {
		return err
	}
	for _, r := range w.Repos {
		src, dst := w.srcDir(r.Name), w.sessionDir()+"/"+r.Name
		if h.exists(ctx, w, dst) && h.exists(ctx, w, src+"/.git") {
			if _, err := h.git(ctx, w, false, "-C", src, "worktree", "remove", "--force", dst); err != nil {
				log.Printf("移除会话 %s 的 %s 工作树失败：%v", w.Session, r.Name, err)
			}
			_, _ = h.git(ctx, w, false, "-C", src, "worktree", "prune")
		}
	}
	_, err := h.inContainer(ctx, w, nil, nil, "rm", "-rf", w.sessionDir())
	return err
}

type containerInfo struct {
	User      string  `json:"user"`
	Name      string  `json:"name"`
	State     string  `json:"state"` // running | exited | ...
	Status    string  `json:"status"`
	CPU       string  `json:"cpu"`
	Memory    string  `json:"memory"`
	Disk      string  `json:"disk"`
	Running   int     `json:"running_turns"`
	IdleSince float64 `json:"last_active"` // unix seconds, 0 if unknown
}

type quotaWindow struct {
	UsedPercent float64 `json:"used_percent"`
	WindowMins  int     `json:"window_minutes"`
	ResetsAt    int64   `json:"resets_at"`
}

type accountInfo struct {
	ID        string       `json:"id"`
	Kind      string       `json:"kind"`
	Plan      string       `json:"plan"`
	Email     string       `json:"email"` // masked
	Primary   *quotaWindow `json:"primary"`
	Secondary *quotaWindow `json:"secondary"`
	Usable    bool         `json:"usable"`
	Error     string       `json:"error,omitempty"`
}

func maskEmail(email string) string {
	user, domain, ok := strings.Cut(email, "@")
	if !ok || user == "" {
		return email
	}
	keep := 2
	if len(user) <= keep {
		keep = 1
	}
	return user[:keep] + "***@" + domain
}

// readCodexQuota asks `codex app-server` (under the account's CODEX_HOME) for its plan and rate limits.
// It reads only; no model call is made, so it costs nothing.
func (h *workhost) readCodexQuota(ctx context.Context, a WorkAccount) accountInfo {
	info := accountInfo{ID: a.ID, Kind: a.Kind}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, h.cfg.CodexCLI, "app-server")
	cmd.Env = append(os.Environ(), "CODEX_HOME="+a.Home)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		info.Error = err.Error()
		return info
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		info.Error = err.Error()
		return info
	}
	if err := cmd.Start(); err != nil {
		info.Error = "启动 codex app-server 失败：" + err.Error()
		return info
	}
	defer func() { _ = stdin.Close(); _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	requests := []string{
		`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"agent-hub","version":"` + version + `"}}}`,
		`{"jsonrpc":"2.0","method":"initialized","params":{}}`,
		`{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}`,
		`{"jsonrpc":"2.0","id":3,"method":"account/read","params":{}}`,
	}
	go func() { _, _ = io.WriteString(stdin, strings.Join(requests, "\n")+"\n") }()
	got := 0
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for got < 2 && scanner.Scan() {
		var msg struct {
			ID     int             `json:"id"`
			Result json.RawMessage `json:"result"`
			Error  *struct {
				Message string `json:"message"`
			} `json:"error"`
		}
		if json.Unmarshal(scanner.Bytes(), &msg) != nil || (msg.ID != 2 && msg.ID != 3) {
			continue
		}
		got++
		if msg.Error != nil {
			info.Error = msg.Error.Message
			continue
		}
		parseCodexReply(msg.ID, msg.Result, &info)
	}
	if got < 2 && info.Error == "" {
		info.Error = "读取额度超时"
	}
	return info
}

func parseCodexReply(id int, raw json.RawMessage, info *accountInfo) {
	switch id {
	case 2:
		var r struct {
			OrdinaryUsageAllowed *bool `json:"ordinaryUsageAllowed"`
			RateLimits           struct {
				Primary   *quotaWindowRaw `json:"primary"`
				Secondary *quotaWindowRaw `json:"secondary"`
				PlanType  string          `json:"planType"`
			} `json:"rateLimits"`
		}
		if json.Unmarshal(raw, &r) == nil {
			info.Primary, info.Secondary = r.RateLimits.Primary.window(), r.RateLimits.Secondary.window()
			if r.RateLimits.PlanType != "" {
				info.Plan = r.RateLimits.PlanType
			}
			info.Usable = r.OrdinaryUsageAllowed == nil || *r.OrdinaryUsageAllowed
		}
	case 3:
		var r struct {
			Account struct {
				Type     string `json:"type"`
				Email    string `json:"email"`
				PlanType string `json:"planType"`
			} `json:"account"`
		}
		if json.Unmarshal(raw, &r) == nil {
			info.Email = maskEmail(r.Account.Email)
			if info.Plan == "" {
				info.Plan = r.Account.PlanType
			}
			if info.Plan == "" {
				info.Plan = r.Account.Type
			}
		}
	}
}

type quotaWindowRaw struct {
	UsedPercent        float64 `json:"usedPercent"`
	WindowDurationMins int     `json:"windowDurationMins"`
	ResetsAt           int64   `json:"resetsAt"`
}

func (q *quotaWindowRaw) window() *quotaWindow {
	if q == nil {
		return nil
	}
	return &quotaWindow{UsedPercent: q.UsedPercent, WindowMins: q.WindowDurationMins, ResetsAt: q.ResetsAt}
}

// containers lists colleagues' containers with live usage and the size of their volume.
func (h *workhost) containers(ctx context.Context) ([]containerInfo, error) {
	out, err := h.docker(ctx, nil, nil, "ps", "-a", "--filter", "label=agent-hub.work", "--filter", "label=agent-hub.hub="+h.hub, "--format", "{{json .}}")
	if err != nil {
		return nil, err
	}
	var list []containerInfo
	var running []string
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		var row struct{ Names, State, Status string }
		if json.Unmarshal([]byte(line), &row) != nil || !strings.HasPrefix(row.Names, "ahw-") {
			continue
		}
		c := containerInfo{Name: row.Names, User: strings.TrimPrefix(row.Names, "ahw-"), State: row.State, Status: row.Status}
		h.mu.Lock()
		c.Running = h.busy[c.User]
		if at, ok := h.active[c.User]; ok {
			c.IdleSince = float64(at.Unix())
		}
		h.mu.Unlock()
		list = append(list, c)
		if row.State == "running" {
			running = append(running, row.Names)
		}
	}
	if len(running) > 0 {
		stats, err := h.docker(ctx, nil, nil, append([]string{"stats", "--no-stream", "--format", "{{json .}}"}, running...)...)
		if err == nil {
			for _, line := range strings.Split(strings.TrimSpace(stats), "\n") {
				var s struct{ Name, CPUPerc, MemUsage string }
				if json.Unmarshal([]byte(line), &s) != nil {
					continue
				}
				for i := range list {
					if list[i].Name == s.Name {
						list[i].CPU, list[i].Memory = s.CPUPerc, s.MemUsage
					}
				}
			}
		}
	}
	if df, err := h.docker(ctx, nil, nil, "system", "df", "-v", "--format", "{{json .Volumes}}"); err == nil {
		var vols []struct{ Name, Size string }
		if json.Unmarshal([]byte(strings.TrimSpace(df)), &vols) == nil {
			for _, v := range vols {
				for i := range list {
					if list[i].Name == v.Name {
						list[i].Disk = v.Size
					}
				}
			}
		}
	}
	return list, nil
}

// answerWorkAdmin serves the 「服务器维护」page: status (containers, host, accounts) and container actions.
func (c *Connector) answerWorkAdmin(raw json.RawMessage) {
	var msg struct {
		Type      string `json:"type"`
		RequestID string `json:"request_id"`
		Action    string `json:"action"`
		User      string `json:"user"`
	}
	if json.Unmarshal(raw, &msg) != nil {
		return
	}
	reply := map[string]any{"type": "rpc_result", "request_id": msg.RequestID}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	h := c.work
	var err error
	if msg.Type == "work_admin" {
		var list []containerInfo
		list, err = h.containers(ctx)
		reply["containers"] = list
		host := map[string]any{"idle_minutes": h.cfg.IdleMinutes, "max_sessions_per_user": h.cfg.MaxSessions,
			"cpus_per_user": h.cfg.CPUs, "memory_per_user": h.cfg.Memory}
		if info, e := h.docker(ctx, nil, nil, "info", "--format", "{{json .}}"); e == nil {
			var d struct {
				NCPU     int
				MemTotal int64
			}
			if json.Unmarshal([]byte(info), &d) == nil {
				host["cpus"], host["memory_bytes"] = d.NCPU, d.MemTotal
			}
		}
		reply["host"] = host
		accounts := []accountInfo{}
		for _, a := range h.cfg.Accounts {
			if a.Kind == "codex" {
				accounts = append(accounts, h.readCodexQuota(ctx, a))
			} else if a.Kind == "claude" {
				accounts = append(accounts, readClaudeQuota(ctx, a))
			} else {
				accounts = append(accounts, accountInfo{ID: a.ID, Kind: a.Kind, Usable: true})
			}
		}
		reply["accounts"] = accounts
	} else {
		if !safeName.MatchString(msg.User) {
			err = fmt.Errorf("容器标识不合法")
		} else {
			name := "ahw-" + msg.User
			h.mu.Lock()
			busy := h.busy[msg.User]
			h.mu.Unlock()
			switch msg.Action {
			case "stop":
				if busy > 0 {
					err = fmt.Errorf("这个容器里还有 %d 个会话在运行，等结束后再休眠", busy)
				} else {
					_, err = h.docker(ctx, nil, nil, "stop", name)
				}
			case "start":
				_, err = h.docker(ctx, nil, nil, "start", name)
				h.touch(msg.User, 0)
			case "restart":
				if busy > 0 {
					err = fmt.Errorf("这个容器里还有 %d 个会话在运行，重启会中断它们", busy)
				} else {
					_, err = h.docker(ctx, nil, nil, "restart", name)
					h.touch(msg.User, 0)
				}
			default:
				err = fmt.Errorf("未知操作 %s", msg.Action)
			}
		}
	}
	if err != nil {
		reply = map[string]any{"type": "rpc_result", "request_id": msg.RequestID, "error": err.Error()}
	}
	c.send(reply)
}

// idleLoop stops a colleague's container once nothing has used it for IdleMinutes; ensure() starts it again.
func (h *workhost) idleLoop(ctx context.Context) {
	if list, err := h.containers(ctx); err == nil {
		for _, c := range list {
			if c.State == "running" {
				h.touch(c.User, 0) // a restarted connector gives running containers a fresh idle window
			}
		}
	}
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		idle := time.Duration(h.cfg.IdleMinutes) * time.Minute
		var sleepy []string
		h.mu.Lock()
		for user, at := range h.active {
			if h.busy[user] == 0 && time.Since(at) >= idle {
				sleepy = append(sleepy, user)
			}
		}
		h.mu.Unlock()
		for _, user := range sleepy {
			lock := h.bootLock(user)
			lock.Lock() // never stop a container while it is being set up
			h.mu.Lock()
			still := h.busy[user] == 0 && time.Since(h.active[user]) >= idle
			if still {
				delete(h.active, user)
			}
			h.mu.Unlock()
			if still {
				stop, cancel := context.WithTimeout(ctx, time.Minute)
				if _, err := h.docker(stop, nil, nil, "stop", "ahw-"+user); err != nil {
					log.Printf("休眠工作容器 ahw-%s 失败：%v", user, err)
				} else {
					log.Printf("工作容器 ahw-%s 闲置超过 %d 分钟，已休眠", user, h.cfg.IdleMinutes)
				}
				cancel()
			}
			lock.Unlock()
		}
	}
}
