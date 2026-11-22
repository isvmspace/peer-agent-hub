package main

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"testing"
)

// Runs only with AHW_DOCKER_TEST=1 and the work image built: a container another hub left behind is never reused,
// and a sleeping container is recreated on its volume when its spec changes.
func TestEnsureOwnershipAndSpec(t *testing.T) {
	if os.Getenv("AHW_DOCKER_TEST") == "" {
		t.Skip("set AHW_DOCKER_TEST=1")
	}
	ctx := context.Background()
	w := &WorkContext{User: "utest-own"}
	cleanup := func() {
		_ = exec.Command("docker", "rm", "-f", "ahw-utest-own").Run()
		_ = exec.Command("docker", "volume", "rm", "ahw-utest-own").Run()
	}
	cleanup()
	defer cleanup()
	h := newWorkhost(WorkhostConfig{Image: "agent-hub-work:latest"})
	h.hub = "http://hub-a"
	if err := h.ensure(ctx, w); err != nil {
		t.Fatal(err)
	}
	if _, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", "echo keep > /home/dev/marker"); err != nil {
		t.Fatal(err)
	}
	other := newWorkhost(WorkhostConfig{Image: "agent-hub-work:latest"})
	other.hub = "http://hub-b"
	if err := other.ensure(ctx, w); err == nil || !strings.Contains(err.Error(), "不是这个 Hub") {
		t.Fatalf("foreign container reused: %v", err)
	}
	_ = exec.Command("docker", "stop", "-t", "1", "ahw-utest-own").Run()
	changed := newWorkhost(WorkhostConfig{Image: "agent-hub-work:latest", Memory: "1g"})
	changed.hub = "http://hub-a"
	if err := changed.ensure(ctx, w); err != nil {
		t.Fatal(err)
	}
	mem, _ := h.docker(ctx, nil, nil, "inspect", "-f", "{{.HostConfig.Memory}}", "ahw-utest-own")
	if strings.TrimSpace(mem) != "1073741824" {
		t.Fatalf("not recreated with new spec: %q", mem)
	}
	if out, err := h.inContainer(ctx, w, nil, nil, "cat", "/home/dev/marker"); err != nil || strings.TrimSpace(out) != "keep" {
		t.Fatalf("volume lost: %q %v", out, err)
	}
	_ = exec.Command("docker", "rm", "-f", "ahw-utest-own").Run()
	if err := other.ensure(ctx, w); err == nil || !strings.Contains(err.Error(), "数据卷") {
		t.Fatalf("foreign volume reused: %v", err)
	}
}

// The colleague asked to work on another branch and the agent checked it out: the session branch follows it, and
// only the agent's own commits count as changes, not the other branch's history.
func TestSessionFollowsSwitchedBranch(t *testing.T) {
	if os.Getenv("AHW_DOCKER_TEST") == "" {
		t.Skip("set AHW_DOCKER_TEST=1")
	}
	ctx := context.Background()
	cleanup := func() {
		_ = exec.Command("docker", "rm", "-f", "ahw-utest-br").Run()
		_ = exec.Command("docker", "volume", "rm", "ahw-utest-br").Run()
	}
	cleanup()
	defer cleanup()
	h := newWorkhost(WorkhostConfig{Image: "agent-hub-work:latest"})
	h.hub = "http://hub-a"
	w := &WorkContext{User: "utest-br", GitName: "T", GitEmail: "t@x", Session: "s-1", Branch: "agent/t/s-1", Folder: "d-1",
		Repos: []WorkRepo{{Name: "demo", URL: "file:///home/dev/remote.git", Branch: "master"}}}
	if err := h.ensure(ctx, w); err != nil {
		t.Fatal(err)
	}
	setup := `set -e; cd /home/dev; git init -q --bare -b master remote.git; git clone -q remote.git seed; cd seed
echo a > a; git add a; git commit -qm base; git push -q origin master
git checkout -qb feature/v1.4.0; for i in 1 2 3; do echo $i >> a; git commit -qam "v140 $i"; done; git push -q origin feature/v1.4.0`
	if out, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", setup); err != nil {
		t.Fatal(out, err)
	}
	if err := h.prepareSession(ctx, w); err != nil {
		t.Fatal(err)
	}
	dst := w.sessionDir() + "/demo"
	agent := `set -e; cd ` + dst + `; git checkout -qb feature/v1.4.0 origin/feature/v1.4.0; echo mine > b; git add b; git commit -qm "agent work"`
	if out, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", agent); err != nil {
		t.Fatal(out, err)
	}
	h.keepSessionBranch(ctx, w)
	// an older version left the branch tracking a branch its code is not on: the code wins and the upstream is repaired
	if out, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", `set -e; cd /home/dev/seed; git checkout -q master; echo m > m; git add m; git commit -qm hotfix; git push -q origin master; git checkout -q feature/v1.4.0`); err != nil {
		t.Fatal(out, err)
	}
	_ = h.prepareSession(ctx, w)
	_, _ = h.git(ctx, w, false, "-C", dst, "branch", "--set-upstream-to=origin/master")
	if base := h.baseBranch(ctx, w, dst, "master"); base != "feature/v1.4.0" {
		t.Fatalf("mismatched upstream not repaired: %s", base)
	}
	if up, _ := h.git(ctx, w, false, "-C", dst, "rev-parse", "--abbrev-ref", "@{u}"); strings.TrimSpace(up) != "origin/feature/v1.4.0" {
		t.Fatalf("upstream: %q", up)
	}
	if cur, _ := h.git(ctx, w, false, "-C", dst, "branch", "--show-current"); strings.TrimSpace(cur) != w.Branch {
		t.Fatalf("not back on the session branch: %q", cur)
	}
	st, err := h.status(ctx, w)
	if err != nil {
		t.Fatal(err)
	}
	if len(st[0].Commits) != 1 || st[0].Commits[0].Subject != "agent work" || st[0].Unpushed != 1 || st[0].Base != "feature/v1.4.0" {
		t.Fatalf("want only the agent's commit, got %+v", st[0])
	}
	if err := h.clone(ctx, w, WorkRepo{Name: "demo", URL: "file:///home/dev/remote.git", Branch: "nope"}); err == nil || !strings.Contains(err.Error(), "没有分支") {
		t.Fatalf("a branch missing on the remote must be refused: %v", err)
	}
	if err := h.clone(ctx, w, WorkRepo{Name: "demo", URL: "file:///home/dev/remote.git", Branch: "feature/v1.4.0"}); err != nil {
		t.Fatal(err)
	}
	// someone pushes to 1.4.0; the next turn's sync brings it, and the agent can merge it without credentials
	if out, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", `set -e; cd /home/dev/seed; echo new >> a; git commit -qam "teammate"; git push -q origin feature/v1.4.0`); err != nil {
		t.Fatal(out, err)
	}
	if err := h.prepareSession(ctx, w); err != nil || len(w.syncFailed) > 0 {
		t.Fatalf("sync: %v %v", err, w.syncFailed)
	}
	if len(w.bases) != 1 || w.bases[0] != "demo → feature/v1.4.0" {
		t.Fatalf("bases: %v", w.bases)
	}
	if out, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", "cd "+dst+" && git rebase -q origin/feature/v1.4.0 && git log -1 --format=%s HEAD^"); err != nil || strings.TrimSpace(out) != "teammate" {
		t.Fatalf("latest code not reachable: %q %v", out, err)
	}
	if res, err := h.push(ctx, w, nil); err != nil || !res[0].Pushed || res[0].Branch != "feature/v1.4.0" {
		t.Fatalf("push: %+v %v", res, err)
	}
	// pushed straight to the branch the session works on, no session branch on the remote
	if out, _ := h.inContainer(ctx, w, nil, nil, "sh", "-c", "git -C /home/dev/remote.git log -3 --format=%s feature/v1.4.0; echo branches: $(git -C /home/dev/remote.git branch --list 'agent/*')"); !strings.Contains(out, "agent work") || !strings.HasSuffix(strings.TrimSpace(out), "branches:") || strings.Contains(out, "Merge") {
		t.Fatalf("remote: %q", out)
	}
	if st, _ = h.status(ctx, w); len(st[0].Commits) != 0 || st[0].Unpushed != 0 || st[0].Base != "feature/v1.4.0" {
		t.Fatalf("after push want nothing pending on feature/v1.4.0, got %+v", st[0])
	}
	// someone else pushed in between: the push is refused with a plain hint instead of overwriting their work
	if out, err := h.inContainer(ctx, w, nil, nil, "sh", "-c", `set -e; cd /home/dev/seed; git pull -q --no-rebase origin feature/v1.4.0; echo x >> a; git commit -qam other; git push -q origin feature/v1.4.0
cd `+dst+`; echo c > c; git add c; git commit -qm "agent again"`); err != nil {
		t.Fatal(out, err)
	}
	_ = h.prepareSession(ctx, w)
	if res, _ := h.push(ctx, w, nil); res[0].Pushed || !strings.Contains(res[0].Error, "拉取最新代码") {
		t.Fatalf("a non-fast-forward push must be refused with a hint: %+v", res)
	}
}

// Images attached to a work message land in the container for the turn and are gone afterwards.
func TestContainerImagesForOneTurn(t *testing.T) {
	if os.Getenv("AHW_DOCKER_TEST") == "" {
		t.Skip("set AHW_DOCKER_TEST=1")
	}
	ctx := context.Background()
	cleanup := func() {
		_ = exec.Command("docker", "rm", "-f", "ahw-utest-img").Run()
		_ = exec.Command("docker", "volume", "rm", "ahw-utest-img").Run()
	}
	cleanup()
	defer cleanup()
	h := newWorkhost(WorkhostConfig{Image: "agent-hub-work:latest"})
	h.hub = "http://hub-a"
	task := Task{Turn: 2, Work: &WorkContext{User: "utest-img", Session: "s-9"},
		Images: []TaskImage{{Name: "截图 1.png", Data: "iVBORw0KGgo="}, {Name: "../x.png", Data: "iVBORw0KGgo="}}}
	if err := h.ensure(ctx, task.Work); err != nil {
		t.Fatal(err)
	}
	done, err := h.writeContainerImages(ctx, &task)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"/home/dev/sessions/s-9/.ahw-images/turn-2/1-截图_1.png", "/home/dev/sessions/s-9/.ahw-images/turn-2/2-x.png"}
	if strings.Join(task.ImagePaths, "|") != strings.Join(want, "|") {
		t.Fatalf("paths: %v", task.ImagePaths)
	}
	if out, err := h.inContainer(ctx, task.Work, nil, nil, "sh", "-c", `head -c 4 "$0" | od -An -c`, want[0]); err != nil || !strings.Contains(out, "P   N   G") {
		t.Fatalf("content: %q %v", out, err)
	}
	done()
	if h.exists(ctx, task.Work, want[0]) {
		t.Fatal("images must be removed after the turn")
	}
}
