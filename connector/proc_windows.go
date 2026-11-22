//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"syscall"
)

const createNoWindow = 0x08000000

func prepareProcess(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
}

func killProcessTree(cmd *exec.Cmd) {
	if cmd.Process != nil {
		kill := exec.Command("taskkill", "/T", "/F", "/PID", fmt.Sprint(cmd.Process.Pid))
		kill.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
		_ = kill.Run()
	}
}

// cleanupGroup is a no-op on Windows; cancel uses taskkill /T while the root is alive.
func cleanupGroup(cmd *exec.Cmd) {}
