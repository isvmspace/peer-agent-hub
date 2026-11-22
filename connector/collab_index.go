package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"slices"
	"strings"
	"sync"
	"time"
)

const collabDetailKept = 3 // sessions whose full parse is kept for paging

const collabReuse = 2 * time.Second // an overview this fresh is served again as is (the page polls every 3 s)

// collabParser reads a log line by line; the index keeps one per log and feeds it what was appended since.
type collabParser interface {
	feed(line []byte)
	session() *CollabSession
}

type collabFile struct {
	info   os.FileInfo // to notice the log being replaced by another file
	mtime  time.Time
	size   int64
	offset int64 // bytes read so far, always at a line end
	parser collabParser
}

type collabDB struct {
	version, since float64
	sessions       []*CollabSession
}

// collabIndex reads every agent's sessions, caching each log by its modification time and size and each
// database by its version, and links who started whom.
type collabIndex struct {
	mu          sync.Mutex
	files       map[string]collabFile
	dbs         map[string]collabDB
	sessions    map[string]*CollabSession
	built       float64
	since       float64
	parses      int                          // logs read, fully or the appended part (tests)
	fullParses  int                          // logs read from the start (tests: a growing log is read on, not again)
	details     map[string]collabDetailCache // the last full read of a session, while its source is unchanged
	detailLogs  map[string]collabFile        // full parses of the logs last opened in the detail view
	detailOrder []string
	hermes      hermesSource // Hermes / OpenCode databases, read on from the last message seen
	opencode    opencodeSource
}

type collabDetailCache struct {
	version string
	session *CollabSession
}

func newCollabIndex() *collabIndex {
	return &collabIndex{files: map[string]collabFile{}, dbs: map[string]collabDB{}, details: map[string]collabDetailCache{},
		detailLogs: map[string]collabFile{}}
}

// readLog brings one log's cached parse up to date (only what was appended, unless it was rewritten). It touches only
// its own entry, so several logs are read at once.
func readLog(path string, c collabFile, cached bool, newParser func(string) collabParser) (collabFile, bool, bool) {
	info, err := os.Stat(path)
	if err != nil {
		return c, false, false
	}
	if cached && c.mtime.Equal(info.ModTime()) && c.size == info.Size() {
		return c, false, false
	}
	full := false
	// a log that shrank, went back in time or was replaced by another file was rewritten: read it again from the start
	if !cached || info.Size() < c.offset || info.ModTime().Before(c.mtime) || !os.SameFile(c.info, info) {
		c, full = collabFile{parser: newParser(path)}, true
	}
	c.offset = readLinesFrom(path, c.offset, c.parser.feed)
	c.info, c.mtime, c.size = info, info.ModTime(), info.Size()
	return c, true, full
}

// logs reads every listed log, a few at a time (a cold start parses hundreds of megabytes of Claude logs).
func (x *collabIndex) logs(paths []string, newParser func(string) collabParser) []*CollabSession {
	type result struct {
		path       string
		c          collabFile
		read, full bool
	}
	results := make([]result, len(paths))
	jobs := make(chan int)
	var wg sync.WaitGroup
	for w := 0; w < min(8, len(paths)); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				c, cached := x.files[paths[i]] // read only here; written back after all workers are done
				nc, read, full := readLog(paths[i], c, cached, newParser)
				results[i] = result{paths[i], nc, read, full}
			}
		}()
	}
	for i := range paths {
		jobs <- i
	}
	close(jobs)
	wg.Wait()
	var out []*CollabSession
	for _, r := range results {
		if r.c.parser == nil {
			continue
		}
		if r.read {
			x.parses++
			x.files[r.path] = r.c
		}
		if r.full {
			x.fullParses++
		}
		if s := r.c.parser.session(); s != nil {
			out = append(out, s)
		}
	}
	return out
}

// readLinesFrom feeds every complete line after offset and returns where the next read starts: a last line still
// being written (no newline yet) is left for the next read.
func readLinesFrom(path string, offset int64, feed func([]byte)) int64 {
	f, err := os.Open(path)
	if err != nil {
		return offset
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return offset
	}
	r := bufio.NewReaderSize(f, 1<<20)
	for {
		line, err := r.ReadBytes('\n')
		if err != nil { // EOF: whatever is left has no newline yet
			return offset
		}
		offset += int64(len(line))
		feed(line)
	}
}

func (x *collabIndex) db(name, path string, load func(float64) []*CollabSession, since float64) []*CollabSession {
	version := dbVersion(path)
	if c, ok := x.dbs[name]; ok && c.version == version && c.since <= since {
		return c.sessions
	}
	sessions := load(since)
	x.dbs[name] = collabDB{version: version, since: since, sessions: sessions}
	return sessions
}

func nowSeconds() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// The sessions are shared with the caches and relinked on every rebuild, so everything that reads them (overview,
// page) runs under the index lock: requests come in on their own goroutines.

// overview of the last `days` days.
func (x *collabIndex) overview(days float64) map[string]any {
	x.mu.Lock()
	defer x.mu.Unlock()
	return collabOverview(x.refreshLocked(days))
}

// page of one session; false when it is not found.
func (x *collabIndex) page(key string, days float64, start int) (map[string]any, bool) {
	x.mu.Lock()
	defer x.mu.Unlock()
	sessions := x.refreshLocked(days)
	full := x.detailLocked(key)
	if full == nil {
		return nil, false
	}
	return collabDetail(full, sessions, start), true
}

// discussion of one session's delegations; false when it is not found.
func (x *collabIndex) discussion(key string, days float64) (map[string]any, bool) {
	x.mu.Lock()
	defer x.mu.Unlock()
	sessions := x.refreshLocked(days)
	owner := sessions[key]
	if owner == nil {
		return nil, false
	}
	return collabDiscussion(owner, sessions), true
}

// refresh returns the sessions active in the last `days` days, linked (tests).
func (x *collabIndex) refresh(days float64) map[string]*CollabSession {
	x.mu.Lock()
	defer x.mu.Unlock()
	return x.refreshLocked(days)
}

func (x *collabIndex) refreshLocked(days float64) map[string]*CollabSession {
	since := nowSeconds() - days*86400
	if x.sessions == nil || nowSeconds()-x.built >= collabReuse.Seconds() || x.since > since {
		found := x.logs(claudeCollabFiles(since), func(p string) collabParser { return newClaudeParser(p, false) })
		names := codexThreadNames()
		for _, s := range x.logs(codexCollabFiles(since), func(p string) collabParser { return newCodexParser(p, false) }) {
			if n := names[s.ID]; n != "" {
				s.Title = n
			}
			found = append(found, s)
		}
		found = append(found, x.db("hermes", collabHermesDB(), x.hermes.load, since)...)
		found = append(found, x.db("opencode", collabOpenCodeDB(), x.opencode.load, since)...)
		sessions := map[string]*CollabSession{}
		for _, s := range found {
			s.Parent, s.ParentCall, s.Link = "", "", ""
			sessions[s.Key()] = s
		}
		collabLink(sessions)
		x.sessions, x.built, x.since = sessions, nowSeconds(), since
	}
	out := map[string]*CollabSession{}
	for k, s := range x.sessions {
		if s.Ended >= since {
			out[k] = s
		}
	}
	return out
}

// detailLocked reads one whole session (every turn and tool call), keeping what linking found. The read is reused
// while the session's source is unchanged, so paging through a long session parses its log once.
func (x *collabIndex) detailLocked(key string) *CollabSession {
	base := x.sessions[key]
	agent, id, _ := strings.Cut(key, ":")
	var full *CollabSession
	switch {
	case agent == "hermes" || agent == "opencode":
		db := collabHermesDB()
		if agent == "opencode" {
			db = collabOpenCodeDB()
		}
		version := fmt.Sprint(dbVersion(db))
		if c, ok := x.details[key]; ok && c.version == version {
			full = c.session
		} else {
			x.parses++
			if agent == "hermes" {
				full = hermesDetail(id)
			} else {
				full = opencodeDetail(id)
			}
			if full != nil {
				x.details[key] = collabDetailCache{version: version, session: full}
			}
		}
	case base != nil && (agent == "claude" || agent == "codex"):
		// a log being paged through is read on from where it stopped, like the overview's
		newParser := func(p string) collabParser { return newClaudeParser(p, true) }
		if agent == "codex" {
			newParser = func(p string) collabParser { return newCodexParser(p, true) }
		}
		c, cached := x.detailLogs[base.Source]
		nc, read, isFull := readLog(base.Source, c, cached, newParser)
		if read {
			x.parses++
		}
		if isFull {
			x.fullParses++
		}
		if nc.parser != nil {
			x.keepDetailLog(base.Source, nc)
			full = nc.parser.session()
		}
	}
	if full != nil && base != nil {
		full.Parent, full.ParentCall, full.Link, full.Origin = base.Parent, base.ParentCall, base.Link, base.Origin
		if base.Title != "" {
			full.Title = base.Title // e.g. the name Codex keeps outside the rollout
		}
	}
	return full
}

// keepDetailLog keeps the full parse of the last few logs opened (they can be large).
func (x *collabIndex) keepDetailLog(path string, c collabFile) {
	x.detailOrder = slices.DeleteFunc(x.detailOrder, func(p string) bool { return p == path })
	x.detailOrder = append(x.detailOrder, path)
	x.detailLogs[path] = c
	for len(x.detailOrder) > collabDetailKept {
		delete(x.detailLogs, x.detailOrder[0])
		x.detailOrder = x.detailOrder[1:]
	}
	if len(x.details) > collabDetailKept { // database sessions: forget the oldest reads
		clear(x.details)
	}
}

// detail is detailLocked for callers outside the index (tests).
func (x *collabIndex) detail(key string) *CollabSession {
	x.mu.Lock()
	defer x.mu.Unlock()
	return x.detailLocked(key)
}
