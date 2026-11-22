package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestNewerThan(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"0.2.0", "0.1.9", true}, {"0.10.0", "0.9.0", true}, {"0.1.0", "0.1.0", false},
		{"0.1.0", "0.2.0", false}, {"dev", "0.1.0", false}, {"1.0.0", "dev", false},
	}
	for _, c := range cases {
		if got := newerThan(c.a, c.b); got != c.want {
			t.Errorf("newerThan(%s,%s)=%v", c.a, c.b, got)
		}
	}
}

func fakeHub(t *testing.T, artifact []byte, advertisedSHA string) *httptest.Server {
	t.Helper()
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer ahc_test" && r.URL.Path != "/artifact" {
			w.WriteHeader(401)
			return
		}
		switch r.URL.Path {
		case "/api/connector/latest":
			_ = json.NewEncoder(w).Encode(UpdateInfo{UpdateAvailable: true, ReleaseID: 7, Platform: platform(),
				LatestVersion: "0.2.0", FileSize: int64(len(artifact)), SHA256: advertisedSHA, Policy: "OPTIONAL"})
		case "/api/connector/download-ticket":
			_ = json.NewEncoder(w).Encode(downloadTicket{DownloadURL: srv.URL + "/artifact", ExpiresAt: 1})
		case "/artifact":
			_, _ = w.Write(artifact)
		}
	}))
	return srv
}

func withVersion(t *testing.T, v string) {
	old := version
	version = v
	t.Cleanup(func() { version = old })
}

func TestDownloadUpdateVerifies(t *testing.T) {
	withVersion(t, "0.1.0")
	artifact := []byte("new-binary")
	sum := sha256.Sum256(artifact)
	srv := fakeHub(t, artifact, hex.EncodeToString(sum[:]))
	defer srv.Close()
	cfg := &Config{HubURL: srv.URL, Token: "ahc_test"}
	info, err := checkUpdate(context.Background(), cfg)
	if err != nil || !info.UpdateAvailable {
		t.Fatalf("check: %+v %v", info, err)
	}
	path, err := downloadUpdate(context.Background(), cfg, info)
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(path)
	got, _ := os.ReadFile(path)
	if string(got) != "new-binary" || !strings.HasSuffix(path, ".new") {
		t.Fatalf("bad download %s %q", path, got)
	}
}

func TestDownloadUpdateRejectsTampered(t *testing.T) {
	withVersion(t, "0.1.0")
	srv := fakeHub(t, []byte("evil-binary"), strings.Repeat("a", 64))
	defer srv.Close()
	cfg := &Config{HubURL: srv.URL, Token: "ahc_test"}
	info, _ := checkUpdate(context.Background(), cfg)
	if _, err := downloadUpdate(context.Background(), cfg, info); err == nil || !strings.Contains(err.Error(), "SHA-256") {
		t.Fatalf("expected sha mismatch, got %v", err)
	}
	exe, _ := executablePath()
	if _, err := os.Stat(exe + ".new"); err == nil {
		t.Fatal("tampered file must not be kept")
	}
}

func TestValidUpdateRejectsDowngradeAndDev(t *testing.T) {
	withVersion(t, "0.3.0")
	info := &UpdateInfo{UpdateAvailable: true, Platform: platform(), LatestVersion: "0.2.0", FileSize: 1, SHA256: strings.Repeat("a", 64)}
	if validUpdate(info) == nil {
		t.Fatal("downgrade accepted")
	}
	withVersion(t, "dev")
	if info, _ := checkUpdate(context.Background(), &Config{HubURL: "http://127.0.0.1:1", Token: "x"}); info.UpdateAvailable {
		t.Fatal("dev build should not update")
	}
}
