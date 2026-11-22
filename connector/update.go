package main

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Self-update follows the ai-client desktop flow: ask the hub for the latest
// release, take a short-lived download ticket, download to a .part file with a
// size cap, verify SHA-256, then swap the binary and restart.

const updateCheckInterval = 6 * time.Hour

type UpdateInfo struct {
	UpdateAvailable bool   `json:"update_available"`
	ReleaseID       int64  `json:"release_id"`
	Platform        string `json:"platform"`
	LatestVersion   string `json:"latest_version"`
	FileSize        int64  `json:"file_size"`
	SHA256          string `json:"sha256"`
	Notes           string `json:"notes"`
	Policy          string `json:"policy"`
}

type downloadTicket struct {
	DownloadURL string `json:"download_url"`
	ExpiresAt   int64  `json:"expires_at"`
}

var versionRe = regexp.MustCompile(`^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$`)
var sha256Re = regexp.MustCompile(`^[0-9a-f]{64}$`)

func parseVersion(v string) ([3]int, bool) {
	var out [3]int
	m := versionRe.FindStringSubmatch(strings.TrimSpace(v))
	if m == nil {
		return out, false
	}
	for i := range out {
		out[i], _ = strconv.Atoi(m[i+1])
	}
	return out, true
}

func newerThan(a, b string) bool {
	va, ok1 := parseVersion(a)
	vb, ok2 := parseVersion(b)
	if !ok1 || !ok2 {
		return false
	}
	for i := range va {
		if va[i] != vb[i] {
			return va[i] > vb[i]
		}
	}
	return false
}

func platform() string { return runtime.GOOS + "/" + runtime.GOARCH }

func hubRequest(ctx context.Context, cfg *Config, method, path string, query url.Values, body any, out any) error {
	u := strings.TrimRight(cfg.HubURL, "/") + path
	if query != nil {
		u += "?" + query.Encode()
	}
	var reader io.Reader
	if body != nil {
		data, _ := json.Marshal(body)
		reader = strings.NewReader(string(data))
	}
	req, err := http.NewRequestWithContext(ctx, method, u, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s 请求失败: %w", method, path, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s %s 返回 %d: %s", method, path, resp.StatusCode, strings.TrimSpace(string(data)))
	}
	return json.Unmarshal(data, out)
}

func checkUpdate(ctx context.Context, cfg *Config) (*UpdateInfo, error) {
	if _, ok := parseVersion(version); !ok {
		return &UpdateInfo{}, nil // dev builds never self-update
	}
	var info UpdateInfo
	q := url.Values{"platform": {platform()}, "current": {version}}
	if err := hubRequest(ctx, cfg, http.MethodGet, "/api/connector/latest", q, nil, &info); err != nil {
		return nil, err
	}
	return &info, nil
}

func validUpdate(info *UpdateInfo) error {
	switch {
	case info == nil || !info.UpdateAvailable:
		return errors.New("没有可用更新")
	case info.Platform != platform():
		return fmt.Errorf("更新平台不匹配：%s", info.Platform)
	case !newerThan(info.LatestVersion, version):
		return fmt.Errorf("拒绝降级或同版本：%s", info.LatestVersion)
	case info.FileSize <= 0 || info.FileSize > 200<<20:
		return errors.New("更新包大小不合法")
	case !sha256Re.MatchString(strings.ToLower(info.SHA256)):
		return errors.New("更新包 SHA-256 不合法")
	}
	return nil
}

// downloadUpdate fetches and verifies the release next to the running binary
// and returns the path of the verified file.
func downloadUpdate(ctx context.Context, cfg *Config, info *UpdateInfo) (string, error) {
	if err := validUpdate(info); err != nil {
		return "", err
	}
	var ticket downloadTicket
	if err := hubRequest(ctx, cfg, http.MethodPost, "/api/connector/download-ticket", nil,
		map[string]any{"release_id": info.ReleaseID}, &ticket); err != nil {
		return "", err
	}
	parsed, err := url.Parse(ticket.DownloadURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return "", errors.New("下载地址不合法")
	}
	exe, err := executablePath()
	if err != nil {
		return "", err
	}
	finalPath := exe + ".new"
	partPath := finalPath + ".part"
	_ = os.Remove(partPath)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, ticket.DownloadURL, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", errors.New("下载更新包失败")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("下载更新包 HTTP %d", resp.StatusCode)
	}
	f, err := os.OpenFile(partPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return "", err
	}
	hash := sha256.New()
	n, copyErr := io.Copy(io.MultiWriter(f, hash), io.LimitReader(resp.Body, info.FileSize+1))
	closeErr := f.Close()
	fail := func(e error) (string, error) { _ = os.Remove(partPath); return "", e }
	if copyErr != nil || closeErr != nil {
		return fail(errors.New("写入更新包失败"))
	}
	if n != info.FileSize {
		return fail(fmt.Errorf("更新包大小不符：%d != %d", n, info.FileSize))
	}
	want, _ := hex.DecodeString(strings.ToLower(info.SHA256))
	if subtle.ConstantTimeCompare(hash.Sum(nil), want) != 1 {
		return fail(errors.New("更新包 SHA-256 校验失败"))
	}
	_ = os.Remove(finalPath)
	if err := os.Rename(partPath, finalPath); err != nil {
		return fail(err)
	}
	return finalPath, nil
}

func executablePath() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	return exe, nil
}

// installUpdate swaps in the verified binary. With restart it replaces the
// current process; otherwise the new version takes effect on next start.
func installUpdate(newPath string, restart bool) error {
	exe, err := executablePath()
	if err != nil {
		return err
	}
	if err := swapBinary(exe, newPath); err != nil {
		return fmt.Errorf("替换可执行文件失败：%w", err)
	}
	if restart {
		return restartSelf(exe)
	}
	return nil
}

func cmdUpdate(path string) error {
	cfg, err := loadConfig(path)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	info, err := checkUpdate(ctx, cfg)
	if err != nil {
		return err
	}
	if !info.UpdateAvailable {
		fmt.Printf("当前 %s 已是最新版本\n", version)
		return nil
	}
	fmt.Printf("发现新版本 %s（当前 %s），下载中…\n", info.LatestVersion, version)
	newPath, err := downloadUpdate(ctx, cfg, info)
	if err != nil {
		return err
	}
	if err := installUpdate(newPath, false); err != nil {
		return err
	}
	fmt.Printf("已更新到 %s。若连接器在后台运行，它会在空闲时自动升级；也可重新登录或执行 install 立即重启。\n", info.LatestVersion)
	return nil
}

// updateLoop checks periodically and when the hub announces a release, and
// installs once no task is queued, running or waiting to be reported.
func (c *Connector) updateLoop(ctx context.Context) {
	cleanupOldBinary()
	timer := time.NewTimer(time.Minute)
	defer timer.Stop()
	for {
		var info *UpdateInfo
		select {
		case <-ctx.Done():
			return
		case info = <-c.updates:
		case <-timer.C:
			timer.Reset(updateCheckInterval)
			checked, err := checkUpdate(ctx, c.cfg)
			if err != nil {
				log.Printf("检查更新失败：%v", err)
				continue
			}
			info = checked
		}
		if info == nil || !info.UpdateAvailable || validUpdate(info) != nil {
			continue
		}
		if !c.cfg.autoUpdate() && info.Policy != "FORCED" {
			log.Printf("有新版本 %s（auto_update 已关闭，执行 update 手动升级）", info.LatestVersion)
			continue
		}
		log.Printf("发现新版本 %s（%s），将在空闲时升级", info.LatestVersion, info.Policy)
		newPath, err := downloadUpdate(ctx, c.cfg, info)
		if err != nil {
			log.Printf("下载更新失败：%v", err)
			continue
		}
		for !c.idle() {
			select {
			case <-ctx.Done():
				return
			case <-time.After(10 * time.Second):
			}
		}
		log.Printf("升级到 %s 并重启", info.LatestVersion)
		if err := installUpdate(newPath, true); err != nil {
			log.Printf("升级失败：%v", err)
		}
	}
}

func (c *Connector) idle() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.tasks) == 0 // unacknowledged results live in the on-disk outbox and survive the restart
}
