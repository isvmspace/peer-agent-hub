//go:build !windows

package main

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestRedactionFormatsAndFalsePositives(t *testing.T) {
	masked := map[string]string{
		"Authorization: Basic Y3gzOnN5bnRoZXRpYy1wYXNzd29yZA==": "Y3gzOnN5",
		"ahp_abcdefghijklmnopqrst":                              "ahp_abc",
		"github_pat_11ABCDEFGHIJKLMNOPQRSTUV_xyz":               "ABCDEFGH",
		"AWS_ACCESS_KEY_ID=AKIACX3SYNTHETIC12345":               "SYNTHETIC",
		`{"auths":{"x":{"auth":"dXNlcjpwYXNz"}}}`:              "dXNlcjpw",
		"curl -u qa:CurlPassword123 http://x":                   "CurlPassword",
		"docker login -u qa -p DockerPassword123":               "DockerPassword",
		`{"password": "one remaining password"}`:                "remaining",
		`password="start,tail&value"`:                           "tail",
	}
	for in, leak := range masked {
		if got := redactText(in); strings.Contains(got, leak) {
			t.Errorf("%q leaked: %q", in, got)
		}
	}
	for _, keep := range []string{"token=4096", "pagination_token=next_page_002", "max_tokens=1000",
		"pytest tests/test_password_reset.py -q", "cat src/secretary.py", "read docs/credentials-guide.md", "author=bob"} {
		if got := redactTool(keep); got != keep {
			t.Errorf("%q changed to %q", keep, got)
		}
	}
}

func TestPrivateKeySplitAcrossLines(t *testing.T) {
	r := &outputRedactor{}
	got := []string{r.mask("-----BEGIN PRIVATE KEY-----"), r.mask("BODY0123456789"), r.mask("-----END PRIVATE KEY----- after")}
	if strings.Contains(strings.Join(got, "|"), "BODY") || !strings.HasSuffix(got[2], "after") || r.inKey {
		t.Fatalf("%q", got)
	}
}

func TestLongCommandStaysSensitive(t *testing.T) {
	cli := fakeHermes(t, `echo '{"type":"tool_use","name":"terminal","tool_call_id":"c1","input":{"command":"cat .env; echo `+strings.Repeat("x", 650)+`"}}'
echo '{"type":"tool_result","name":"terminal","tool_call_id":"c1","output":"OPAQUE_SECRET_VALUE"}'
echo '{"type":"result","exit_code":0,"text":"ok"}'
`)
	hermesStreamSupport.Delete(cli)
	ctx, events := sinkCollector()
	emit, _ := collect()
	runAgent(ctx, AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	for _, e := range *events {
		if strings.Contains(e.Summary, "OPAQUE") || e.Summary != hiddenNotice {
			t.Fatalf("events %+v", *events)
		}
	}
}

func TestHermesStderrIsMasked(t *testing.T) {
	cli := fakeHermes(t, `echo 'password=STDERR_PASSWORD_12345' >&2; exit 7`)
	hermesStreamSupport.Delete(cli)
	emit, _ := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "failed" || strings.Contains(out.Error, "STDERR_PASSWORD") {
		t.Fatalf("%+v", out)
	}
}

func TestHermesLongLineDoesNotHideFailure(t *testing.T) {
	cli := fakeHermes(t, `head -c 17000000 /dev/zero | tr '\0' 'a'; echo
echo '{"type":"result","exit_code":3,"error":"AFTER_LONG_LINE"}'
`)
	hermesStreamSupport.Delete(cli)
	emit, _ := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "failed" || !strings.Contains(out.Error, "AFTER_LONG_LINE") {
		t.Fatalf("%+v", out)
	}
}

func TestHermesWithoutResultFails(t *testing.T) {
	cli := fakeHermes(t, `exit 0`)
	hermesStreamSupport.Delete(cli)
	emit, _ := collect()
	if out := runAgent(context.Background(), AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 1, Prompt: "x"}, emit); out.Status != "failed" {
		t.Fatalf("%+v", out)
	}
}

func TestHermesProbeHonorsCancel(t *testing.T) {
	cli := fakeCLI(t, `if [ "$2" = --help ]; then sleep 3; echo "--query-file stream-json"; exit 0; fi
echo '{"type":"result","exit_code":0,"text":"ran after cancellation"}'`)
	hermesStreamSupport.Delete(cli)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	emit, _ := collect()
	start := time.Now()
	out := runAgent(ctx, AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "failed" || strings.Contains(out.Result, "ran after") || time.Since(start) > 2*time.Second {
		t.Fatalf("%+v after %s", out, time.Since(start))
	}
}

func TestHermesTextKeyAcrossFlushes(t *testing.T) {
	cli := fakeHermes(t, `echo '{"type":"text","text":"-----BEGIN PRIVATE KEY-----"}'
echo '{"type":"tool_use","name":"terminal","tool_call_id":"c1","input":{"command":"ls"}}'
echo '{"type":"text","text":"STREAM_KEY_BODY\n-----END PRIVATE KEY-----"}'
echo '{"type":"result","exit_code":0,"text":"done"}'
`)
	hermesStreamSupport.Delete(cli)
	emit, lines := collect()
	runAgent(context.Background(), AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if strings.Contains(strings.Join(*lines, "|"), "STREAM_KEY_BODY") {
		t.Fatalf("%q", *lines)
	}
}

func TestHermesProbeCancelKillsChildren(t *testing.T) {
	cli := fakeCLI(t, `if [ "$2" = --help ]; then sleep 3 & wait; exit 0; fi
echo '{"type":"result","exit_code":0,"text":"ran"}'`)
	hermesStreamSupport.Delete(cli)
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(300*time.Millisecond, cancel)
	emit, _ := collect()
	start := time.Now()
	out := runAgent(ctx, AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
	if out.Status != "failed" || time.Since(start) > 2500*time.Millisecond {
		t.Fatalf("%+v after %s", out, time.Since(start))
	}
}
