//go:build !windows

package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeHermes answers `chat --help` like a stream-capable Hermes and replays body for a run.
func fakeHermes(t *testing.T, body string) string {
	return fakeCLI(t, `if [ "$1" = chat ] && [ "$2" = --help ]; then echo "--query-file PATH  --format {text,stream-json}"; exit 0; fi
`+body)
}

func TestHermesStreamsToolsAndHidesSecrets(t *testing.T) {
	args := filepath.Join(t.TempDir(), "args")
	cli := fakeHermes(t, `echo "$@" > `+args+`
echo '{"type":"system","subtype":"init","session_id":"hs1"}'
echo '{"type":"text","text":"先拉代码。"}'
echo '{"type":"tool_use","name":"terminal","tool_call_id":"c1","input":{"command":"git fetch ai-client-60.bundle"}}'
echo '{"type":"tool_result","name":"terminal","tool_call_id":"c1","output":"{\\"output\\": \\"done\\", \\"exit_code\\": 0, \\"error\\": null}","is_error":false}'
echo '{"type":"tool_use","name":"read_file","tool_call_id":"c2","input":{"path":"E:/x/.secrets/zentao-60.env"}}'
echo '{"type":"tool_result","name":"read_file","tool_call_id":"c2","output":"USER=a"}'
echo '{"type":"tool_use","name":"terminal","tool_call_id":"c3","input":{"command":"curl -H \"Authorization: Bearer abcdefghijklmnop\" http://x"}}'
echo '{"type":"tool_result","name":"terminal","tool_call_id":"c3","output":"token=zzzzzzzz ok","is_error":true}'
echo '{"type":"text","text":"打包完成。"}'
echo '{"type":"result","session_id":"hs1","exit_code":0,"text":"先拉代码。打包完成。","tokens":{"input":10,"output":3,"cache_read":1}}'
`)
	hermesStreamSupport.Delete(cli)
	ctx, events := sinkCollector()
	emit, lines := collect()
	out := runAgent(ctx, AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 7, Prompt: "x"}, emit)
	if out.Status != "completed" || out.Result != "先拉代码。打包完成。" || out.SessionID != "hs1" || out.Usage["input_tokens"] != int64(10) {
		t.Fatalf("outcome %+v", out)
	}
	a, _ := os.ReadFile(args)
	if !strings.Contains(string(a), "chat --query-file") || !strings.Contains(string(a), "--format stream-json") {
		t.Fatalf("args %q", a)
	}
	ev := *events
	if len(ev) != 6 || ev[0].Phase != "started" || !strings.Contains(ev[0].Summary, "git fetch") || ev[1].Summary != "done" {
		t.Fatalf("events %+v", ev)
	}
	if ev[2].Summary != hiddenNotice || ev[3].Summary != hiddenNotice {
		t.Fatalf("secret file must be hidden: %+v %+v", ev[2], ev[3])
	}
	if strings.Contains(ev[4].Summary, "abcdefghijklmnop") || strings.Contains(ev[5].Summary, "zzzzzzzz") || ev[5].Phase != "failed" {
		t.Fatalf("credential values must be masked: %+v %+v", ev[4], ev[5])
	}
	if strings.Join(*lines, "|") != "先拉代码。|打包完成。" {
		t.Fatalf("narration lines %q", *lines)
	}
}

func TestHermesStreamFailure(t *testing.T) {
	cli := fakeHermes(t, `echo '{"type":"result","exit_code":1,"text":"","error":"model unavailable"}'
exit 1
`)
	hermesStreamSupport.Delete(cli)
	emit, _ := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 8, Prompt: "x"}, emit)
	if out.Status != "failed" || out.Error != "model unavailable" {
		t.Fatalf("outcome %+v", out)
	}
}

func TestOldHermesFallsBackToOneShot(t *testing.T) {
	args := filepath.Join(t.TempDir(), "args")
	cli := fakeCLI(t, `if [ "$1" = chat ]; then echo "usage: hermes chat [-q QUERY]"; exit 0; fi
echo "$@" > `+args+`
echo final answer
`)
	hermesStreamSupport.Delete(cli)
	emit, _ := collect()
	out := runAgent(context.Background(), AgentConfig{Kind: "hermes", Command: cli}, Task{ID: 9, Prompt: "x"}, emit)
	a, _ := os.ReadFile(args)
	if out.Status != "completed" || out.Result != "final answer" || !strings.HasPrefix(string(a), "-z ") {
		t.Fatalf("outcome %+v args %q", out, a)
	}
}

func TestRedaction(t *testing.T) {
	for in, want := range map[string]string{
		"ls -la":                             "ls -la",
		"cat ~/.ssh/id_rsa":                  hiddenNotice,
		"type .env.local":                    hiddenNotice,
		"mysql -uroot -pSecret123 -e 'show'": "mysql -uroot -p*** -e 'show'",
		"git clone https://bob:hunter2@x/y":  "git clone https://bob:***@x/y",
		"curl -H 'X: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N'": "curl -H 'X: ***'",
	} {
		if got := redactTool(in); got != want {
			t.Errorf("redactTool(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestClaudeAndCodexTracesAreRedacted(t *testing.T) {
	claude := fakeCLI(t, `cat > /dev/null
echo '{"type":"system","subtype":"init","session_id":"s"}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"用 token=abcdef123 登录"},{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"cat ~/.ssh/id_rsa"}}]}}'
echo '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"KEYDATA"}]}}'
echo '{"type":"result","subtype":"success","is_error":false,"result":"ok","session_id":"s"}'
`)
	codex := fakeCLI(t, `cat > /dev/null
echo '{"type":"thread.started","thread_id":"th"}'
echo '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"cat .env"}}'
echo '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"cat .env","aggregated_output":"DB=1","exit_code":0,"status":"completed"}}'
echo '{"type":"item.completed","item":{"id":"m","type":"agent_message","text":"Authorization: Bearer abcdefghijkl"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
`)
	for kind, cli := range map[string]string{"claude": claude, "codex": codex} {
		ctx, events := sinkCollector()
		emit, lines := collect()
		out := runAgent(ctx, AgentConfig{Kind: kind, Command: cli}, Task{ID: 1, Prompt: "x"}, emit)
		if out.Status != "completed" || len(*events) != 2 {
			t.Fatalf("%s outcome %+v events %+v", kind, out, *events)
		}
		for _, ev := range *events {
			if ev.Summary != hiddenNotice {
				t.Fatalf("%s secret call must be hidden: %+v", kind, ev)
			}
		}
		all := strings.Join(*lines, "|")
		if strings.Contains(all, "abcdef123") || strings.Contains(all, "abcdefghijkl") {
			t.Fatalf("%s output not masked: %q", kind, all)
		}
	}
}
