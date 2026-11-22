"""Every audit action the hub writes needs a Chinese label in the console's audit page."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "hub/src/agent_hub"
VIEW = ROOT / "frontend/src/views/AuditView.vue"
CJK = re.compile(r"[一-鿿]")


def _names(node: ast.AST) -> set[str]:
    """Action names an argument can evaluate to; anything computed at runtime is rejected."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _names(node.body) | _names(node.orelse)
    raise ValueError(f"line {node.lineno}: audit action must be a string literal (or a conditional of literals), got {ast.dump(node)[:80]}")


def audit_actions(source: str) -> set[str]:
    actions = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "audit":
            if len(node.args) >= 2:
                actions |= _names(node.args[1])
            for kw in node.keywords:
                if kw.arg == "action":
                    actions |= _names(kw.value)
    return actions


def labels(view: str) -> dict[str, str]:
    block = re.search(r"const ACTIONS = \{(.*?)\n\}", view, re.S).group(1)
    return dict(re.findall(r"""['"]([a-z_]+\.[a-z_.]+)['"]\s*:\s*['"]([^'"]*)['"]""", block))


def test_every_audit_action_has_a_chinese_label():
    actions = set()
    for path in PACKAGE.rglob("*.py"):
        actions |= audit_actions(path.read_text())
    assert len(actions) > 20, "scan found too few audit calls; the scan is broken"
    shown = labels(VIEW.read_text())
    assert sorted(actions - shown.keys()) == [], "audit actions without a label"
    assert {k: v for k, v in shown.items() if not CJK.search(v)} == {}, "labels must be Chinese"


def test_guard_catches_mistakes():
    # each of these must be caught, otherwise the guard above proves nothing
    assert audit_actions("db.audit(int(user['id']), 'x.single_quoted')") == {"x.single_quoted"}
    assert audit_actions("self.db.audit(u, 'a.yes' if ok else 'a.no', k=1)") == {"a.yes", "a.no"}
    with pytest.raises(ValueError):
        audit_actions("db.audit(u, f'friend.{action}')")
    assert labels("const ACTIONS = {\n  'a.b': 'Login',\n}") == {"a.b": "Login"}
