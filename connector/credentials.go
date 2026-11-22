package main

// Agent logins lent to the work containers. A login is a file under the account folder, or, for Claude Code on
// macOS, the keychain item the CLI keeps it in. The CLI in a container may refresh the token while it runs; the
// refreshed login is written back so the owner's own CLI on this machine keeps working.

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"
)

// claudeKeychain is the keychain item Claude Code keeps its login in on macOS.
const claudeKeychain = "Claude Code-credentials"

func keychainExists(service string) bool {
	return runtime.GOOS == "darwin" && exec.Command("security", "find-generic-password", "-s", service).Run() == nil
}

func (a WorkAccount) readCredentials() ([]byte, error) {
	if a.Keychain != "" {
		out, err := exec.Command("security", "find-generic-password", "-s", a.Keychain, "-w").Output()
		if err != nil {
			return nil, fmt.Errorf("读取钥匙串 %s 失败（需在弹窗里允许连接器访问）：%v", a.Keychain, err)
		}
		return bytes.TrimSpace(out), nil
	}
	file, ok := credentialFiles[a.Kind]
	if !ok {
		return nil, fmt.Errorf("不支持的账号类型 %s", a.Kind)
	}
	return os.ReadFile(filepath.Join(a.Home, file.name))
}

var keychainAccount = regexp.MustCompile(`"acct"<blob>="([^"]*)"`)

func (a WorkAccount) writeCredentials(data []byte) error {
	if a.Keychain != "" {
		attrs, err := exec.Command("security", "find-generic-password", "-s", a.Keychain).Output()
		if err != nil {
			return err
		}
		m := keychainAccount.FindSubmatch(attrs)
		if m == nil {
			return fmt.Errorf("钥匙串 %s 没有账户名", a.Keychain)
		}
		// through stdin and hex-encoded, so the login never shows up in a process list
		cmd := exec.Command("security", "-i")
		cmd.Stdin = strings.NewReader(fmt.Sprintf("add-generic-password -U -a %q -s %q -X %s\n",
			string(m[1]), a.Keychain, hex.EncodeToString(data)))
		if out, err := cmd.CombinedOutput(); err != nil || len(bytes.TrimSpace(out)) > 0 {
			return fmt.Errorf("写回钥匙串失败：%v %s", err, redactText(string(out)))
		}
		return nil
	}
	file := credentialFiles[a.Kind]
	path := filepath.Join(a.Home, file.name)
	tmp := path + ".ahw-tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// saveRefreshed writes back the login the container's CLI refreshed during the turn. It writes only while the
// source still holds what was lent, so a refresh by the owner's own CLI in the meantime is never overwritten.
func (h *workhost) saveRefreshed(ctx context.Context, w *WorkContext, acct WorkAccount, lent []byte) {
	file := credentialFiles[acct.Kind]
	out, err := h.inContainer(ctx, w, nil, nil, "cat", workHome+"/"+file.dir+"/"+file.name)
	now := bytes.TrimSpace([]byte(out))
	if err != nil || len(now) == 0 || !json.Valid(now) || bytes.Equal(now, bytes.TrimSpace(lent)) {
		return
	}
	current, err := acct.readCredentials()
	if err != nil || !bytes.Equal(bytes.TrimSpace(current), bytes.TrimSpace(lent)) {
		return
	}
	if err := acct.writeCredentials(now); err != nil {
		log.Printf("账号 %s 刷新后的登录写回失败：%v", acct.ID, err)
	}
}

// readClaudeQuota reads the plan and the 5-hour / 7-day usage of a Claude login; no model call is made.
func readClaudeQuota(ctx context.Context, a WorkAccount) accountInfo {
	info := accountInfo{ID: a.ID, Kind: a.Kind}
	data, err := a.readCredentials()
	if err != nil {
		info.Error = err.Error()
		return info
	}
	var doc struct {
		OAuth struct {
			AccessToken      string `json:"accessToken"`
			ExpiresAt        int64  `json:"expiresAt"`
			SubscriptionType string `json:"subscriptionType"`
		} `json:"claudeAiOauth"`
	}
	if json.Unmarshal(data, &doc) != nil || doc.OAuth.AccessToken == "" {
		info.Error = "登录凭据里没有 claudeAiOauth"
		return info
	}
	info.Plan = doc.OAuth.SubscriptionType
	if doc.OAuth.ExpiresAt > 0 && time.UnixMilli(doc.OAuth.ExpiresAt).Before(time.Now()) {
		info.Usable = true // the CLI refreshes it on its next run
		info.Error = "登录令牌已过期，下次使用时会自动刷新，届时再显示额度"
		return info
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", "https://api.anthropic.com/api/oauth/usage", nil)
	req.Header.Set("Authorization", "Bearer "+doc.OAuth.AccessToken)
	req.Header.Set("anthropic-beta", "oauth-2025-04-20")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		info.Error = "读取额度失败：" + redactText(err.Error())
		return info
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		info.Error = fmt.Sprintf("读取额度失败：HTTP %d", resp.StatusCode)
		return info
	}
	var usage map[string]json.RawMessage
	if err := json.NewDecoder(resp.Body).Decode(&usage); err != nil {
		info.Error = "额度返回无法解析"
		return info
	}
	window := func(key string, mins int) *quotaWindow {
		var u struct {
			Utilization *float64 `json:"utilization"`
			ResetsAt    string   `json:"resets_at"`
		}
		if json.Unmarshal(usage[key], &u) != nil || u.Utilization == nil {
			return nil
		}
		q := &quotaWindow{UsedPercent: *u.Utilization, WindowMins: mins}
		if t, err := time.Parse(time.RFC3339, u.ResetsAt); err == nil {
			q.ResetsAt = t.Unix()
		}
		return q
	}
	info.Primary, info.Secondary = window("five_hour", 300), window("seven_day", 7*24*60)
	info.Usable = true
	for _, q := range []*quotaWindow{info.Primary, info.Secondary} {
		if q != nil && q.UsedPercent >= 100 {
			info.Usable = false
		}
	}
	return info
}
