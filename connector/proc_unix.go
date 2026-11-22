//go:build !windows

package main

import (
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

// prepareProcess puts the agent in its own process group so cancel kills its children too.
func prepareProcess(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// killProcessTree kills the agent's process group plus every descendant, since
// agent CLIs (e.g. Codex) start tool commands in their own process groups.
func killProcessTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	root := cmd.Process.Pid
	pids := descendants(root)
	_ = syscall.Kill(-root, syscall.SIGKILL)
	for _, pid := range pids {
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
}

func descendants(root int) []int {
	out, err := exec.Command("ps", "-A", "-o", "pid=,ppid=").Output()
	if err != nil {
		return nil
	}
	children := map[int][]int{}
	for _, line := range strings.Split(string(out), "\n") {
		f := strings.Fields(line)
		if len(f) != 2 {
			continue
		}
		pid, err1 := strconv.Atoi(f[0])
		ppid, err2 := strconv.Atoi(f[1])
		if err1 == nil && err2 == nil {
			children[ppid] = append(children[ppid], pid)
		}
	}
	var result []int
	queue := []int{root}
	for len(queue) > 0 {
		p := queue[0]
		queue = queue[1:]
		for _, c := range children[p] {
			result = append(result, c)
			queue = append(queue, c)
		}
	}
	return result
}

// cleanupGroup kills whatever is left in the agent's process group after it exits.
func cleanupGroup(cmd *exec.Cmd) {
	if cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
}
