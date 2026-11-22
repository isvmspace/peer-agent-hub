package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCollectFiles(t *testing.T) {
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	work := t.TempDir()
	outside := t.TempDir()
	gen := filepath.Join(home, "generated_images", "sess-1")
	os.MkdirAll(gen, 0o755)
	for _, p := range []string{filepath.Join(gen, "a.png"), filepath.Join(work, "out", "b.jpg"), filepath.Join(outside, "c.png")} {
		os.MkdirAll(filepath.Dir(p), 0o755)
		os.WriteFile(p, []byte("img"), 0o644)
	}
	out := Outcome{SessionID: "sess-1", Result: "见 out/b.jpg 和 " + filepath.Join(outside, "c.png") + "，以及 missing.png"}
	start := time.Now().Add(-time.Minute)

	got := collectFiles(AgentConfig{Kind: "codex"}, work, out, start)
	if len(got) != 2 || filepath.Base(got[0]) != "a.png" || filepath.Base(got[1]) != "b.jpg" {
		t.Fatalf("sandboxed agent should only upload workspace/generated files, got %v", got)
	}
	got = collectFiles(AgentConfig{Kind: "codex", Isolation: "full"}, work, out, start)
	if len(got) != 3 {
		t.Fatalf("full isolation may upload mentioned files anywhere, got %v", got)
	}
}
