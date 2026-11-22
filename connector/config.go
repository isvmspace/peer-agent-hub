package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// AgentConfig describes one local agent CLI that this connector exposes to the hub.
type AgentConfig struct {
	Key         string            `json:"key"`
	Name        string            `json:"name"`
	Kind        string            `json:"kind"` // claude | codex | hermes | command
	Command     string            `json:"command,omitempty"`
	Args        []string          `json:"args,omitempty"` // extra args; for kind=command "{prompt}" is replaced, otherwise prompt goes to stdin
	Workdir     string            `json:"workdir,omitempty"`
	Description string            `json:"description,omitempty"`
	Tags        []string          `json:"tags,omitempty"`
	Env         map[string]string `json:"env,omitempty"`
	TimeoutMin  int               `json:"timeout_minutes,omitempty"`
	// Workspaces are the folders tasks may choose to run in; Workdir is the default.
	Workspaces []Workspace `json:"workspaces,omitempty"`
	// Isolation: "workspace" (default) confines writes to the task's folder via the
	// CLI's own sandbox; "full" restores unrestricted permissions for this agent.
	Isolation string `json:"isolation,omitempty"`
	// SandboxDomains lists hosts Claude's sandbox may reach (default: all).
	SandboxDomains []string `json:"sandbox_domains,omitempty"`
	// Workhost offers this agent to the 「工作」menu when the connector runs as a work server
	Workhost bool `json:"workhost,omitempty"`
	// Wrap prefixes the CLI invocation (workhost mode runs it through `docker exec`); never configured by hand
	Wrap []string `json:"-"`
}

// isolationLevel describes what actually confines this agent on this OS:
// sandbox = OS-level write sandbox, limited = edits/dangerous commands blocked
// without an OS sandbox, none = full permissions.
func (a AgentConfig) isolationLevel() string {
	if a.Isolation == "full" {
		return "none"
	}
	switch a.Kind {
	case "codex":
		return "sandbox"
	case "claude":
		if runtime.GOOS == "windows" {
			return "limited"
		}
		return "sandbox"
	case "hermes":
		return "limited"
	}
	return "none"
}

type Workspace struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// cleanWorkspaces keeps absolute paths of existing directories, deduplicated.
func cleanWorkspaces(in []Workspace) (out []Workspace, rejected []string) {
	seen := map[string]bool{}
	for _, w := range in {
		p := strings.TrimSpace(w.Path)
		if p == "" || seen[p] {
			continue
		}
		if st, err := os.Stat(p); !filepath.IsAbs(p) || err != nil || !st.IsDir() {
			rejected = append(rejected, p)
			continue
		}
		seen[p] = true
		if w.Name == "" {
			w.Name = filepath.Base(p)
		}
		out = append(out, Workspace{Name: w.Name, Path: p})
	}
	return out, rejected
}

// resolveWorkdir returns the directory a task runs in: the requested workspace
// if it is authorized for the agent, else the agent's default.
func (a AgentConfig) resolveWorkdir(requested string) (string, error) {
	if requested == "" {
		return a.Workdir, nil
	}
	for _, w := range a.Workspaces {
		if w.Path == requested {
			return w.Path, nil
		}
	}
	return "", fmt.Errorf("工作区 %s 未在本机授权给智能体 %s", requested, a.Key)
}

type Config struct {
	HubURL string `json:"hub_url"`
	Token  string `json:"token"`
	Name   string `json:"name,omitempty"`
	// AutoUpdate defaults to true; FORCED releases install even when false.
	AutoUpdate *bool         `json:"auto_update,omitempty"`
	Agents     []AgentConfig `json:"agents"`
	// Workhost turns this connector into a work server for the hub's 「工作」menu (see workhost.go)
	Workhost *WorkhostConfig `json:"workhost,omitempty"`

	path string
}

func defaultConfigDir() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		home, _ := os.UserHomeDir()
		dir = filepath.Join(home, ".config")
	}
	return filepath.Join(dir, "agent-hub")
}

func defaultConfigPath() string {
	return filepath.Join(defaultConfigDir(), "connector.json")
}

func loadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("配置文件不存在：%s，请先执行 init", path)
		}
		return nil, err
	}
	cfg := &Config{path: path}
	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("解析配置失败 %s: %w", path, err)
	}
	return cfg, cfg.validate()
}

func (c *Config) validate() error {
	if c.HubURL == "" || c.Token == "" {
		return errors.New("配置缺少 hub_url 或 token")
	}
	seen := map[string]bool{}
	for i := range c.Agents {
		a := &c.Agents[i]
		if a.Key == "" || strings.Contains(a.Key, "/") {
			return fmt.Errorf("智能体 key 不能为空且不能包含 /：%q", a.Key)
		}
		if seen[a.Key] {
			return fmt.Errorf("智能体 key 重复：%s", a.Key)
		}
		seen[a.Key] = true
		if a.Kind == "" {
			a.Kind = "command"
		}
		if a.Command == "" {
			if a.Kind == "command" {
				return fmt.Errorf("智能体 %s 为 command 类型时必须配置 command", a.Key)
			}
			a.Command = a.Kind
		}
		if a.Name == "" {
			a.Name = a.Key
		}
	}
	return nil
}

func (c *Config) save() error {
	if c.path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(c.path), 0o700); err != nil {
		return err
	}
	data, _ := json.MarshalIndent(c, "", "  ")
	return os.WriteFile(c.path, data, 0o600)
}

var knownAgents = []AgentConfig{
	{Key: "claude", Name: "Claude Code", Kind: "claude", Description: "Anthropic Claude Code，可读写代码、执行命令"},
	{Key: "codex", Name: "Codex", Kind: "codex", Description: "OpenAI Codex CLI，可读写代码、执行命令"},
	{Key: "hermes", Name: "Hermes", Kind: "hermes", Description: "Hermes Agent，可执行运维与通用任务"},
}

// detectAgents returns the well-known agent CLIs found on PATH.
func detectAgents(workdir string) []AgentConfig {
	var found []AgentConfig
	for _, a := range knownAgents {
		if _, err := exec.LookPath(a.Kind); err == nil {
			a.Command = a.Kind
			a.Workdir = workdir
			found = append(found, a)
		}
	}
	return found
}

// isWorkAgent: the agent serves the 「工作」menu. With a workhost section but no agent marked, all of them do
// (configs written before agents could be picked one by one).
func (c *Config) isWorkAgent(a AgentConfig) bool {
	if c.Workhost == nil {
		return false
	}
	for _, x := range c.Agents {
		if x.Workhost {
			return a.Workhost
		}
	}
	return true
}

func (c *Config) autoUpdate() bool { return c.AutoUpdate == nil || *c.AutoUpdate }
