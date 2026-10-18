"""Console GitHub tab: workflow runs, failed-job alert, log excerpts, caching and token handling."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui" / "backend"))

import app as console  # noqa: E402
import catalog  # noqa: E402
import github_ci  # noqa: E402
from github_ci import MAX_LINES, GitHubCI, GitHubError, excerpt  # noqa: E402

REPO = "acme/credit-risk-poc"
LOG = "\n".join([
    "2026-10-03T22:35:30.0000000Z ##[group]Run actions/checkout@v4",
    "2026-10-03T22:35:31.0000000Z \x1b[36;1mfetching\x1b[0m",
    "2026-10-03T22:35:32.0000000Z ##[endgroup]",
    *[f"2026-10-03T22:35:4{i}.0000000Z line {i}" for i in range(5)],
    "2026-10-03T22:36:01.5104650Z ##[error]Credentials could not be loaded",
    "2026-10-03T22:36:02.0000000Z Post job cleanup.",
])


def run(rid, status="completed", conclusion="success", title="commit"):
    return {"id": rid, "run_number": rid, "display_title": title, "head_sha": "abcdef1234", "head_branch": "main",
            "event": "push", "actor": {"login": "deb"}, "status": status, "conclusion": conclusion,
            "run_started_at": "2026-10-18T21:00:00Z", "updated_at": "2026-10-18T21:03:00Z",
            "html_url": f"https://github.com/{REPO}/actions/runs/{rid}", "run_attempt": 1}


def job(jid, conclusion="failure", failed_step="Configure AWS credentials"):
    steps = [{"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success"},
             {"number": 2, "name": failed_step, "status": "completed", "conclusion": conclusion}]
    return {"id": jid, "name": "train", "status": "completed", "conclusion": conclusion, "steps": steps,
            "started_at": "2026-10-18T21:00:00Z", "completed_at": "2026-10-18T21:01:00Z",
            "html_url": f"https://github.com/{REPO}/actions/runs/1/job/{jid}"}


class FakeGitHub(GitHubCI):
    def __init__(self, runs, jobs, token="tok", log=LOG):
        super().__init__({"MLP_GITHUB_TOKEN": token} if token else {}, clock=lambda: self.now)
        self.now, self.calls = 0.0, []
        self.data = {"runs": runs, "jobs": jobs, "log": log}

    def _request(self, path, accept="application/vnd.github+json"):
        self.calls.append(path)
        if path.endswith("/logs"):
            return self.data["log"].encode()
        if "/jobs" in path:
            return json.dumps({"jobs": self.data["jobs"].get(int(path.split("/")[-2]), [])}).encode()
        return json.dumps({"workflow_runs": self.data["runs"]}).encode()


def test_excerpt_keeps_errors_with_context_and_strips_noise():
    out = excerpt(LOG)
    texts = [l["text"] for l in out["lines"]]
    assert out["errors"] == 1 and out["total_lines"] == 10
    assert "Credentials could not be loaded" in texts and "line 0" in texts
    assert [l for l in out["lines"] if l["error"]][0]["text"] == "Credentials could not be loaded"
    assert not any("2026-10-03T" in t or "\x1b" in t or "##[error]" in t for t in texts)


def test_excerpt_marks_gaps_and_falls_back_to_tail():
    far = "\n".join([f"x{i}" for i in range(100)] + ["##[error]boom"] + [f"y{i}" for i in range(100)] + ["##[error]bang"])
    out = excerpt(far)
    assert any(l["gap"] for l in out["lines"]) and out["errors"] == 2
    tail = excerpt("\n".join(f"line {i}" for i in range(100)))
    assert tail["errors"] == 0 and tail["lines"][-1]["text"] == "line 99" and len(tail["lines"]) == 40


def test_excerpt_is_capped():
    out = excerpt("\n".join("##[error]e" for _ in range(500)))
    assert out["truncated"] and len(out["lines"]) == MAX_LINES


def test_runs_are_cached_for_a_minute():
    gh = FakeGitHub([run(1)], {})
    gh.runs(REPO, "ci.yml")
    gh.runs(REPO, "ci.yml")
    assert len(gh.calls) == 1
    gh.now = 61
    gh.runs(REPO, "ci.yml")
    assert len(gh.calls) == 2


def test_finished_jobs_cached_active_jobs_reread():
    gh = FakeGitHub([], {1: [job(9)]})
    gh.jobs(REPO, 1, finished=True)
    gh.jobs(REPO, 1, finished=True)
    gh.jobs(REPO, 1, finished=False)
    assert len(gh.calls) == 2


def test_logs_need_a_token():
    with pytest.raises(GitHubError, match="MLP_GITHUB_TOKEN") as exc:
        FakeGitHub([], {}, token="").failure_log(REPO, 9)
    assert exc.value.status == 401


class FakeOpener:
    def __init__(self, status, body):
        self.status, self.body, self.seen = status, body, {}

    def open(self, req, timeout):
        self.seen["auth"] = req.get_header("Authorization")
        raise urllib.error.HTTPError(req.full_url, self.status, "error", {}, io.BytesIO(self.body))


def test_rate_limit_message_and_auth_header(monkeypatch):
    opener = FakeOpener(403, b'{"message": "API rate limit exceeded for 1.2.3.4."}')
    monkeypatch.setattr(github_ci, "_OPENER", opener)
    with pytest.raises(GitHubError, match="rate limit reached; add MLP_GITHUB_TOKEN"):
        GitHubCI({"MLP_GITHUB_TOKEN": "tok"}).runs(REPO, "ci.yml")
    assert opener.seen["auth"] == "Bearer tok"


def test_non_json_error_body_does_not_crash(monkeypatch):
    monkeypatch.setattr(github_ci, "_OPENER", FakeOpener(400, b"<Error><Code>InvalidAuth</Code></Error>"))
    with pytest.raises(GitHubError, match="GitHub returned 400") as exc:
        GitHubCI({"MLP_GITHUB_TOKEN": "tok"}).failure_log(REPO, 9)
    assert exc.value.status == 400


@pytest.mark.parametrize("target, keeps_token", [
    ("https://results-receiver.actions.githubusercontent.com/log.txt", False),
    ("https://api.github.com/repositories/1/actions/jobs/9/logs", True),
])
def test_redirect_drops_token_when_leaving_github(target, keeps_token):
    req = urllib.request.Request(f"{github_ci.API}/repos/{REPO}/actions/jobs/9/logs", headers={"Authorization": "Bearer tok"})
    new = github_ci._DropAuthOnRedirect().redirect_request(req, None, 302, "Found", {}, target)
    assert new.has_header("Authorization") is keeps_token


ENTRY = {"id": "credit-risk", "name": "Credit risk", "model_package_group": "ModelCreditRisk", "pipeline": "credit-risk",
         "features": ["income"], "github": {"repo": REPO, "workflow": "ml-lifecycle.yml", "branch": "main"}}


@pytest.fixture
def client(monkeypatch):
    def make(runs, jobs, token="tok", entries=(ENTRY,)):
        gh = FakeGitHub(runs, jobs, token=token)
        monkeypatch.setattr(console, "MODELS", catalog.load({"MLP_MODELS": json.dumps(list(entries))}))
        monkeypatch.setattr(console, "GITHUB", gh)
        return gh, TestClient(console.app)
    return make


def test_alert_when_latest_finished_run_failed(client):
    gh, c = client([run(3, status="in_progress", conclusion=None), run(2, conclusion="failure"), run(1)],
                   {2: [job(20), job(21, conclusion="success", failed_step="Build")]})
    body = c.get("/api/github").json()
    assert body["configured"] and body["repo"] == REPO and body["active"]
    assert body["alert"]["run"]["id"] == 2
    assert [j["id"] for j in body["alert"]["jobs"]] == [20]
    assert body["alert"]["jobs"][0]["failed_step"] == "Configure AWS credentials"
    assert f"/repos/{REPO}/actions/workflows/ml-lifecycle.yml/runs?per_page=15&branch=main" in gh.calls


def test_no_alert_when_latest_finished_run_passed(client):
    _, c = client([run(2), run(1, conclusion="failure")], {})
    assert c.get("/api/github").json()["alert"] is None


def test_model_without_repo_is_not_configured(client):
    _, c = client([], {}, entries=({**ENTRY, "github": None},))
    assert c.get("/api/github").json() == {"configured": False, "model": "credit-risk"}
    assert c.get("/api/github/run", params={"run_id": 1}).status_code == 404


def test_run_detail_only_for_listed_runs(client):
    _, c = client([run(2, conclusion="failure")], {2: [job(20)]})
    assert c.get("/api/github/run", params={"run_id": 2}).json()["jobs"][0]["id"] == 20
    assert c.get("/api/github/run", params={"run_id": 999}).status_code == 404


def test_log_excerpt_route(client):
    gh, c = client([], {})
    body = c.get("/api/github/log", params={"job_id": 20}).json()
    assert body["errors"] == 1
    assert gh.calls == [f"/repos/{REPO}/actions/jobs/20/logs"]


def test_log_without_token_explains(client):
    _, c = client([], {}, token="")
    r = c.get("/api/github/log", params={"job_id": 20})
    assert r.status_code == 409 and "MLP_GITHUB_TOKEN" in r.json()["detail"]
