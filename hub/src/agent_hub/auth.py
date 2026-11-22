"""Passwords, web sessions (JWT) and long-lived API tokens for MCP clients and connectors."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import jwt

from .db import Database

SESSION_TTL_SECONDS = 7 * 24 * 3600
TOKEN_PREFIX = {"personal": "ahp_", "connector": "ahc_", "task": "aht_"}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def issue_session(secret: str, user_id: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + SESSION_TTL_SECONDS}, secret, "HS256")


def decode_session(secret: str, token: str) -> int | None:
    try:
        return int(jwt.decode(token, secret, algorithms=["HS256"])["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(db: Database, user_id: int, kind: str, name: str) -> tuple[int, str]:
    token = TOKEN_PREFIX[kind] + secrets.token_urlsafe(32)
    token_id = db.execute(
        "INSERT INTO api_tokens (user_id, kind, name, token_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, kind, name, _hash_token(token), token[:10], time.time()),
    )
    return token_id, token


def resolve_api_token(db: Database, token: str, kind: str | tuple | None = None) -> dict | None:
    row = db.one(
        "SELECT t.*, u.username FROM api_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ? AND t.revoked = 0 AND u.disabled = 0",
        (_hash_token(token),),
    )
    kinds = (kind,) if isinstance(kind, str) else kind
    if not row or (kinds and row["kind"] not in kinds):
        return None
    db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (time.time(), row["id"]))
    return row


def bearer(headers) -> str:
    value = headers.get("authorization") or ""
    return value[7:].strip() if value.lower().startswith("bearer ") else ""
