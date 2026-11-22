//go:build !windows

package main

import (
	"os"
	"syscall"
)

// swapBinary renames over the running executable, which Unix allows.
func swapBinary(exe, newPath string) error {
	return os.Rename(newPath, exe)
}

// restartSelf replaces this process image; launchd/systemd keep the same PID.
func restartSelf(exe string) error {
	return syscall.Exec(exe, os.Args, os.Environ())
}

func cleanupOldBinary() {}
