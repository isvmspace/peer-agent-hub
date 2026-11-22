package main

// Who started whom. Two ways:
//   - native: the agent recorded the parent itself (Claude sub-agents, Hermes and OpenCode sub-sessions);
//   - inferred: a shell command in one session started another agent's CLI (`codex exec`, `claude -p`,
//     `hermes chat -q`, `opencode run`) and a non-interactive session of that agent began while the command ran,
//     in the same folder (or within 15 s anywhere).

import (
	"regexp"
	"sort"
	"strings"
)

const (
	collabGrace      = 3.0   // seconds of clock slack between a command and the session it starts
	collabBackground = 120.0 // a backgrounded command returns at once; the agents it started begin within this
	collabElsewhere  = 15.0  // a session in another folder still counts when it began this soon
	collabLoopCap    = 20
)

var (
	collabNonInteractive = map[string]map[string]bool{
		"codex": {"exec": true}, "claude": {"非交互（-p）": true}, "hermes": {"非交互（oneshot）": true, "终端": true}, "opencode": {"": true},
	}
	collabSubagentTools = map[string]bool{"Task": true, "Agent": true, "delegate_task": true, "task": true}
	collabLoops         = regexp.MustCompile(`\b(for|while|xargs|parallel)\b|&\s*$|&\s*\n`)
)

func related(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	a, b = strings.TrimRight(a, "/"), strings.TrimRight(b, "/")
	return a == b || strings.HasPrefix(a, b+"/") || strings.HasPrefix(b, a+"/")
}

func collabLink(sessions map[string]*CollabSession) {
	// 1. what the agents recorded themselves
	for _, s := range sessions {
		parent := sessions[s.NativeParent]
		if s.NativeParent == "" || parent == nil {
			continue
		}
		s.Parent, s.ParentCall, s.Link = parent.Key(), s.NativeCall, "native"
		if s.ParentCall == "" { // Hermes/OpenCode: the delegating call closest before the child started
			best := -1.0
			for _, c := range parent.Calls {
				if contains(c.Launches, s.Agent) && c.TS <= s.Started+collabGrace && c.TS > best {
					best, s.ParentCall = c.TS, c.ID
				}
			}
		}
	}
	// 2. CLI launches matched by time and folder; earlier commands claim first
	byAgent := map[string][]*CollabSession{}
	type launch struct {
		owner *CollabSession
		call  CollabCall
	}
	var launches []launch
	for _, s := range sessions {
		byAgent[s.Agent] = append(byAgent[s.Agent], s)
		for _, c := range s.Calls {
			if len(c.Launches) > 0 {
				launches = append(launches, launch{s, c})
			}
		}
	}
	sort.SliceStable(launches, func(i, j int) bool { return launches[i].call.TS < launches[j].call.TS })
	for _, l := range launches {
		for _, agent := range l.call.Launches {
			if agent == l.owner.Agent && collabSubagentTools[l.call.Tool] {
				continue // the agent's own sub-agents are linked natively
			}
			until := l.call.TS + 6*3600
			if l.call.End != nil {
				until = *l.call.End
			}
			if until < l.call.TS+collabBackground {
				until = l.call.TS + collabBackground
			}
			until += collabGrace
			var near, anywhere []*CollabSession
			for _, c := range byAgent[agent] {
				if c.Parent != "" || c == l.owner || c.Started < l.call.TS-collabGrace || c.Started > until ||
					!collabNonInteractive[agent][c.Origin] {
					continue
				}
				if related(c.Cwd, l.owner.Cwd) {
					near = append(near, c)
				} else if c.Started-l.call.TS <= collabElsewhere {
					anywhere = append(anywhere, c)
				}
			}
			if len(near) == 0 {
				near = anywhere
			}
			sort.Slice(near, func(i, j int) bool { return near[i].Started < near[j].Started })
			limit := len(regexp.MustCompile(`\b`+agent+`\b`).FindAllString(l.call.Command, -1))
			if collabLoops.MatchString(l.call.Command) {
				limit = collabLoopCap
			}
			if limit < 1 {
				limit = 1
			}
			for i, child := range near {
				if i >= limit {
					break
				}
				child.Parent, child.ParentCall, child.Link = l.owner.Key(), l.call.ID, "inferred"
			}
		}
	}
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

const (
	collabActiveFor = 90.0 // a session whose log was written this recently is working right now
	collabMaxEvents = 30
)

// collabNode is a session's node on the star graph: Claude's sub-agents get their own.
func collabNode(s *CollabSession) string {
	if s.Agent == "claude" && s.Origin == "子代理" {
		return "subagent"
	}
	return s.Agent
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return oneLine(s, 120)
}

func lastReply(s *CollabSession) string {
	for i := len(s.Turns) - 1; i >= 0; i-- {
		if strings.TrimSpace(s.Turns[i].Reply) != "" {
			return s.Turns[i].Reply
		}
	}
	return ""
}

func findCall(s *CollabSession, id string) *CollabCall {
	for i := range s.Calls {
		if s.Calls[i].ID == id {
			return &s.Calls[i]
		}
	}
	return nil
}

// collabOverview: the star graph (nodes, node → node counts), what is working now, the latest events, and every
// session with its children.
func collabOverview(sessions map[string]*CollabSession) map[string]any {
	now := nowSeconds()
	active := func(s *CollabSession) bool { return now-s.Ended <= collabActiveFor }
	nodes := map[string]map[string]int{}
	edges := map[[2]string]int{}
	children := map[string][]*CollabSession{}
	launches := map[string]map[string]any{} // parent key + call id → launch event
	var events []map[string]any
	for _, s := range sessions {
		kind := collabNode(s)
		if nodes[kind] == nil {
			nodes[kind] = map[string]int{"sessions": 0, "active": 0}
		}
		nodes[kind]["sessions"]++
		if active(s) {
			nodes[kind]["active"]++
		}
		p := sessions[s.Parent]
		if p == nil {
			continue
		}
		edges[[2]string{collabNode(p), kind}]++
		children[p.Key()] = append(children[p.Key()], s)
		id := p.Key() + "\x00" + s.ParentCall
		ev := launches[id]
		if ev == nil {
			ts, summary := s.Started, ""
			if c := findCall(p, s.ParentCall); c != nil {
				ts, summary = c.TS, c.Summary // masked before it was cut to one line (when the log was read)
			}
			ev = map[string]any{"kind": "launch", "ts": ts, "from": p.Key(), "from_agent": collabNode(p),
				"to": []map[string]any{}, "summary": redactTool(summary)}
			launches[id] = ev
			events = append(events, ev)
		}
		ev["to"] = append(ev["to"].([]map[string]any), map[string]any{"key": s.Key(), "agent": kind})
		if reply := lastReply(s); !active(s) && reply != "" {
			events = append(events, map[string]any{"kind": "reply", "ts": s.Ended, "from": s.Key(), "from_agent": kind,
				"to": []map[string]any{{"key": p.Key(), "agent": collabNode(p)}}, "summary": firstLine(redactText(reply))})
		}
	}
	sort.Slice(events, func(i, j int) bool { return events[i]["ts"].(float64) > events[j]["ts"].(float64) })
	if len(events) > collabMaxEvents {
		events = events[:collabMaxEvents]
	}
	edgeList := []map[string]any{}
	for pair, n := range edges {
		edgeList = append(edgeList, map[string]any{"from": pair[0], "to": pair[1], "count": n})
	}
	sort.Slice(edgeList, func(i, j int) bool { // equal counts in a fixed order, so the graph does not reshuffle every poll
		a, b := edgeList[i], edgeList[j]
		if a["count"].(int) != b["count"].(int) {
			return a["count"].(int) > b["count"].(int)
		}
		return a["from"].(string)+">"+a["to"].(string) < b["from"].(string)+">"+b["to"].(string)
	})
	list := make([]*CollabSession, 0, len(sessions))
	agents := map[string]int{}
	for _, s := range sessions {
		list = append(list, s)
		agents[s.Agent]++
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Started > list[j].Started })
	out := make([]map[string]any, 0, len(list))
	for _, s := range list {
		kids := children[s.Key()]
		sort.Slice(kids, func(i, j int) bool { return kids[i].Started < kids[j].Started })
		keys := make([]string, 0, len(kids))
		for _, k := range kids {
			keys = append(keys, k.Key())
		}
		b := collabBrief(s, keys)
		b["active"] = active(s)
		b["node"] = collabNode(s)
		out = append(out, b)
	}
	if events == nil {
		events = []map[string]any{}
	}
	return map[string]any{"agents": agents, "nodes": nodes, "edges": edgeList, "events": events, "now": now, "sessions": out}
}

// collabDiscussion: every round of work a session handed out, as a group chat — the question, then each agent's
// answer in the order they finished.
func collabDiscussion(owner *CollabSession, sessions map[string]*CollabSession) map[string]any {
	now := nowSeconds()
	byCall := map[string][]*CollabSession{}
	for _, s := range sessions {
		if s.Parent == owner.Key() {
			byCall[s.ParentCall] = append(byCall[s.ParentCall], s)
		}
	}
	rounds := []map[string]any{}
	for id, kids := range byCall {
		sort.Slice(kids, func(i, j int) bool { return kids[i].Ended < kids[j].Ended })
		ts, prompt := kids[0].Started, ""
		if c := findCall(owner, id); c != nil {
			ts = c.TS
			if c.Tool == "Task" || c.Tool == "Agent" {
				prompt = c.Command
			}
		}
		to := []string{}
		replies := []map[string]any{}
		for _, k := range kids {
			if prompt == "" && len(k.Turns) > 0 {
				prompt = k.Turns[0].Prompt
			}
			to = append(to, collabNode(k))
			r := collabBrief(k, nil)
			r["agent"], r["active"] = collabNode(k), now-k.Ended <= collabActiveFor
			r["reply"] = redactText(lastReply(k))
			replies = append(replies, r)
		}
		rounds = append(rounds, map[string]any{"call": id, "ts": ts, "prompt": redactText(prompt), "to": to, "replies": replies})
	}
	sort.Slice(rounds, func(i, j int) bool { return rounds[i]["ts"].(float64) < rounds[j]["ts"].(float64) })
	out := collabBrief(owner, nil)
	out["node"], out["rounds"] = collabNode(owner), rounds
	return out
}

// collabBrief is a session's summary as it leaves this machine: the title is masked like any text.
func collabBrief(s *CollabSession, children []string) map[string]any {
	b := s.Brief()
	b["title"] = redactText(b["title"].(string))
	if children != nil {
		b["children"] = children
	}
	return b
}
