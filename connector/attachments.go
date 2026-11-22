package main

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// Files an agent produced in a turn (generated images, mostly) are sent back to the hub so the
// conversation can show them. Candidates are Codex's generated_images folder for the session and
// image paths mentioned in the reply; outside "full" isolation they must sit inside the workspace.

const (
	maxAttachments     = 6
	maxAttachmentBytes = 8 << 20
)

var imagePathRe = regexp.MustCompile("(?i)[^\\s\"'()<>\\[\\]`*]+\\.(?:png|jpe?g|gif|webp)")

func codexHome() string {
	if v := os.Getenv("CODEX_HOME"); v != "" {
		return v
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".codex")
}

func within(path, root string) bool {
	if root == "" {
		return false
	}
	rel, err := filepath.Rel(filepath.Clean(root), filepath.Clean(path))
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func collectFiles(a AgentConfig, workdir string, out Outcome, start time.Time) []string {
	var found []string
	seen := map[string]bool{}
	add := func(path string, roots ...string) {
		if len(found) >= maxAttachments || seen[path] {
			return
		}
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > maxAttachmentBytes {
			return
		}
		if a.Isolation != "full" {
			ok := false
			for _, r := range roots {
				ok = ok || within(path, r)
			}
			if !ok {
				return
			}
		}
		seen[path] = true
		found = append(found, path)
	}

	var generated string
	if a.Kind == "codex" && out.SessionID != "" {
		generated = filepath.Join(codexHome(), "generated_images", out.SessionID)
		entries, _ := os.ReadDir(generated)
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, e := range entries {
			if info, err := e.Info(); err == nil && !info.ModTime().Before(start.Add(-2*time.Second)) &&
				imagePathRe.MatchString(e.Name()) {
				add(filepath.Join(generated, e.Name()), generated)
			}
		}
	}
	for _, m := range imagePathRe.FindAllString(out.Result, -1) {
		m = strings.TrimPrefix(m, "file://")
		if !filepath.IsAbs(m) {
			m = filepath.Join(workdir, m)
		}
		add(filepath.Clean(m), workdir, generated)
	}
	return found
}

func attachmentMessage(t Task, path string) map[string]any {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return map[string]any{
		"type": "task_attachment", "task_id": t.ID, "turn": t.Turn,
		"name": filepath.Base(path), "data": base64.StdEncoding.EncodeToString(data),
	}
}
