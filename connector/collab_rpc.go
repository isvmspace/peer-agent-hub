package main

import (
	"encoding/json"
	"fmt"
	"sort"
)

const collabPage = 20 // turns per detail page

// collabDetail is one page of a session: `start` is the first turn shown (-1 = the latest page). Every text that
// leaves this machine is masked.
func collabDetail(full *CollabSession, all map[string]*CollabSession, start int) map[string]any {
	byCall := map[string][]map[string]any{}
	var kids []*CollabSession
	for _, s := range all {
		if s.Parent == full.Key() {
			kids = append(kids, s)
		}
	}
	sort.Slice(kids, func(i, j int) bool { return kids[i].Started < kids[j].Started })
	for _, k := range kids {
		byCall[k.ParentCall] = append(byCall[k.ParentCall], collabBrief(k, nil))
	}
	total := len(full.Turns)
	if start < 0 {
		start = max(0, total-collabPage)
	}
	start = min(start, max(0, total-1))
	end := min(total, start+collabPage)
	begin, stop := -1e18, 1e18
	if start > 0 {
		begin = full.Turns[start].TS - 1
	}
	if end < total {
		stop = full.Turns[end].TS - 1
	}
	turnOf := func(ts float64) int {
		i := 0
		for i+1 < total && full.Turns[i+1].TS-1 <= ts {
			i++
		}
		return i
	}
	launches := []map[string]any{}
	calls := []map[string]any{}
	for _, c := range full.Calls {
		children := byCall[c.ID]
		delete(byCall, c.ID)
		if children == nil {
			children = []map[string]any{}
		} else {
			launches = append(launches, map[string]any{"call": c.ID, "turn": turnOf(c.TS), "ts": c.TS,
				"summary": redactText(c.Summary), "children": children})
		}
		if c.TS >= begin && c.TS < stop {
			result := redactText(c.Result)
			if sensitiveCall(c.Command) || sensitiveCall(c.Summary) {
				result = hiddenNotice // reading credentials: the whole output stays here, as in the session viewer
			}
			calls = append(calls, map[string]any{"id": c.ID, "tool": c.Tool, "ts": c.TS, "end": c.End,
				"summary": redactTool(c.Summary), "command": redactTool(c.Command), "result": result,
				"launches": orEmpty(c.Launches), "children": children})
		}
	}
	turns := []map[string]any{}
	for i := start; i < end; i++ {
		t := full.Turns[i]
		turns = append(turns, map[string]any{"index": i, "ts": t.TS, "prompt": redactText(t.Prompt), "reply": redactText(t.Reply)})
	}
	others := []map[string]any{} // their call is no longer in the log
	for _, group := range byCall {
		others = append(others, group...)
	}
	out := collabBrief(full, nil)
	for k, v := range map[string]any{"total_turns": total, "start": start, "end": end, "turns": turns, "calls": calls,
		"launches": launches, "other_children": others, "parent_brief": nil} {
		out[k] = v
	}
	if p := all[full.Parent]; p != nil {
		out["parent_brief"] = collabBrief(p, nil)
	}
	return out
}

// answerCollab handles the hub's collab_overview / collab_session requests, off the task queue.
func (c *Connector) answerCollab(raw json.RawMessage) {
	var msg struct {
		Type      string  `json:"type"`
		RequestID string  `json:"request_id"`
		Days      float64 `json:"days"`
		Key       string  `json:"key"`
		Start     *int    `json:"start"`
	}
	if json.Unmarshal(raw, &msg) != nil {
		return
	}
	reply := map[string]any{"type": "rpc_result", "request_id": msg.RequestID}
	days := min(max(msg.Days, 0.04), 90)
	if msg.Days == 0 {
		days = 3
	}
	switch msg.Type {
	case "collab_overview":
		reply["overview"] = c.collab.overview(days)
	case "collab_session":
		start := -1
		if msg.Start != nil {
			start = *msg.Start
		}
		page, ok := c.collab.page(msg.Key, days, start)
		if !ok {
			reply["error"] = "找不到这个会话"
			break
		}
		reply["session"] = page
	case "collab_discussion":
		d, ok := c.collab.discussion(msg.Key, days)
		if !ok {
			reply["error"] = "找不到这个会话"
			break
		}
		reply["discussion"] = d
	default:
		reply["error"] = fmt.Sprintf("未知请求 %s", msg.Type)
	}
	c.send(reply)
}

// orEmpty keeps an empty list a list in JSON (a nil slice would go out as null).
func orEmpty(list []string) []string {
	if list == nil {
		return []string{}
	}
	return list
}
