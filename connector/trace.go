package main

import (
	"context"
	"os/exec"
	"strings"
	"time"
)

// ToolEvent is one observed phase of a tool call. A call may be seen only
// finishing (no start) or only starting (killed mid-call); nothing is invented.
type ToolEvent struct {
	CallID   string
	Name     string
	Phase    string // started | completed | failed
	Summary  string
	ExitCode *int
}

type toolSinkKey struct{}

// withToolSink makes runAgent report tool calls as structured events. Without
// it (the hub is too old to store them) tools are reported as text lines.
func withToolSink(ctx context.Context, sink func(ToolEvent)) context.Context {
	return context.WithValue(ctx, toolSinkKey{}, sink)
}

func toolSink(ctx context.Context) func(ToolEvent) {
	sink, _ := ctx.Value(toolSinkKey{}).(func(ToolEvent))
	return sink
}

// toolReporter routes a tool call either to the structured sink or, for old
// hubs, to the readable text the console showed before.
type toolReporter struct {
	sink  func(ToolEvent)
	emit  Emitter
	names map[string]string
	// hidden remembers calls whose input targets secrets, so their output is hidden too.
	hidden map[string]bool
}

func newToolReporter(ctx context.Context, emit Emitter) *toolReporter {
	return &toolReporter{sink: toolSink(ctx), emit: emit, names: map[string]string{}, hidden: map[string]bool{}}
}

// started reports the start of a call; legacy says whether old hubs got a text line for it.
func (r *toolReporter) started(id, name, summary string, legacy bool) {
	r.names[id] = name
	if sensitiveCall(summary) {
		r.hidden[id] = true
	}
	summary = redactTool(summary)
	if r.sink != nil {
		r.sink(ToolEvent{CallID: id, Name: name, Phase: "started", Summary: truncate(summary, 600)})
		return
	}
	if legacy {
		r.emit("output", strings.TrimSpace("🔧 "+name+" "+truncate(summary, 600)))
	}
}

// finished reports the end of a call; legacyText is what old hubs get.
func (r *toolReporter) finished(id, name string, failed bool, summary string, exit *int, legacyText string) {
	if name == "" {
		name = r.names[id]
	}
	delete(r.names, id)
	if r.hidden[id] || sensitiveCall(summary) {
		summary = hiddenNotice
		if legacyText != "" {
			legacyText = strings.TrimSpace("🔧 " + name + " " + hiddenNotice)
		}
	} else {
		summary, legacyText = redactText(summary), redactText(legacyText)
	}
	delete(r.hidden, id)
	if r.sink != nil {
		phase := "completed"
		if failed {
			phase = "failed"
		}
		r.sink(ToolEvent{CallID: id, Name: name, Phase: phase, Summary: truncate(summary, 600), ExitCode: exit})
		return
	}
	if legacyText != "" {
		r.emit("output", legacyText)
	}
}

// Usage holds token and cost numbers exactly as the CLI reported them.
type Usage map[string]any

// codeRef records the git baseline of a working folder after a turn, so a
// deliverable can be tied to the code it was produced against.
func codeRef(dir string) map[string]string {
	if dir == "" {
		return nil
	}
	run := func(args ...string) (string, bool) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
		prepareProcess(cmd) // no console window on Windows
		out, err := cmd.Output()
		return strings.TrimSpace(string(out)), err == nil
	}
	commit, ok := run("rev-parse", "HEAD")
	if !ok || commit == "" {
		return nil
	}
	ref := map[string]string{"commit": commit}
	if branch, ok := run("rev-parse", "--abbrev-ref", "HEAD"); ok {
		ref["branch"] = branch
	}
	if status, ok := run("status", "--porcelain", "--untracked-files=no"); ok {
		if status == "" {
			ref["dirty"] = "false"
		} else {
			ref["dirty"] = "true"
		}
	}
	return ref
}
