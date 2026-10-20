"""Model code tab: read the files a data scientist may change and propose edits as a pull request.

Only the model's own files are editable (EDITABLE_FILES); everything else in the repository
(workflows, Terraform, Dockerfile, the console) stays with the platform. In model.yaml only the
fields in EDITABLE_CONFIG may change, and thresholds may only go up: lowering one needs a second
senior data scientist. Changes never land on the branch directly; they open a pull request, so
the build workflow's tests and a reviewer still apply.

Token:
  MLP_GITHUB_PR_TOKEN  fine-grained token with "Contents" and "Pull requests" read and write on
                       the model repos. Without it the tab is read-only (public repos only).
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import yaml

API = "https://api.github.com"
EDITABLE_FILES = ("train.py", "evaluate.py", "preprocess.py", "inference.py", "requirements.txt", "model.yaml")
EDITABLE_CONFIG = (
    "data.train_uri", "data.target_column", "steps.prepare.enabled",
    "steps.train.instance", "steps.train.max_runtime", "steps.train.framework_version", "steps.evaluate.instance",
    "evaluation.metrics", "evaluation.thresholds", "serving.instance", "serving.instance_count",
)
MAX_BYTES = 200_000
MAX_SERVING_INSTANCES = 4
INSTANCE_TYPE = re.compile(r"ml\.[a-z0-9]+\.[a-z0-9]+")


class ChangeRejected(ValueError):
    """A proposed edit breaks the editing rules; the message says which and why."""


class CodeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ----- editing rules ---------------------------------------------------------------------
def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out or {prefix.rstrip("."): {}}
    return {prefix.rstrip("."): value}


def _editable(path: str) -> bool:
    return any(path == p or path.startswith(p + ".") for p in EDITABLE_CONFIG)


def check_config(old_text: str, new_text: str) -> None:
    try:
        new = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise ChangeRejected(f"model.yaml is not valid YAML: {exc}") from None
    if not isinstance(new, dict):
        raise ChangeRejected("model.yaml must be a mapping")
    old = yaml.safe_load(old_text) or {}
    before, after = _flatten(old), _flatten(new)
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    locked = sorted(k for k in changed if not _editable(k))
    if locked:
        raise ChangeRejected(f"model.yaml: {', '.join(locked)} {'is' if len(locked) == 1 else 'are'} managed by the "
                             "platform and cannot be changed here")
    old_t = (old.get("evaluation") or {}).get("thresholds") or {}
    new_t = (new.get("evaluation") or {}).get("thresholds") or {}
    for metric, minimum in old_t.items():
        value = new_t.get(metric)
        if not isinstance(value, (int, float)) or value < minimum:
            raise ChangeRejected(f"model.yaml: lowering or removing the {metric} threshold ({minimum}) needs a "
                                 "second senior data scientist; ask the platform team")
    for path in ("steps.train.instance", "steps.evaluate.instance", "serving.instance"):
        value = after.get(path)
        if path in changed and not (isinstance(value, str) and INSTANCE_TYPE.fullmatch(value)):
            raise ChangeRejected(f"model.yaml: {path} must be a SageMaker instance type such as ml.m5.large")
    count = after.get("serving.instance_count")
    if "serving.instance_count" in changed and not (
            isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= MAX_SERVING_INSTANCES):
        raise ChangeRejected(f"model.yaml: serving.instance_count must be 1 to {MAX_SERVING_INSTANCES}")


def check_requirements(text: str) -> None:
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-") or "://" in line or line.startswith("git+") or " @ " in line:
            raise ChangeRejected(f"requirements.txt line {number}: only plain package requirements are allowed "
                                 f"(no options, URLs or local paths): {raw.strip()!r}")


def validate(path: str, old_text: Optional[str], new_text: str) -> None:
    """Raise ChangeRejected unless new_text is an allowed edit of path."""
    if path not in EDITABLE_FILES:
        raise ChangeRejected(f"{path} is not one of the files a data scientist can change")
    if len(new_text.encode()) > MAX_BYTES:
        raise ChangeRejected(f"{path} is larger than {MAX_BYTES // 1000} KB")
    if path.endswith(".py"):
        try:
            compile(new_text, path, "exec")
        except SyntaxError as exc:
            raise ChangeRejected(f"{path} line {exc.lineno}: {exc.msg}") from None
    elif path == "requirements.txt":
        check_requirements(new_text)
    elif path == "model.yaml":
        if old_text is None:
            raise ChangeRejected("model.yaml is missing on the branch; ask the platform team")
        check_config(old_text, new_text)


# ----- GitHub -------------------------------------------------------------------------------
def _message(body: bytes) -> str:
    try:
        return str(json.loads(body or b"{}").get("message", ""))
    except (ValueError, AttributeError):
        return ""


class CodeRepo:
    def __init__(self, env: Optional[dict[str, str]] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        env = dict(os.environ if env is None else env)
        self.token = env.get("MLP_GITHUB_PR_TOKEN", "").strip()
        self.read_token = self.token or env.get("MLP_GITHUB_TOKEN", "").strip()
        self._clock = clock

    @property
    def can_propose(self) -> bool:
        return bool(self.token)

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
                 token: Optional[str] = None) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "ml-lifecycle-console"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise CodeError(404, "not found") from None
            raise CodeError(exc.code, _message(exc.read()) or f"GitHub returned {exc.code}") from None
        except urllib.error.URLError as exc:
            raise CodeError(502, f"cannot reach GitHub: {exc.reason}") from None
        return json.loads(raw) if raw else {}

    def files(self, repo: str, branch: str) -> dict[str, Optional[dict[str, str]]]:
        """Each editable file on branch as {"text", "sha"}, or None when the file does not exist."""
        out: dict[str, Optional[dict[str, str]]] = {}
        ref = urllib.parse.quote(branch, safe="")
        for path in EDITABLE_FILES:
            try:
                item = self._request("GET", f"/repos/{repo}/contents/{path}?ref={ref}", token=self.read_token)
            except CodeError as exc:
                if exc.status != 404:
                    raise
                out[path] = None
                continue
            out[path] = {"text": base64.b64decode(item.get("content", "")).decode("utf-8"), "sha": item["sha"]}
        return out

    def propose(self, repo: str, base: str, changes: dict[str, str], current: dict[str, Optional[dict[str, str]]],
                user: str, title: str, description: str) -> dict[str, Any]:
        """Create a branch from base, commit each changed file, and open a pull request."""
        if not self.token:
            raise CodeError(409, "proposing changes needs MLP_GITHUB_PR_TOKEN")
        head = self._request("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(base, safe='')}",
                             token=self.token)["object"]["sha"]
        slug = re.sub(r"[^a-z0-9]+", "-", user.lower()).strip("-")[:30] or "console"
        branch = f"console/{slug}-{self._clock():%Y%m%d-%H%M%S}"
        self._request("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head}, token=self.token)
        for path in sorted(changes):
            body: dict[str, Any] = {
                "message": f"Update {path}\n\nRequested in the lifecycle console by {user}.",
                "content": base64.b64encode(changes[path].encode()).decode(), "branch": branch,
            }
            if current.get(path):
                body["sha"] = current[path]["sha"]
            self._request("PUT", f"/repos/{repo}/contents/{path}", body, token=self.token)
        files = "\n".join(f"- `{p}`" for p in sorted(changes))
        pr = self._request("POST", f"/repos/{repo}/pulls", {
            "title": title, "head": branch, "base": base,
            "body": (f"{description.strip()}\n\n" if description.strip() else "")
                    + f"Requested in the lifecycle console by **{user}**.\n\nFiles changed:\n{files}\n\n"
                      "A reviewer other than the requester merges this once the build workflow passes.",
        }, token=self.token)
        return {"url": pr.get("html_url"), "number": pr.get("number"), "branch": branch, "files": sorted(changes)}
