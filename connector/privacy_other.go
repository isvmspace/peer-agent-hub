//go:build !darwin

package main

// blockedFolders: only macOS gates folder access per process.
func blockedFolders() string { return "" }
