package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"sync"
)

// outbox keeps task results until the hub acknowledges them, on disk so a
// connector restart or a lost connection never loses a finished turn. The hub
// applies each msg_id once, so resending after a lost ack is harmless.
type outbox struct {
	mu    sync.Mutex
	path  string // empty: memory only (tests, or no config file)
	items []map[string]any
}

func newOutbox(path string) *outbox {
	o := &outbox{path: path}
	if path == "" {
		return o
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return o
	}
	if err := json.Unmarshal(data, &o.items); err != nil {
		log.Printf("待上报结果文件损坏，已忽略：%v", err)
		o.items = nil
	}
	if len(o.items) > 0 {
		log.Printf("有 %d 条未确认的任务结果，连上 Hub 后重新上报", len(o.items))
	}
	return o
}

func newMsgID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// add stores a report and gives it a msg_id.
func (o *outbox) add(msg map[string]any) map[string]any {
	msg["msg_id"] = newMsgID()
	o.mu.Lock()
	o.items = append(o.items, msg)
	o.saveLocked()
	o.mu.Unlock()
	return msg
}

func (o *outbox) ack(id string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	for i, m := range o.items {
		if m["msg_id"] == id {
			o.items = append(o.items[:i], o.items[i+1:]...)
			o.saveLocked()
			return
		}
	}
}

func (o *outbox) pending() []map[string]any {
	o.mu.Lock()
	defer o.mu.Unlock()
	return append([]map[string]any(nil), o.items...)
}

// taskIDs lists tasks with a finished turn still waiting for the hub.
func (o *outbox) taskIDs() []any {
	o.mu.Lock()
	defer o.mu.Unlock()
	ids := []any{}
	for _, m := range o.items {
		if m["type"] == "task_done" {
			ids = append(ids, m["task_id"])
		}
	}
	return ids
}

func (o *outbox) saveLocked() {
	if o.path == "" {
		return
	}
	data, err := json.Marshal(o.items)
	if err == nil {
		if err = os.MkdirAll(filepath.Dir(o.path), 0o700); err == nil {
			tmp := o.path + ".tmp"
			if err = os.WriteFile(tmp, data, 0o600); err == nil {
				err = os.Rename(tmp, o.path)
			}
		}
	}
	if err != nil {
		log.Printf("保存待上报结果失败：%v", err)
	}
}
