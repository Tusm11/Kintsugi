"""Minimal GitHub REST client for the Action Layer (stdlib only, no extra dependency).

Configuration:
    GITHUB_TOKEN     token with `contents:write`, `pull_requests:write` and
                     `issues:write` on the target repos (fine-grained PAT or App token)
    GITHUB_API_URL   default https://api.github.com (set for GitHub Enterprise)

The token is sent only in the Authorization header to GITHUB_API_URL. It is
never put in URLs, logs, audit entries or error messages.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional


class GitHubError(RuntimeError):
    def __init__(self, status: Optional[int], message: str):
        super().__init__(f"GitHub API error{f' {status}' if status else ''}: {message}")
        self.status = status


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        api_url: Optional[str] = None,
        opener: Optional[Callable[..., Any]] = None,
        timeout: float = 30.0,
    ):
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN") or None
        self.api_url = (api_url or os.getenv("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.token:
            raise GitHubError(None, "GITHUB_TOKEN is not set")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "kintsugi",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            raise GitHubError(exc.code, f"{method} {path}: {detail or exc.reason}") from None
        except urllib.error.URLError as exc:
            raise GitHubError(None, f"{method} {path}: {exc.reason}") from None
        return json.loads(raw.decode("utf-8")) if raw else {}
