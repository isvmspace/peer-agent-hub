"""GitLab REST API for the 「工作」menu: project search, merge requests with their pipeline, OAuth.

Only the standard library: requests run in a worker thread so the event loop never waits on GitLab.
A token is either a personal access token (sent as PRIVATE-TOKEN) or an OAuth access token (Bearer).
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .hub import HubError

TIMEOUT = 20


def base_url(repo_url: str = "") -> str:
    """The GitLab to talk to: AGENT_HUB_GITLAB_URL, else the scheme and host of a repository URL."""
    configured = os.getenv("AGENT_HUB_GITLAB_URL", "").rstrip("/")
    if configured:
        return configured
    parts = urllib.parse.urlsplit(repo_url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"
    raise HubError("没有配置 GitLab 地址（AGENT_HUB_GITLAB_URL），也无法从仓库地址推出", 409)


def project_path(repo_url: str) -> str:
    """`group/sub/project` of an http(s) repository URL."""
    parts = urllib.parse.urlsplit(repo_url)
    if parts.scheme not in ("http", "https"):
        raise HubError("只有 GitLab 上的 http(s) 仓库能建合并请求", 400)
    path = parts.path.strip("/")
    configured = urllib.parse.urlsplit(os.getenv("AGENT_HUB_GITLAB_URL", "")).path.strip("/")
    if configured and path.startswith(configured + "/"):
        path = path[len(configured) + 1:]  # GitLab served under a sub path
    return path[:-4] if path.endswith(".git") else path


def _request(base: str, token: dict, method: str, path: str, data: dict | None = None,
             query: dict | None = None) -> tuple[int, object]:
    url = f"{base}/api/v4{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json"}
    if token.get("kind") == "oauth":
        headers["Authorization"] = f"Bearer {token['value']}"
    elif token.get("value"):
        headers["PRIVATE-TOKEN"] = token["value"]
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # intranet GitLab: never via a proxy
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, {"message": raw.decode(errors="replace")[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HubError(f"连不上 GitLab：{exc}", 502) from None


async def request(base: str, token: dict, method: str, path: str, data: dict | None = None,
                  query: dict | None = None) -> tuple[int, object]:
    return await asyncio.to_thread(_request, base, token, method, path, data, query)


def _message(body: object) -> str:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error_description") or body.get("error") or ""
        if isinstance(msg, list):
            msg = "；".join(map(str, msg))
        if isinstance(msg, dict):
            msg = "；".join(f"{k} {v}" for k, v in msg.items())
        return str(msg)[:300]
    return ""


def explain(status: int, body: object, action: str) -> HubError:
    if status == 401:
        return HubError(f"{action}失败：GitLab 令牌无效或已过期，请在「工作」里重新连接", 409)
    if status == 403:
        return HubError(f"{action}失败：令牌权限不够（建合并请求需要 api 权限）", 403)
    if status == 404:
        return HubError(f"{action}失败：GitLab 上找不到这个项目，或你没有权限", 404)
    return HubError(f"{action}失败：{_message(body) or f'GitLab 返回 {status}'}", 502)


async def projects(base: str, token: dict, search: str) -> list[dict]:
    status, body = await request(base, token, "GET", "/projects", query={
        "membership": "true", "simple": "true", "per_page": 30, "order_by": "last_activity_at", "search": search,
    })
    if status != 200 or not isinstance(body, list):
        raise explain(status, body, "查询 GitLab 项目")
    return [{"name": p.get("path") or p.get("name"), "path": p.get("path_with_namespace"),
             "url": p.get("http_url_to_repo"), "default_branch": p.get("default_branch") or "main",
             "description": (p.get("description") or "")[:200]} for p in body]


def _mr(body: dict) -> dict:
    pipeline = body.get("head_pipeline") or body.get("pipeline") or {}
    return {"iid": body.get("iid"), "url": body.get("web_url"), "state": body.get("state"),
            "merge_status": body.get("detailed_merge_status") or body.get("merge_status"),
            "pipeline": pipeline.get("status") if isinstance(pipeline, dict) else None,
            "pipeline_url": pipeline.get("web_url") if isinstance(pipeline, dict) else None}


async def create_mr(base: str, token: dict, project: str, source: str, target: str, title: str,
                    description: str) -> dict:
    pid = urllib.parse.quote(project, safe="")
    status, body = await request(base, token, "POST", f"/projects/{pid}/merge_requests", data={
        "source_branch": source, "target_branch": target, "title": title[:255], "description": description[:5000],
        "remove_source_branch": True,
    })
    if status == 409:  # one is already open for this branch: use it
        status, body = await request(base, token, "GET", f"/projects/{pid}/merge_requests",
                                     query={"source_branch": source, "state": "opened"})
        if status == 200 and isinstance(body, list) and body:
            return _mr(body[0])
    if status not in (200, 201) or not isinstance(body, dict):
        raise explain(status, body, "创建合并请求")
    return _mr(body)


async def mr_status(base: str, token: dict, project: str, iid: int) -> dict:
    pid = urllib.parse.quote(project, safe="")
    status, body = await request(base, token, "GET", f"/projects/{pid}/merge_requests/{int(iid)}")
    if status != 200 or not isinstance(body, dict):
        raise explain(status, body, "查询合并请求")
    return _mr(body)


# ---- OAuth (optional: AGENT_HUB_GITLAB_OAUTH_ID / _SECRET) --------------------

def oauth_config() -> dict | None:
    client_id, secret = os.getenv("AGENT_HUB_GITLAB_OAUTH_ID", ""), os.getenv("AGENT_HUB_GITLAB_OAUTH_SECRET", "")
    if not client_id or not secret or not os.getenv("AGENT_HUB_GITLAB_URL"):
        return None
    public = os.getenv("AGENT_HUB_PUBLIC_URL", "").rstrip("/")
    return {"id": client_id, "secret": secret, "base": base_url(),
            "redirect": f"{public}/api/work/gitlab/callback"}


def authorize_url(cfg: dict, state: str) -> str:
    return f"{cfg['base']}/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": cfg["id"], "redirect_uri": cfg["redirect"], "response_type": "code", "state": state,
        "scope": "api read_user",
    })


def _token_call(cfg: dict, form: dict) -> dict:
    data = urllib.parse.urlencode({"client_id": cfg["id"], "client_secret": cfg["secret"], **form}).encode()
    req = urllib.request.Request(f"{cfg['base']}/oauth/token", data=data, method="POST",
                                 headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise HubError(f"GitLab 授权失败：{_message(json.loads(exc.read() or b'{}'))}", 409) from None
    except (urllib.error.URLError, OSError) as exc:
        raise HubError(f"连不上 GitLab：{exc}", 502) from None


async def exchange_code(cfg: dict, code: str) -> dict:
    return await asyncio.to_thread(_token_call, cfg, {"grant_type": "authorization_code", "code": code,
                                                      "redirect_uri": cfg["redirect"]})


async def refresh_token(cfg: dict, refresh: str) -> dict:
    return await asyncio.to_thread(_token_call, cfg, {"grant_type": "refresh_token", "refresh_token": refresh,
                                                      "redirect_uri": cfg["redirect"]})


async def current_user(base: str, token: dict) -> dict:
    status, body = await request(base, token, "GET", "/user")
    if status != 200 or not isinstance(body, dict):
        raise explain(status, body, "读取 GitLab 用户")
    return {"username": body.get("username"), "name": body.get("name"), "email": body.get("email") or ""}
