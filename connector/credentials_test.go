package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFileCredentialsRoundTrip(t *testing.T) {
	dir := t.TempDir()
	a := WorkAccount{ID: "claude-x", Kind: "claude", Home: dir}
	if err := os.WriteFile(filepath.Join(dir, ".credentials.json"), []byte(`{"a":1}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := a.writeCredentials([]byte(`{"a":2}`)); err != nil {
		t.Fatal(err)
	}
	got, err := a.readCredentials()
	if err != nil || string(got) != `{"a":2}` {
		t.Fatalf("got %q %v", got, err)
	}
	if st, _ := os.Stat(filepath.Join(dir, ".credentials.json")); st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", st.Mode())
	}
}

func TestClaudeQuotaExpiredOrMissing(t *testing.T) {
	dir := t.TempDir()
	a := WorkAccount{ID: "claude-x", Kind: "claude", Home: dir}
	_ = os.WriteFile(filepath.Join(dir, ".credentials.json"), []byte(`{"claudeAiOauth":{"accessToken":"t","expiresAt":1000,"subscriptionType":"max"}}`), 0o600)
	info := readClaudeQuota(context.Background(), a)
	if info.Plan != "max" || !info.Usable || !strings.Contains(info.Error, "过期") {
		t.Fatalf("%+v", info)
	}
	_ = os.WriteFile(filepath.Join(dir, ".credentials.json"), []byte(`{}`), 0o600)
	if info := readClaudeQuota(context.Background(), a); !strings.Contains(info.Error, "claudeAiOauth") {
		t.Fatalf("%+v", info)
	}
}

func TestAddLocalAccountsKeepsExisting(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("CODEX_HOME", "")
	_ = os.MkdirAll(filepath.Join(home, ".claude"), 0o700)
	_ = os.WriteFile(filepath.Join(home, ".claude", ".credentials.json"), []byte(`{}`), 0o600)
	cfg := &Config{Agents: []AgentConfig{{Key: "codex", Kind: "codex", Workhost: true}, {Key: "claude", Kind: "claude", Workhost: true}},
		Workhost: &WorkhostConfig{Accounts: []WorkAccount{{ID: "codex-01", Kind: "codex", Home: "/x"}}}}
	addLocalAccounts(cfg)
	if n := len(cfg.Workhost.Accounts); n != 2 || cfg.Workhost.Accounts[1].Kind != "claude" || cfg.Workhost.Accounts[0].ID != "codex-01" {
		t.Fatalf("%+v", cfg.Workhost.Accounts)
	}
}
