//go:build windows

package main

import (
	"os"
	"os/exec"
	"syscall"
)

const (
	detachedProcess       = 0x00000008
	createNewProcessGroup = 0x00000200
)

// swapBinary moves the running exe aside (Windows forbids overwriting it but
// allows renaming) and puts the new one in its place, rolling back on failure.
func swapBinary(exe, newPath string) error {
	old := exe + ".old"
	_ = os.Remove(old)
	if err := os.Rename(exe, old); err != nil {
		return err
	}
	if err := os.Rename(newPath, exe); err != nil {
		_ = os.Rename(old, exe)
		return err
	}
	return nil
}

// restartSelf starts the new binary detached with the same arguments and exits.
func restartSelf(exe string) error {
	cmd := exec.Command(exe, os.Args[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow | detachedProcess | createNewProcessGroup}
	if err := cmd.Start(); err != nil {
		return err
	}
	os.Exit(0)
	return nil
}

func cleanupOldBinary() {
	if exe, err := executablePath(); err == nil {
		_ = os.Remove(exe + ".old")
	}
}
