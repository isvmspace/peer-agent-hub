"""Masking for execution traces shown to people other than the agent's owner.

Connectors 0.8.4+ already redact before reporting; the hub applies the same rules again so traces
from older connectors are safe too, and additionally hides local paths, which the connector keeps
for the owner. Keep the credential rules in step with connector/redact.go.
"""

from __future__ import annotations

import re

HIDDEN = "（涉及凭据或密钥，内容已隐藏）"

# commands and paths that read or write secrets: the whole line is hidden. Only file and command
# shapes, not words like "password" or "secret", so tests and docs about them stay visible.
_SENSITIVE = re.compile(
    r"(?i)(\.secrets?\b|(^|[\\/\s\"'=<])\.env(\.[\w-]+)?\b|\.ssh[\\/]|\bid_(rsa|ed25519|ecdsa|dsa)\b|\.pem\b|\.p12\b|\.pfx\b|\.key\b"
    r"|\.netrc|\.git-credentials|\.pgpass|\.npmrc|\.pypirc|\.docker[\\/]config\.json|\.kube[\\/]config|\.aws[\\/]"
    r"|(^|[\\/\s\"'=<])credentials(\.json|\.toml|\.ya?ml)?($|[\s\"';&|)<>])|gh[\\/]hosts\.ya?ml|\.config[\\/]gcloud[\\/]|\.azure[\\/]|\.vault-token|\.s3cfg|keychain|security\s+(find|dump)-|login\s+-p\b"
    r"|private[_ -]?key|\bprintenv\b|(^|[;&|]\s*)env\s*($|[;&|]))"
)

_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)")
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

# key = value / key: value / "key": "value"; whether the key names a credential is decided in code
_ASSIGN = re.compile(
    r"([A-Za-z0-9_.-]{0,63}[A-Za-z0-9])([\"']?\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.)*(?:\"|$)|'[^']*(?:'|$)|(?:(?i:basic|bearer|token|digest)\s+)?(?:\\.|[^\s\"'&,;}\\])+)"
)
_SECRET_KEYS = ("password", "passwd", "pwd", "passphrase", "secret", "token", "apikey", "api_key", "api-key",
                "accesskey", "access_key", "access-key", "privatekey", "private_key", "auth", "authorization",
                "credential", "credentials", "cookie", "session_key", "signature")
# "page_token=…", "max_tokens" style counters and cursors are not credentials
_BENIGN_TOKEN_PREFIX = ("page", "pagination", "next", "next_page", "prev", "cursor", "continuation", "sync",
                        "max", "num", "total", "input", "output", "cache", "prompt", "completion")

_Q = r"(\"[^\"]*\"?|'[^']*'?|[^\s'\"]+)"  # a CLI value: quoted (maybe cut off) or bare
_OTHER = [
    (re.compile(r"(?i)\b((?:set-)?cookie[\"']?\s*[:=]\s*)[^\n]+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{3,}"), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "***"),
    (re.compile(r"\b(sk|pk|rk|ghp|gho|ghs|ghu|ghr|glpat|ahp|ahc|xox[abprs]|hf|npm)[-_][A-Za-z0-9_-]{12,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "***"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{12,}\b"), "***"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "***"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1***\2"),
    (re.compile(r"((?:\s-u|--user)(?:=|\s*)\"[^:\"]+:)[^\"]*"), r"\1***"),  # curl -u "user:pass with spaces"
    (re.compile(r"((?:\s-u|--user)(?:=|\s*)'[^:']+:)[^']*"), r"\1***"),
    (re.compile(r"((?:\s-u|--user)(?:=|\s*)[^\s:'\"]+:)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)(--(?:password|passwd|pass|token|secret|api-key|auth|access-token)(?:=|\s+))" + _Q), r"\1***"),
    (re.compile(r"((?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n]*?\s-p)" + _Q), r"\1***"),  # -p only attached
    (re.compile(r"((?:sshpass|docker\s+login|podman\s+login|helm\s+registry\s+login|mongo\w*)\b[^\n]*?\s-p\s*)" + _Q), r"\1***"),
    (re.compile(r"(redis-cli\b[^\n]*?\s-a\s*)" + _Q), r"\1***"),
]


def _assign(m: re.Match, nested: bool = True) -> str:
    key, sep, value = m.group(1), m.group(2), m.group(3)
    k = key.lower()
    hit = next((s for s in _SECRET_KEYS if k == s or k.endswith(s) and not k[-len(s) - 1].isalnum()), None)
    if hit is None and not k.endswith(("password", "passwd", "secret", "token")):
        # "INFO: password=x": the outer value may hold a credential assignment of its own
        return key + sep + (_ASSIGN.sub(lambda n: _assign(n, False), value) if nested else value)
    bare = value.strip("\"'")
    if k.endswith("token") or hit == "token":
        prefix = k[: -len("token")].rstrip("_-.")
        if prefix in _BENIGN_TOKEN_PREFIX or (k in ("token", "tokens") and bare.isdigit()):
            return m.group(0)
    if not bare:
        return m.group(0)
    if value[0] in "\"'":
        return f"{key}{sep}{value[0]}***{value[0] if value[-1] == value[0] and len(value) > 1 else ''}"
    return f"{key}{sep}***"


def mask_credentials(text: str) -> str:
    text = _PEM.sub("***", text)
    text = _ASSIGN.sub(_assign, text)
    for pattern, repl in _OTHER:
        text = pattern.sub(repl, text)
    return text


# absolute local paths: shown as …/<last part>, so the owner's folder layout stays private
_ROOT = r"(?:~|/(?!/)|[A-Za-z]:[\\/]|\\\\)"
_PATH = re.compile(
    r"(?<=[\"'])" + _ROOT + r"[^\"'\n]*[\\/][^\"'\n]*(?=[\"'])"  # quoted: may contain spaces
    r"|(?<![\w.:/\\-])" + _ROOT + r"[^\s\"'<>|;,)`\]]*[\\/][^\s\"'<>|;,)`\]]*"
    r"|/(?:Users|home|root|private|var|tmp|opt|data|mnt|srv|Volumes|etc|usr)/[^\s\"'<>|;,)`\]]*"  # glued to "-I", "Error:", "file://"
)


def _short_path(m: re.Match) -> str:
    start = max(m.string.rfind(c, 0, m.start()) for c in " \t\n\"'(<[") + 1
    token = m.string[start:m.start()]
    if "://" in token and not token.lower().startswith("file://"):
        return m.group(0)  # part of a web address, not a local path
    path = m.group(0).rstrip("/\\")
    return "…/" + re.split(r"[\\/]", path)[-1]


def mask(text: str) -> str:
    """Mask credential values and local paths but keep the text readable."""
    return _PATH.sub(_short_path, mask_credentials(text or ""))


def sensitive(text: str) -> bool:
    return bool(text) and bool(_SENSITIVE.search(text))


def tool_text(text: str) -> str:
    """A tool's command or output: hidden entirely when it targets secrets, masked otherwise."""
    return HIDDEN if sensitive(text or "") else mask(text or "")


def pem_open(text: str) -> bool:
    """Whether a private key begins in this text and does not end in it, so following chunks are key body."""
    begins = list(_PEM_BEGIN.finditer(text or ""))
    return bool(begins) and not _PEM_END.search(text, begins[-1].end())


def event(row: dict, in_key: bool = False) -> dict:
    """A process event as non-owners see it. in_key: an earlier chunk opened a private key."""
    content = row["content"] or ""
    if row["kind"] == "tool":
        payload = dict(row.get("payload") or {})
        if "summary" in payload:
            payload["summary"] = tool_text(payload["summary"])
        return {**row, "content": tool_text(content), "payload": payload}
    if in_key:
        end = _PEM_END.search(content)
        return {**row, "content": "***" + (mask(content[end.end():]) if end else "")}
    return {**row, "content": mask(content)}


def calls_hidden(rows: list[dict]) -> set:
    """Call ids whose start targeted secrets, so their finish is hidden too even if its output looks harmless."""
    return {
        (r["turn"], (r.get("payload") or {}).get("call_id"))
        for r in rows
        if r["kind"] == "tool" and (r.get("payload") or {}).get("phase") == "started"
        and ((r.get("payload") or {}).get("sensitive") or sensitive((r.get("payload") or {}).get("summary", ""))
             or sensitive(r.get("content") or ""))
    }
