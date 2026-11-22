package main

// Maven for the colleagues' containers: the server owner may lend a settings.xml (private repository mirrors and
// credentials) and their local repository, so builds don't download everything again. Only the owner sets this, on
// their own machine (`workhost maven`); a path chosen from the web could otherwise mount any folder of this machine.
//
// The local repository is read-only by default: Maven reads it as a "tail" behind each colleague's own repository
// on their volume (maven.repo.local.tail, Maven 3.9+), so nothing a colleague's build does lands in the owner's
// folder. With Shared it is mounted writable as the colleagues' local repository itself.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type MavenShare struct {
	Settings   string `json:"settings,omitempty"`   // settings.xml on this machine
	Repository string `json:"repository,omitempty"` // local repository folder on this machine
	Shared     bool   `json:"shared,omitempty"`     // colleagues write into it too
}

const (
	containerMavenSettings = workHome + "/.m2/settings.xml"
	containerMavenRepo     = workHome + "/.m2/repository"
	containerMavenTail     = "/opt/host-m2"
)

func expandHome(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		home, _ := os.UserHomeDir()
		return filepath.Join(home, strings.TrimPrefix(p, "~"))
	}
	return p
}

func newMavenShare(settings, repo string, shared bool) (*MavenShare, error) {
	if settings == "" && repo == "" {
		return nil, fmt.Errorf("用法：workhost maven --settings <settings.xml> --repo <本地仓库目录> [--shared]，或 workhost maven off")
	}
	m := &MavenShare{Shared: shared}
	if settings != "" {
		p, err := filepath.Abs(expandHome(settings))
		if err != nil {
			return nil, err
		}
		if st, err := os.Stat(p); err != nil || !st.Mode().IsRegular() {
			return nil, fmt.Errorf("settings 文件不存在：%s", p)
		}
		m.Settings = p
	}
	if repo != "" {
		p, err := filepath.Abs(expandHome(repo))
		if err != nil {
			return nil, err
		}
		if st, err := os.Stat(p); err != nil || !st.IsDir() {
			return nil, fmt.Errorf("本地仓库目录不存在：%s", p)
		}
		m.Repository = p
	}
	if shared && m.Repository == "" {
		return nil, fmt.Errorf("--shared 需要同时指定 --repo")
	}
	return m, nil
}

// dockerArgs: the mounts and environment a colleague's container gets for this share.
func (m *MavenShare) dockerArgs() []string {
	if m == nil {
		return nil
	}
	var args []string
	if m.Settings != "" {
		args = append(args, "-v", m.Settings+":"+containerMavenSettings+":ro")
	}
	// the owner's settings.xml usually names a <localRepository> path of their machine: inside the container the
	// repository is always the one on the colleague's volume (or the shared one mounted there)
	mavenArgs := "-Dmaven.repo.local=" + containerMavenRepo
	switch {
	case m.Repository != "" && m.Shared:
		// several colleagues' builds write into it at once: Maven 3.9's file locks keep them from clashing
		args = append(args, "-v", m.Repository+":"+containerMavenRepo)
		mavenArgs += " -Daether.syncContext.named.factory=file-lock -Daether.syncContext.named.nameMapper=file-gav"
	case m.Repository != "":
		args = append(args, "-v", m.Repository+":"+containerMavenTail+":ro")
		mavenArgs += " -Dmaven.repo.local.tail=" + containerMavenTail + " -Dmaven.repo.local.tail.ignoreAvailability=true"
	}
	return append(args, "-e", "MAVEN_ARGS="+mavenArgs)
}

func (m *MavenShare) spec() string {
	if m == nil {
		return ""
	}
	return fmt.Sprintf("%s|%s|%t", m.Settings, m.Repository, m.Shared)
}

// info is what the hub shows on 「服务器维护」: whether each is lent, not the paths.
func (m *MavenShare) info() map[string]any {
	if m == nil {
		return nil
	}
	repo := ""
	if m.Repository != "" {
		repo = map[bool]string{true: "shared", false: "readonly"}[m.Shared]
	}
	return map[string]any{"settings": m.Settings != "", "repository": repo}
}
