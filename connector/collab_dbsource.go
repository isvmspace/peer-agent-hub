package main

// Hermes and OpenCode write to their databases all the time while they work, so reading them again on every poll
// (seconds for a few days of history) would make the live view lag. These sources keep what they read and fetch only
// messages newer than the last one seen; sessions new to the window get their earlier messages once.

import (
	"fmt"
	"math"
)

type dbSessions struct {
	since    float64
	sessions map[string]*CollabSession
	mark     int64 // highest message id read
}

// sync brings the kept sessions up to date: list is the sessions in the window now (cheap), build makes a session
// from a row, update refreshes a kept one and says whether its stored messages may have been rewritten (then it is
// read again from scratch), messages fetches messages matching a condition on their id, feed applies them.
func (d *dbSessions) sync(since float64, list []map[string]any, build func(map[string]any) *CollabSession,
	update func(*CollabSession, map[string]any) bool, messages func(map[string]*CollabSession, string) ([]map[string]any, error),
	feed func(map[string]*CollabSession, []map[string]any)) []*CollabSession {
	if d.sessions == nil || since < d.since {
		d.sessions, d.mark, d.since = map[string]*CollabSession{}, 0, since
	}
	history := map[string]*CollabSession{} // new to the window: their messages up to the mark
	rebuild := map[string]*CollabSession{} // rewritten: all their messages again
	current := map[string]*CollabSession{}
	for _, r := range list {
		id := str(r["id"])
		if s := d.sessions[id]; s != nil {
			if update(s, r) {
				s.Turns, s.Calls, s.Tools = nil, nil, 0
				rebuild[id] = s
			}
			current[id] = s
			continue
		}
		s := build(r)
		current[id] = s
		if d.mark > 0 {
			history[id] = s
		}
	}
	// a session whose earlier messages could not be read is left out, so the next poll tries again
	read := func(group map[string]*CollabSession, cond string) {
		if len(group) == 0 {
			return
		}
		rows, err := messages(group, cond)
		if err != nil {
			for id := range group {
				delete(current, id)
			}
			return
		}
		feed(group, rows)
	}
	read(history, fmt.Sprintf("id <= %d", d.mark))
	read(rebuild, "1 = 1")
	d.sessions = current
	if len(current) == 0 {
		return nil
	}
	rest := map[string]*CollabSession{}
	for id, s := range current {
		if rebuild[id] == nil {
			rest[id] = s
		}
	}
	if len(rest) > 0 {
		rows, err := messages(rest, fmt.Sprintf("id > %d", d.mark))
		if err != nil {
			return nil
		}
		feed(rest, rows)
		for _, r := range rows {
			d.mark = int64(math.Max(float64(d.mark), num(r["id"])))
		}
	}
	out := make([]*CollabSession, 0, len(current))
	for _, s := range current {
		if s.Title == "" && len(s.Turns) > 0 {
			s.Title = oneLine(s.Turns[0].Prompt, 80)
		}
		out = append(out, s)
	}
	return out
}

type hermesSource struct{ dbSessions }

func (h *hermesSource) load(since float64) []*CollabSession {
	list, err := sqliteRows(collabHermesDB(), fmt.Sprintf("SELECT id, source, parent_session_id, started_at, ended_at, cwd, "+
		"display_name, tool_call_count FROM sessions WHERE started_at >= %f OR ended_at >= %f", since, since))
	if err != nil {
		return nil
	}
	return h.sync(since, list, hermesSession, func(s *CollabSession, r map[string]any) bool {
		if e := num(r["ended_at"]); e > s.Ended {
			s.Ended = e
		}
		if t := str(r["display_name"]); t != "" {
			s.Title = t
		}
		s.Tools = int(num(r["tool_call_count"]))
		return false // Hermes adds messages; it does not rewrite them
	}, func(ss map[string]*CollabSession, cond string) ([]map[string]any, error) {
		return hermesMessages(ss, cond, false)
	}, func(ss map[string]*CollabSession, rows []map[string]any) { hermesFeed(ss, rows, false) })
}

type opencodeSource struct {
	dbSessions
	updated map[string]float64 // session_v2.time_updated last seen: OpenCode rewrites a turn's row while it runs
}

func (o *opencodeSource) load(since float64) []*CollabSession {
	list, err := sqliteRows(collabOpenCodeDB(), fmt.Sprintf("SELECT * FROM session_v2 WHERE time_updated >= %f", since*1000))
	if err != nil {
		return nil
	}
	if o.updated == nil {
		o.updated = map[string]float64{}
	}
	seen := func(r map[string]any) bool { // records the session's update time; true when it moved
		id, at := str(r["id"]), num(r["time_updated"])
		moved := o.updated[id] != 0 && o.updated[id] != at
		o.updated[id] = at
		return moved
	}
	build := func(r map[string]any) *CollabSession { seen(r); return opencodeSession(r) }
	return o.sync(since, list, build, func(s *CollabSession, r map[string]any) bool {
		if e := num(r["time_updated"]) / 1000; e > s.Ended {
			s.Ended = e
		}
		if t := str(r["title"]); t != "" {
			s.Title = t
		}
		return seen(r)
	}, func(ss map[string]*CollabSession, cond string) ([]map[string]any, error) {
		return opencodeMessages(ss, cond, false)
	}, func(ss map[string]*CollabSession, rows []map[string]any) { opencodeFeed(ss, rows, false) })
}
