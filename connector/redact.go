package main

import (
	"regexp"
	"strings"
	"unicode"
)

// hiddenNotice replaces a whole tool summary that touches credentials: even a
// masked command line can reveal which secret file or account is involved.
const hiddenNotice = "（涉及凭据或密钥，内容已隐藏）"

// sensitiveTarget marks commands and paths that read or write secrets. Only file
// and command shapes, not words like "password", so tests and docs about them
// stay visible. Keep in step with hub/src/agent_hub/redact.py.
var sensitiveTarget = regexp.MustCompile(`(?i)(\.secrets?\b|(^|[\\/\s"'=<])\.env(\.[\w-]+)?\b|\.ssh[\\/]|\bid_(rsa|ed25519|ecdsa|dsa)\b|\.pem\b|\.p12\b|\.pfx\b|\.key\b` +
	`|\.netrc|\.git-credentials|\.pgpass|\.npmrc|\.pypirc|\.docker[\\/]config\.json|\.kube[\\/]config|\.aws[\\/]` +
	`|(^|[\\/\s"'=<])credentials(\.json|\.toml|\.ya?ml)?($|[\s"';&|)<>])|gh[\\/]hosts\.ya?ml|\.config[\\/]gcloud[\\/]|\.azure[\\/]|\.vault-token|\.s3cfg|keychain|security\s+(find|dump)-|login\s+-p\b` +
	`|private[_ -]?key|\bprintenv\b|(^|[;&|]\s*)env\s*($|[;&|]))`)

var (
	pemBlock = regexp.MustCompile(`-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)`)
	pemBegin = regexp.MustCompile(`-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----`)
	pemEnd   = regexp.MustCompile(`-----END [A-Z0-9 ]*PRIVATE KEY-----`)
	// key = value / key: value / "key": "value"; whether the key names a credential is decided in code
	assignment = regexp.MustCompile(`([A-Za-z0-9_.-]{0,63}[A-Za-z0-9])(["']?\s*[:=]\s*)("(?:[^"\\]|\\.)*(?:"|$)|'[^']*(?:'|$)|(?:(?i:basic|bearer|token|digest)\s+)?(?:\\.|[^\s"'&,;}\\])+)`)
)

var secretKeys = []string{"password", "passwd", "pwd", "passphrase", "secret", "token", "apikey", "api_key", "api-key",
	"accesskey", "access_key", "access-key", "privatekey", "private_key", "auth", "authorization",
	"credential", "credentials", "cookie", "session_key", "signature"}

// "page_token=…", "max_tokens" style counters and cursors are not credentials
var benignTokenPrefix = map[string]bool{"page": true, "pagination": true, "next": true, "next_page": true, "prev": true,
	"cursor": true, "continuation": true, "sync": true, "max": true, "num": true, "total": true, "input": true,
	"output": true, "cache": true, "prompt": true, "completion": true}

type maskRule struct {
	re   *regexp.Regexp
	repl string
}

// cliValue is an option's value: quoted (maybe cut off) or bare
const cliValue = `("[^"]*"?|'[^']*'?|[^\s'"]+)`

var otherSecrets = []maskRule{
	{regexp.MustCompile(`(?i)\b((?:set-)?cookie["']?\s*[:=]\s*)[^\n]+`), "${1}***"},
	{regexp.MustCompile(`(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{3,}`), "${1}***"},
	{regexp.MustCompile(`eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}`), "***"},
	{regexp.MustCompile(`\b(sk|pk|rk|ghp|gho|ghs|ghu|ghr|glpat|ahp|ahc|xox[abprs]|hf|npm)[-_][A-Za-z0-9_-]{12,}`), "***"},
	{regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}`), "***"},
	{regexp.MustCompile(`\b(AKIA|ASIA)[0-9A-Z]{12,}\b`), "***"},
	{regexp.MustCompile(`\bAIza[0-9A-Za-z_-]{20,}`), "***"},
	{regexp.MustCompile(`(://[^/\s:@]+:)[^@\s/]+(@)`), "${1}***${2}"},
	{regexp.MustCompile(`((?:\s-u|--user)(?:=|\s*)"[^:"]+:)[^"]*`), "${1}***"}, // curl -u "user:pass with spaces"
	{regexp.MustCompile(`((?:\s-u|--user)(?:=|\s*)'[^:']+:)[^']*`), "${1}***"},
	{regexp.MustCompile(`((?:\s-u|--user)(?:=|\s*)[^\s:'"]+:)[^\s'"]+`), "${1}***"},
	{regexp.MustCompile(`(?i)(--(?:password|passwd|pass|token|secret|api-key|auth|access-token)(?:=|\s+))` + cliValue), "${1}***"},
	{regexp.MustCompile(`((?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n]*?\s-p)` + cliValue), "${1}***"}, // -p only attached
	{regexp.MustCompile(`((?:sshpass|docker\s+login|podman\s+login|helm\s+registry\s+login|mongo\w*)\b[^\n]*?\s-p\s*)` + cliValue), "${1}***"},
	{regexp.MustCompile(`(redis-cli\b[^\n]*?\s-a\s*)` + cliValue), "${1}***"},
}

func secretAssignment(m string) string { return maskAssignment(m, true) }

func maskAssignment(m string, nested bool) string {
	g := assignment.FindStringSubmatch(m)
	key, sep, value := g[1], g[2], g[3]
	k := strings.ToLower(key)
	hit := ""
	for _, s := range secretKeys {
		if k == s || (strings.HasSuffix(k, s) && !isAlnum(rune(k[len(k)-len(s)-1]))) {
			hit = s
			break
		}
	}
	if hit == "" && !hasAnySuffix(k, "password", "passwd", "secret", "token") {
		// "INFO: password=x": the outer value may hold a credential assignment of its own
		if !nested {
			return m
		}
		return key + sep + assignment.ReplaceAllStringFunc(value, func(n string) string { return maskAssignment(n, false) })
	}
	bare := strings.Trim(value, `"'`)
	if strings.HasSuffix(k, "token") {
		prefix := strings.TrimRight(strings.TrimSuffix(k, "token"), "_-.")
		if benignTokenPrefix[prefix] || ((k == "token" || k == "tokens") && isDigits(bare)) {
			return m
		}
	}
	if bare == "" {
		return m
	}
	if value[0] == '"' || value[0] == '\'' {
		closing := ""
		if len(value) > 1 && value[len(value)-1] == value[0] {
			closing = value[:1]
		}
		return key + sep + value[:1] + "***" + closing
	}
	return key + sep + "***"
}

func isAlnum(r rune) bool { return unicode.IsLetter(r) || unicode.IsDigit(r) }

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func hasAnySuffix(s string, suffixes ...string) bool {
	for _, x := range suffixes {
		if strings.HasSuffix(s, x) {
			return true
		}
	}
	return false
}

// redactText masks credential values but keeps the rest readable. Local paths
// stay: the owner sees them, and the hub hides them from everyone else.
func redactText(s string) string {
	s = pemBlock.ReplaceAllString(s, "***")
	s = assignment.ReplaceAllStringFunc(s, secretAssignment)
	for _, r := range otherSecrets {
		s = r.re.ReplaceAllString(s, r.repl)
	}
	return s
}

// redactTool hides a tool summary entirely when the call targets secrets,
// and otherwise masks any credential values in it.
func redactTool(summary string) string {
	if sensitiveTarget.MatchString(summary) {
		return hiddenNotice
	}
	return redactText(summary)
}

// sensitiveCall reports whether a call's input (command, path…) targets secrets,
// so its output is hidden too even when the output itself looks harmless.
func sensitiveCall(input string) bool {
	return strings.TrimSpace(input) != "" && sensitiveTarget.MatchString(input)
}

// outputRedactor masks streamed output chunk by chunk, hiding a private key
// whose BEGIN, body and END arrive in separate chunks.
type outputRedactor struct{ inKey bool }

func (r *outputRedactor) mask(s string) string {
	if r.inKey {
		loc := pemEnd.FindStringIndex(s)
		if loc == nil {
			return "***"
		}
		r.inKey = false
		return "***" + r.mask(s[loc[1]:])
	}
	if begins := pemBegin.FindAllStringIndex(s, -1); len(begins) > 0 {
		last := begins[len(begins)-1]
		if !pemEnd.MatchString(s[last[1]:]) {
			r.inKey = true
			return redactText(s[:last[0]]) + "***"
		}
	}
	return redactText(s)
}
