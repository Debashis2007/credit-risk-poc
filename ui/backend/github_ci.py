"""GitHub Actions runs for the console's GitHub tab: run list, jobs and failed-step log excerpts.

Repository and workflow come from the model catalog (entry field "github"). Optional token:
  MLP_GITHUB_TOKEN   fine-grained token with read-only "Actions" and "Metadata" on the model repos.
                     Without it, public repos still list runs (60 requests/hour per IP, cached);
                     job logs always need a token.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

API = "https://api.github.com"
RUNS_TTL_S = 60
CONTEXT_BEFORE, CONTEXT_AFTER, MAX_LINES = 15, 3, 200
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Job logs redirect to blob storage, which rejects (and must never see) the GitHub token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_DropAuthOnRedirect)


def _message(body: bytes) -> str:
    try:
        return str(json.loads(body or b"{}").get("message", ""))
    except (ValueError, AttributeError):
        return ""


class GitHubCI:
    def __init__(self, env: Optional[dict[str, str]] = None, clock: Callable[[], float] = time.monotonic):
        env = dict(os.environ if env is None else env)
        self.token = env.get("MLP_GITHUB_TOKEN", "").strip()
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _request(self, path: str, accept: str = "application/vnd.github+json") -> bytes:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ml-lifecycle-console"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with _OPENER.open(urllib.request.Request(f"{API}{path}", headers=headers), timeout=20) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = _message(exc.read()) if exc.code != 404 else "not found"
            if exc.code == 403 and "rate limit" in detail.lower():
                detail = "GitHub rate limit reached; add MLP_GITHUB_TOKEN to raise it"
            raise GitHubError(exc.code, detail or f"GitHub returned {exc.code}") from None
        except urllib.error.URLError as exc:
            raise GitHubError(502, f"cannot reach GitHub: {exc.reason}") from None

    def _cached(self, key: str, ttl: Optional[float], load: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._cache.get(key)
            if hit and (ttl is None or self._clock() - hit[0] < ttl):
                return hit[1]
        value = load()
        with self._lock:
            self._cache[key] = (self._clock(), value)
        return value

    def runs(self, repo: str, workflow: str, branch: Optional[str] = None, limit: int = 15) -> list[dict[str, Any]]:
        query = f"?per_page={limit}" + (f"&branch={branch}" if branch else "")

        def load() -> list[dict[str, Any]]:
            data = json.loads(self._request(f"/repos/{repo}/actions/workflows/{workflow}/runs{query}"))
            return [_run_view(r) for r in data.get("workflow_runs", [])]
        return self._cached(f"runs:{repo}:{workflow}:{branch}", RUNS_TTL_S, load)

    def jobs(self, repo: str, run_id: int, finished: bool) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            data = json.loads(self._request(f"/repos/{repo}/actions/runs/{run_id}/jobs"))
            return [_job_view(j) for j in data.get("jobs", [])]
        # A finished run's jobs never change; an active run's are re-read each time.
        return self._cached(f"jobs:{repo}:{run_id}", None, load) if finished else load()

    def failure_log(self, repo: str, job_id: int) -> dict[str, Any]:
        if not self.token:
            raise GitHubError(401, "reading job logs needs MLP_GITHUB_TOKEN")
        raw = self._cached(f"log:{repo}:{job_id}", None,
                           lambda: self._request(f"/repos/{repo}/actions/jobs/{job_id}/logs", accept="*/*"))
        return excerpt(raw.decode("utf-8", errors="replace"))


def _run_view(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "number": r.get("run_number"), "title": r.get("display_title"),
            "sha": (r.get("head_sha") or "")[:7], "branch": r.get("head_branch"), "event": r.get("event"),
            "actor": (r.get("actor") or {}).get("login"), "status": r.get("status"), "conclusion": r.get("conclusion"),
            "started_at": r.get("run_started_at") or r.get("created_at"), "updated_at": r.get("updated_at"),
            "url": r.get("html_url"), "attempt": r.get("run_attempt")}


def _job_view(j: dict[str, Any]) -> dict[str, Any]:
    steps = [{"number": s.get("number"), "name": s.get("name"), "status": s.get("status"),
              "conclusion": s.get("conclusion")} for s in j.get("steps") or []]
    failed = next((s["name"] for s in steps if s["conclusion"] == "failure"), None)
    return {"id": j["id"], "name": j.get("name"), "status": j.get("status"), "conclusion": j.get("conclusion"),
            "started_at": j.get("started_at"), "completed_at": j.get("completed_at"), "url": j.get("html_url"),
            "failed_step": failed, "steps": steps}


def excerpt(log: str) -> dict[str, Any]:
    """Error lines with the lines leading up to them; the log's tail when nothing is marked as an error."""
    lines = [ANSI.sub("", TIMESTAMP.sub("", line)) for line in log.splitlines()]
    errors = [i for i, line in enumerate(lines) if "##[error]" in line]
    if errors:
        keep: set[int] = set()
        for i in errors:
            keep.update(range(max(0, i - CONTEXT_BEFORE), min(len(lines), i + CONTEXT_AFTER + 1)))
        picked = sorted(keep)
    else:
        picked = list(range(max(0, len(lines) - 40), len(lines)))
    truncated = len(picked) > MAX_LINES
    picked = picked[-MAX_LINES:]
    out, previous = [], None
    for i in picked:
        if previous is not None and i != previous + 1:
            out.append({"text": "…", "error": False, "gap": True})
        text = lines[i]
        out.append({"text": text.replace("##[error]", ""), "error": "##[error]" in text, "gap": False})
        previous = i
    return {"lines": out, "errors": len(errors), "truncated": truncated, "total_lines": len(lines)}
