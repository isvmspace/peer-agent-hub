//go:build darwin

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProbeFolder(t *testing.T) {
	dir := t.TempDir()
	if got := probeFolder(dir, "ok"); got != "" {
		t.Fatalf("readable folder reported %q", got)
	}
	if got := probeFolder(filepath.Join(dir, "missing"), "missing"); got != "" {
		t.Fatalf("missing folder reported %q", got)
	}
	locked := filepath.Join(dir, "locked")
	if err := os.Mkdir(locked, 0o000); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(locked, 0o700)
	if os.Geteuid() == 0 {
		t.Skip("root can read any folder")
	}
	if got := probeFolder(locked, "~/Locked"); !strings.Contains(got, "~/Locked（无权限）") {
		t.Fatalf("locked folder reported %q", got)
	}
}
