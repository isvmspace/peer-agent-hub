package main

// Images a person attached to their message. They arrive inside the task, are written where the agent can read
// them for this turn only, and are handed to the CLI: Codex takes them as --image, Claude Code and the others are
// told the paths in the prompt and read the files themselves.

import (
	"context"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

type TaskImage struct {
	Name string `json:"name"`
	Data string `json:"data"` // base64
}

var unsafeFileChars = regexp.MustCompile(`[^A-Za-z0-9._\-\p{Han}]+`)

// imageFileName keeps a readable, safe name, numbered so two images with the same name do not collide.
func imageFileName(i int, name string) string {
	base := unsafeFileChars.ReplaceAllString(filepath.Base(name), "_")
	if base == "" || base == "." || base == ".." {
		base = "image"
	}
	return fmt.Sprintf("%d-%s", i+1, base)
}

func decodeImage(img TaskImage) ([]byte, error) {
	data, err := base64.StdEncoding.DecodeString(img.Data)
	if err != nil || len(data) == 0 {
		return nil, fmt.Errorf("图片 %s 无法读取", img.Name)
	}
	return data, nil
}

// writeLocalImages puts the images in a temporary folder on this machine; the returned function removes it.
func writeLocalImages(t *Task) (func(), error) {
	if len(t.Images) == 0 {
		return func() {}, nil
	}
	dir, err := os.MkdirTemp("", "agent-hub-images-")
	if err != nil {
		return func() {}, err
	}
	cleanup := func() { _ = os.RemoveAll(dir) }
	for i, img := range t.Images {
		data, err := decodeImage(img)
		if err != nil {
			cleanup()
			return func() {}, err
		}
		path := filepath.Join(dir, imageFileName(i, img.Name))
		if err := os.WriteFile(path, data, 0o600); err != nil {
			cleanup()
			return func() {}, err
		}
		t.ImagePaths = append(t.ImagePaths, path)
	}
	return cleanup, nil
}

// writeContainerImages puts the images into the colleague's container, next to the session's repositories.
func (h *workhost) writeContainerImages(ctx context.Context, t *Task) (func(), error) {
	if len(t.Images) == 0 {
		return func() {}, nil
	}
	w := t.Work
	dir := fmt.Sprintf("%s/.ahw-images/turn-%d", w.sessionDir(), t.Turn)
	cleanup := func() {
		_, _ = h.inContainer(context.Background(), w, nil, nil, "rm", "-rf", dir)
	}
	for i, img := range t.Images {
		data, err := decodeImage(img)
		if err != nil {
			return cleanup, err
		}
		path := dir + "/" + imageFileName(i, img.Name)
		if _, err := h.inContainer(ctx, w, nil, data, "sh", "-c", `mkdir -p "$(dirname "$0")" && cat > "$0"`, path); err != nil {
			return cleanup, fmt.Errorf("把图片放进工作容器失败：%v", err)
		}
		t.ImagePaths = append(t.ImagePaths, path)
	}
	return cleanup, nil
}

// imageNote tells the agent where this message's images are.
func imageNote(paths []string) string {
	if len(paths) == 0 {
		return ""
	}
	return "\n\n[同事随消息发来 " + fmt.Sprint(len(paths)) + " 张图片，请先查看再回答：\n" + strings.Join(paths, "\n") + "\n]"
}

// codexImageArgs attaches the images with the --image=<file> form, ahead of the other flags, so the option's
// multiple values never swallow the session id or the "-" that follow.
func codexImageArgs(t Task) []string {
	var args []string
	for _, p := range t.ImagePaths {
		args = append(args, "--image="+p)
	}
	return args
}
