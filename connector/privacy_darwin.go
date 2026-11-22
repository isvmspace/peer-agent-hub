//go:build darwin

package main

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// macOS privacy protection (TCC) applies to the connector as a background service:
// reading ~/Documents, ~/Desktop or ~/Downloads can block until the user allows it.
// Agent CLIs inherit this, so e.g. a Claude Code setting that points into
// ~/Documents makes every Claude run hang in its own startup.

const (
	folderProbeTimeout = 5 * time.Second
	folderProbeTTL     = 10 * time.Minute // granting access takes effect without restarting the connector
)

var (
	folderMu      sync.Mutex
	folderChecked time.Time
	folderResult  string
)

// blockedFolders describes the protected folders this process could not read, or "" when all are fine.
func blockedFolders() string {
	folderMu.Lock()
	defer folderMu.Unlock()
	if !folderChecked.IsZero() && time.Since(folderChecked) < folderProbeTTL {
		return folderResult
	}
	home, _ := os.UserHomeDir()
	names := []string{"Documents", "Desktop", "Downloads"}
	problems := make([]string, len(names))
	var wg sync.WaitGroup
	for i, name := range names {
		wg.Add(1)
		go func(i int, name string) {
			defer wg.Done()
			problems[i] = probeFolder(filepath.Join(home, name), "~/"+name)
		}(i, name)
	}
	wg.Wait() // probes run side by side, so the whole check takes at most one timeout
	var found []string
	for _, p := range problems {
		if p != "" {
			found = append(found, p)
		}
	}
	folderResult = ""
	if len(found) > 0 {
		folderResult = "连接器无法读取 " + strings.Join(found, "、") +
			"，疑似 macOS 隐私权限未授权。智能体若在启动时读取这些目录（如 Claude Code 设置里引用了 ~/Documents 下的目录）会一直卡住。" +
			"可在 系统设置 → 隐私与安全性 → 完整磁盘访问权限 中添加并允许 agent-hub-connector（连接器升级后可能需要重新允许）"
	}
	folderChecked = time.Now()
	return folderResult
}

// probeFolder returns "" when dir is readable, otherwise a short description of what happened.
func probeFolder(dir, label string) string {
	done := make(chan error, 1) // buffered: a read that finishes after the timeout must not block
	go func() {
		_, err := os.ReadDir(dir)
		done <- err
	}()
	select {
	case err := <-done:
		switch {
		case err == nil, os.IsNotExist(err):
			return ""
		case os.IsPermission(err):
			return label + "（无权限）"
		default:
			return label + "（" + err.Error() + "）"
		}
	case <-time.After(folderProbeTimeout):
		return label + "（读取 5 秒无响应）"
	}
}
