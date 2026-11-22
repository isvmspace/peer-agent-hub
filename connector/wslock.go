package main

import (
	"context"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
)

// workspaceLocks lets only one task at a time work in a folder, across all
// agents of this connector (e.g. Claude and Codex sharing a repository).
// Nested folders count as the same folder. A lock is held until the agent's
// process has exited, never released on a network drop or hub timeout.
type workspaceLocks struct {
	mu      sync.Mutex
	holders map[string]int64 // canonical folder -> task id
	changed chan struct{}    // closed and replaced whenever a lock is released
}

func newWorkspaceLocks() *workspaceLocks {
	return &workspaceLocks{holders: map[string]int64{}, changed: make(chan struct{})}
}

// canonicalDir resolves symlinks and, on case-insensitive systems, case.
func canonicalDir(dir string) string {
	if abs, err := filepath.Abs(dir); err == nil {
		dir = abs
	}
	if real, err := filepath.EvalSymlinks(dir); err == nil {
		dir = real
	}
	dir = filepath.Clean(dir)
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		dir = strings.ToLower(dir)
	}
	return dir
}

func overlaps(a, b string) bool {
	sep := string(filepath.Separator)
	return a == b || strings.HasPrefix(a, strings.TrimSuffix(b, sep)+sep) || strings.HasPrefix(b, strings.TrimSuffix(a, sep)+sep)
}

// holderOf returns the task holding a folder overlapping dir, or 0.
func (l *workspaceLocks) holderOfLocked(dir string, self int64) int64 {
	for held, task := range l.holders {
		if task != self && overlaps(held, dir) {
			return task
		}
	}
	return 0
}

// acquire blocks until the folder is free or ctx ends. onWait is called with
// the holder each time the task has to wait for a different holder.
func (l *workspaceLocks) acquire(ctx context.Context, dir string, task int64, onWait func(holder int64)) (release func(), err error) {
	dir = canonicalDir(dir)
	var lastHolder int64
	for {
		l.mu.Lock()
		holder := l.holderOfLocked(dir, task)
		if holder == 0 {
			l.holders[dir] = task
			l.mu.Unlock()
			return func() { l.release(dir, task) }, nil
		}
		changed := l.changed
		l.mu.Unlock()
		if holder != lastHolder {
			lastHolder = holder
			onWait(holder)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-changed:
		}
	}
}

func (l *workspaceLocks) release(dir string, task int64) {
	l.mu.Lock()
	if l.holders[dir] == task {
		delete(l.holders, dir)
	}
	close(l.changed)
	l.changed = make(chan struct{})
	l.mu.Unlock()
}
