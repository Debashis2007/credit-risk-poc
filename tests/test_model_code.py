"""Model code tab: editing rules, the GitHub pull request flow, and the console routes."""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui" / "backend"))

import app as console  # noqa: E402
import catalog  # noqa: E402
import model_code  # noqa: E402
from model_code import ChangeRejected, CodeError, CodeRepo, check_config, validate  # noqa: E402

MODEL_YAML = (ROOT / "model.yaml").read_text()
REPO = "acme/credit-risk-poc"


# ----- editing rules ---------------------------------------------------------------------
def edit(old: str, new: str) -> str:
    assert old in MODEL_YAML, old
    return MODEL_YAML.replace(old, new)


@pytest.mark.parametrize("old, new", [
    ("target_column: default", "target_column: defaulted"),
    ("instance: ml.m5.xlarge\n    max_runtime: 2h", "instance: ml.m5.2xlarge\n    max_runtime: 3h"),
    ("auc: 0.75", "auc: 0.80"),
    ("metrics: [auc, accuracy]", "metrics: [auc, accuracy, f1]"),
    ("prepare:\n    enabled: false", "prepare:\n    enabled: true"),
    ("instance_count: 2\n\ndeployment", "instance_count: 3\n\ndeployment"),
])
def test_allowed_model_yaml_edits_pass(old, new):
    check_config(MODEL_YAML, edit(old, new))


def test_adding_a_new_threshold_is_allowed():
    check_config(MODEL_YAML, edit("auc: 0.75", "auc: 0.75\n    accuracy: 0.7"))


def test_comments_and_formatting_are_not_changes():
    check_config(MODEL_YAML, "# reviewed\n" + MODEL_YAML.replace("auc: 0.75", "auc:    0.75   # agreed with risk"))


@pytest.mark.parametrize("old, new, locked", [
    ("id: credit-risk", "id: credit-risk-2", "model.id"),
    ("tenant_id: demo", "tenant_id: other", "model.tenant_id"),
    ('account_id: "848973819860"\n  region', 'account_id: "111111111111"\n  region', "aws.account_id"),
    ("model_package_group: ModelCreditRisk", "model_package_group: Other", "registry.model_package_group"),
    ("instance_count: 1 }", "instance_count: 5 }", "deployment.targets"),
    ("senior.datascientist@example.com", "me@example.com", "approvers"),
    ("train:\n    enabled: true", "train:\n    enabled: false", "steps.train.enabled"),
])
def test_platform_fields_are_locked(old, new, locked):
    with pytest.raises(ChangeRejected, match=locked.replace(".", r"\.")):
        check_config(MODEL_YAML, edit(old, new))


def test_lowering_or_removing_a_threshold_needs_a_second_senior():
    with pytest.raises(ChangeRejected, match="lowering or removing the auc threshold"):
        check_config(MODEL_YAML, edit("auc: 0.75", "auc: 0.7"))
    with pytest.raises(ChangeRejected, match="lowering or removing the auc threshold"):
        check_config(MODEL_YAML, edit("    auc: 0.75\n", "    accuracy: 0.9\n"))


@pytest.mark.parametrize("old, new, message", [
    ("instance: ml.m5.large", "instance: big-box", "serving.instance must be a SageMaker instance type"),
    ("instance_count: 2\n\ndeployment", "instance_count: 9\n\ndeployment", "serving.instance_count must be 1 to 4"),
])
def test_editable_fields_are_still_validated(old, new, message):
    with pytest.raises(ChangeRejected, match=message):
        check_config(MODEL_YAML, edit(old, new))


def test_invalid_yaml_is_rejected():
    with pytest.raises(ChangeRejected, match="not valid YAML"):
        check_config(MODEL_YAML, "model: [unclosed")
    with pytest.raises(ChangeRejected, match="must be a mapping"):
        check_config(MODEL_YAML, "- a list")


def test_python_must_compile():
    validate("train.py", "x = 1\n", "def train():\n    return 1\n")
    with pytest.raises(ChangeRejected, match="train.py line 1"):
        validate("train.py", "x = 1\n", "def train(:\n")


@pytest.mark.parametrize("line", ["-e .", "--index-url https://evil.example/simple", "git+https://github.com/x/y",
                                  "pkg @ file:///tmp/pkg", "pkg @ https://evil.example/pkg.whl", "-r other.txt"])
def test_requirements_allow_only_plain_packages(line):
    with pytest.raises(ChangeRejected, match="only plain package requirements"):
        validate("requirements.txt", "", f"pandas>=2.0\n{line}\n")


def test_plain_requirements_pass():
    validate("requirements.txt", "", "# comment\nscikit-learn>=1.3.0,<1.6\npandas==2.2.0  # pinned\n\nnumpy\n")


@pytest.mark.parametrize("path", [".github/workflows/model-deploy.yml", "infra/endpoint/main.tf", "Dockerfile",
                                  "ui/backend/app.py", "../train.py"])
def test_platform_files_cannot_be_changed(path):
    with pytest.raises(ChangeRejected, match="not one of the files"):
        validate(path, "", "anything")


def test_size_limit():
    with pytest.raises(ChangeRejected, match="larger than 200 KB"):
        validate("train.py", "", "x = 1\n" * 40_000)


# ----- GitHub pull request flow ----------------------------------------------------------
class FakeGitHub:
    """Enough of the GitHub REST API for CodeRepo: contents, refs and pulls."""

    def __init__(self, files: dict[str, str]):
        self.files = {p: {"text": t, "sha": f"sha-{p}"} for p, t in files.items()}
        self.calls: list[tuple[str, str, dict | None, str | None]] = []

    def request(self, method, path, body=None, token=None):
        self.calls.append((method, path, body, token))
        if method == "GET" and "/contents/" in path:
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            if name not in self.files:
                raise CodeError(404, "not found")
            item = self.files[name]
            return {"content": base64.b64encode(item["text"].encode()).decode(), "sha": item["sha"]}
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": "base-sha"}}
        if method == "POST" and path.endswith("/git/refs"):
            return {"ref": body["ref"]}
        if method == "PUT" and "/contents/" in path:
            return {"content": {"path": path.split("/contents/", 1)[1]}}
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": f"https://github.com/{REPO}/pull/7", "number": 7}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def repo():
    fake = FakeGitHub({"train.py": "x = 1\n", "evaluate.py": "y = 2\n", "inference.py": "z = 3\n",
                       "requirements.txt": "numpy\n", "model.yaml": MODEL_YAML})
    code = CodeRepo({"MLP_GITHUB_PR_TOKEN": "ghp_pr"}, clock=lambda: datetime(2026, 10, 20, 19, 30, tzinfo=timezone.utc))
    code._request = fake.request
    return code, fake


def test_files_reads_every_editable_file_and_marks_missing_ones(repo):
    code, fake = repo
    files = code.files(REPO, "main")
    assert list(files) == list(model_code.EDITABLE_FILES)
    assert files["train.py"] == {"text": "x = 1\n", "sha": "sha-train.py"}
    assert files["preprocess.py"] is None
    assert all(c[3] == "ghp_pr" for c in fake.calls)
    assert all("ref=main" in c[1] for c in fake.calls)


def test_propose_branches_commits_each_file_and_opens_a_pull_request(repo):
    code, fake = repo
    current = code.files(REPO, "main")
    fake.calls.clear()
    pr = code.propose(REPO, "main", {"train.py": "x = 2\n", "preprocess.py": "p = 1\n"}, current,
                      "Dana Scientist", "Tune the model", "Raised depth")
    assert pr == {"url": f"https://github.com/{REPO}/pull/7", "number": 7,
                  "branch": "console/dana-scientist-20261020-193000", "files": ["preprocess.py", "train.py"]}
    methods = [(m, p) for m, p, _, _ in fake.calls]
    assert methods == [
        ("GET", f"/repos/{REPO}/git/ref/heads/main"),
        ("POST", f"/repos/{REPO}/git/refs"),
        ("PUT", f"/repos/{REPO}/contents/preprocess.py"),
        ("PUT", f"/repos/{REPO}/contents/train.py"),
        ("POST", f"/repos/{REPO}/pulls"),
    ]
    ref = fake.calls[1][2]
    assert ref == {"ref": "refs/heads/console/dana-scientist-20261020-193000", "sha": "base-sha"}
    new_file, updated = fake.calls[2][2], fake.calls[3][2]
    assert "sha" not in new_file  # a file that did not exist is created
    assert updated["sha"] == "sha-train.py" and base64.b64decode(updated["content"]) == b"x = 2\n"
    assert updated["branch"] == "console/dana-scientist-20261020-193000"
    assert "Requested in the lifecycle console by Dana Scientist" in updated["message"]
    pull = fake.calls[4][2]
    assert pull["base"] == "main" and pull["head"] == ref["ref"].removeprefix("refs/heads/")
    assert pull["title"] == "Tune the model"
    assert pull["body"].startswith("Raised depth") and "**Dana Scientist**" in pull["body"]
    assert "A reviewer other than the requester merges this" in pull["body"]


def test_propose_without_a_pr_token_is_refused():
    code = CodeRepo({"MLP_GITHUB_TOKEN": "read-only"})
    assert not code.can_propose and code.read_token == "read-only"
    with pytest.raises(CodeError, match="MLP_GITHUB_PR_TOKEN"):
        code.propose(REPO, "main", {"train.py": "x"}, {}, "u", "t", "")


# ----- console routes --------------------------------------------------------------------
CATALOG = [{"id": "credit-risk", "name": "Credit risk", "model_package_group": "ModelCreditRisk",
            "pipeline": "credit-risk", "features": ["income"],
            "github": {"repo": REPO, "workflow": "ml-lifecycle.yml"}},
           {"id": "churn", "name": "Churn", "model_package_group": "ModelChurn", "pipeline": "churn",
            "features": ["tenure"]}]


@pytest.fixture
def api(monkeypatch, repo):
    code, fake = repo
    monkeypatch.setattr(console, "MODE", "aws")
    monkeypatch.setattr(console, "MODELS", catalog.load({"MLP_MODELS": json.dumps(CATALOG)}))
    monkeypatch.setattr(console, "CODE", code)
    monkeypatch.setattr(console, "_login", lambda request: request.headers.get("x-test-login", "dana"))
    monkeypatch.setenv("CONSOLE_ROLES", "dana:data_scientist,sam:senior_data_scientist,vic:viewer")
    return TestClient(console.app), fake


def propose(client, files, login="dana", **extra):
    return client.post("/api/code/propose", headers={"x-test-login": login}, json={"files": files, **extra})


def test_code_lists_editable_files_and_rules(api):
    client, _ = api
    body = client.get("/api/code").json()
    assert body["configured"] and body["can_propose"] and body["repo"] == REPO and body["branch"] == "main"
    assert [f["path"] for f in body["files"]] == list(model_code.EDITABLE_FILES)
    assert next(f for f in body["files"] if f["path"] == "preprocess.py") == {"path": "preprocess.py", "exists": False, "text": ""}
    assert "evaluation.thresholds" in body["editable_config"] and "aws.account_id" not in body["editable_config"]


def test_model_without_a_repo_is_not_configured(api):
    client, _ = api
    assert client.get("/api/code", params={"model": "churn"}).json() == {"configured": False, "model": "churn"}


def test_data_scientist_opens_a_pull_request_with_only_the_changed_files(api):
    client, fake = api
    r = propose(client, {"train.py": "x = 2\n", "evaluate.py": "y = 2\n", "model.yaml": edit("auc: 0.75", "auc: 0.8")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["number"] == 7 and body["files"] == ["model.yaml", "train.py"]
    pull = next(c[2] for c in fake.calls if c[1].endswith("/pulls"))
    assert pull["title"] == "Update model.yaml, train.py from the lifecycle console"
    assert "**dana**" in pull["body"]


def test_viewer_cannot_propose(api):
    client, fake = api
    r = propose(client, {"train.py": "x = 2\n"}, login="vic")
    assert r.status_code == 403 and "cannot change model code" in r.json()["detail"]
    assert not any(c[0] in ("PUT", "POST") for c in fake.calls)


def test_senior_data_scientist_can_also_propose(api):
    client, _ = api
    assert propose(client, {"train.py": "x = 2\n"}, login="sam").status_code == 200


@pytest.mark.parametrize("files, status, detail", [
    ({"Dockerfile": "FROM scratch"}, 422, "Dockerfile cannot be changed from the console"),
    ({"train.py": "x = 1\n"}, 400, "nothing changed"),
    ({"train.py": "def broken(:\n"}, 422, "train.py line 1"),
    ({"model.yaml": "placeholder"}, 422, "model.id"),
    ({"model.yaml": "placeholder-threshold"}, 422, "lowering or removing the auc threshold"),
    ({"requirements.txt": "numpy\n--extra-index-url https://x\n"}, 422, "only plain package requirements"),
])
def test_rule_breaking_proposals_are_refused_before_anything_is_written(api, files, status, detail):
    client, fake = api
    if files.get("model.yaml") == "placeholder":
        files = {"model.yaml": edit("id: credit-risk", "id: renamed")}
    elif files.get("model.yaml") == "placeholder-threshold":
        files = {"model.yaml": edit("auc: 0.75", "auc: 0.5")}
    r = propose(client, files)
    assert r.status_code == status and detail in r.json()["detail"]
    assert not any(c[0] in ("PUT", "POST") for c in fake.calls)


def test_proposing_without_a_pr_token_is_refused(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(console, "CODE", CodeRepo({}))
    r = propose(client, {"train.py": "x = 2\n"})
    assert r.status_code == 409 and "MLP_GITHUB_PR_TOKEN" in r.json()["detail"]


def test_github_failures_are_reported(api, repo):
    client, fake = api
    code, _ = repo

    def broken(method, path, body=None, token=None):
        raise CodeError(403, "Resource not accessible by personal access token")

    code._request = broken
    r = client.get("/api/code")
    assert r.status_code == 409 and "Resource not accessible" in r.json()["detail"]


def test_local_mode_uses_the_signed_in_identity(api, monkeypatch):
    client, fake = api

    class FakeLocal:
        class cfg:
            tenant_id = "demo"

        @staticmethod
        def identities():
            return [{"pk": "ds@example.com", "roles": ["data_scientist"], "tenants": ["demo"], "github_login": "ds-gh"},
                    {"pk": "viewer@example.com", "roles": ["viewer"], "tenants": ["demo"]}]

    monkeypatch.setattr(console, "MODE", "local")
    monkeypatch.setattr(console, "_local", lambda: FakeLocal)
    r = client.post("/api/code/propose", headers={"X-Console-User": "ds@example.com"}, json={"files": {"train.py": "x = 9\n"}})
    assert r.status_code == 200, r.text
    assert "**ds-gh**" in next(c[2] for c in fake.calls if c[1].endswith("/pulls"))["body"]
    r = client.post("/api/code/propose", headers={"X-Console-User": "viewer@example.com"}, json={"files": {"train.py": "x"}})
    assert r.status_code == 403


def test_remote_mode_has_no_code_tab_writes(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(console, "MODE", "remote")
    r = propose(client, {"train.py": "x = 2\n"})
    assert r.status_code == 404 and "aws or local mode" in r.json()["detail"]
