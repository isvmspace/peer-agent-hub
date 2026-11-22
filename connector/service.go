package main

import (
	"encoding/binary"
	"fmt"
	"html"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"unicode/utf16"
)

// Autostart runs in the user's login session (not as a system service) so the
// agents see the same PATH, credentials and logins as the user's own terminal.

const serviceLabel = "dev.agent-hub.connector"

func installService(configPath string) error {
	if _, err := loadConfig(configPath); err != nil {
		return err
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	exe, _ = filepath.EvalSymlinks(exe)
	configPath, _ = filepath.Abs(configPath)
	switch runtime.GOOS {
	case "darwin":
		return installLaunchd(exe, configPath)
	case "windows":
		return installWindows(exe, configPath)
	case "linux":
		return installSystemd(exe, configPath)
	}
	return fmt.Errorf("不支持的系统：%s", runtime.GOOS)
}

func uninstallService() error {
	switch runtime.GOOS {
	case "darwin":
		plist := launchdPlistPath()
		_ = exec.Command("launchctl", "bootout", fmt.Sprintf("gui/%d", os.Getuid()), plist).Run()
		_ = os.Remove(plist)
	case "windows":
		_ = exec.Command("schtasks", "/End", "/TN", "AgentHubConnector").Run()
		if out, err := exec.Command("schtasks", "/Delete", "/F", "/TN", "AgentHubConnector").CombinedOutput(); err != nil {
			return fmt.Errorf("删除计划任务失败：%s", out)
		}
	case "linux":
		_ = exec.Command("systemctl", "--user", "disable", "--now", "agent-hub-connector").Run()
		home, _ := os.UserHomeDir()
		_ = os.Remove(filepath.Join(home, ".config/systemd/user/agent-hub-connector.service"))
	}
	fmt.Println("已移除自启")
	return nil
}

func launchdPlistPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library/LaunchAgents", serviceLabel+".plist")
}

func installLaunchd(exe, configPath string) error {
	esc := html.EscapeString
	logPath := filepath.Join(filepath.Dir(configPath), "launchd.log")
	plist := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array><string>%s</string><string>run</string><string>--config</string><string>%s</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>%s</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict></plist>
`, serviceLabel, esc(exe), esc(configPath), esc(os.Getenv("PATH")), esc(logPath), esc(logPath))
	path := launchdPlistPath()
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, []byte(plist), 0o644); err != nil {
		return err
	}
	domain := fmt.Sprintf("gui/%d", os.Getuid())
	_ = exec.Command("launchctl", "bootout", domain, path).Run()
	if out, err := exec.Command("launchctl", "bootstrap", domain, path).CombinedOutput(); err != nil {
		return fmt.Errorf("launchctl bootstrap 失败：%s", out)
	}
	fmt.Printf("已安装登录自启（launchd）：%s\n日志：%s（启动错误见 %s）\n", path, filepath.Join(filepath.Dir(configPath), "connector.log"), logPath)
	return nil
}

func installWindows(exe, configPath string) error {
	// A VBScript launcher starts the connector without a console window.
	vbs := filepath.Join(filepath.Dir(configPath), "start-connector.vbs")
	q := func(s string) string { return `""` + strings.ReplaceAll(s, `"`, `""`) + `""` }
	script := fmt.Sprintf("CreateObject(\"WScript.Shell\").Run \"%s run --config %s\", 0, False\r\n", q(exe), q(configPath))
	// wscript reads UTF-16LE with BOM correctly, so non-ASCII user folders keep working.
	if err := os.WriteFile(vbs, utf16LE(script), 0o644); err != nil {
		return err
	}
	tr := fmt.Sprintf(`wscript.exe "%s"`, vbs)
	if out, err := exec.Command("schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", "AgentHubConnector", "/TR", tr).CombinedOutput(); err != nil {
		return fmt.Errorf("创建计划任务失败：%s", out)
	}
	if out, err := exec.Command("schtasks", "/Run", "/TN", "AgentHubConnector").CombinedOutput(); err != nil {
		return fmt.Errorf("启动计划任务失败：%s", out)
	}
	fmt.Printf("已安装登录自启（计划任务 AgentHubConnector）并已启动\n日志：%s\n", filepath.Join(filepath.Dir(configPath), "connector.log"))
	return nil
}

func installSystemd(exe, configPath string) error {
	home, _ := os.UserHomeDir()
	unit := fmt.Sprintf(`[Unit]
Description=Agent Hub connector
After=network-online.target

[Service]
ExecStart=%s run --config %s
Environment=PATH=%s
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
`, exe, configPath, os.Getenv("PATH"))
	path := filepath.Join(home, ".config/systemd/user/agent-hub-connector.service")
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, []byte(unit), 0o644); err != nil {
		return err
	}
	_ = exec.Command("systemctl", "--user", "daemon-reload").Run()
	if out, err := exec.Command("systemctl", "--user", "enable", "--now", "agent-hub-connector").CombinedOutput(); err != nil {
		return fmt.Errorf("systemctl 失败：%s", out)
	}
	fmt.Println("已安装 systemd 用户服务 agent-hub-connector")
	return nil
}

func utf16LE(s string) []byte {
	units := utf16.Encode([]rune(s))
	out := make([]byte, 2+2*len(units))
	out[0], out[1] = 0xFF, 0xFE
	for i, u := range units {
		binary.LittleEndian.PutUint16(out[2+2*i:], u)
	}
	return out
}
