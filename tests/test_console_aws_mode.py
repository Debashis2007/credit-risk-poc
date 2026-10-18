"""Console aws mode: real registry listing, approve/reject in SageMaker, deploy on approval, real pipeline runs."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui" / "backend"))

import app as console  # noqa: E402
import catalog  # noqa: E402

ACCOUNT = "123456789012"
GROUP = "ModelCreditRisk"
CATALOG = [
    {"id": "credit-risk", "name": "Credit risk", "model_package_group": GROUP, "pipeline": "credit-risk",
     "endpoint": "credit-risk-qa", "thresholds": {"auc": 0.75}, "features": ["income", "debt_to_income"]},
    {"id": "churn", "name": "Customer churn", "model_package_group": "ModelChurn", "pipeline": "churn",
     "endpoint": "churn-qa", "thresholds": {"auc": 0.7}, "features": ["tenure_months", "monthly_spend"]},
]
ROLE = f"arn:aws:iam::{ACCOUNT}:role/ml-poc-sagemaker-exec"
T0 = datetime(2026, 10, 18, 19, 0, tzinfo=timezone.utc)


def pkg_arn(version: int) -> str:
    return f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:model-package/{GROUP}/{version}"


def not_found(op: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": "ValidationException", "Message": message}}, op)


class FakeS3:
    def __init__(self, objects: dict[str, dict]):
        self.objects = objects

    def get_object(self, Bucket: str, Key: str):
        return {"Body": io.BytesIO(json.dumps(self.objects[f"s3://{Bucket}/{Key}"]).encode())}


class FakeSageMaker:
    def __init__(self, aucs: dict[int, float], statuses: dict[int, str], serving: int | None = 3,
                 executing: bool = False, started_by: dict[int, str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.started_by = started_by or {}
        self.packages = {v: {
            "ModelPackageArn": pkg_arn(v), "ModelPackageVersion": v, "ModelPackageGroupName": GROUP,
            "ModelApprovalStatus": statuses[v], "CreationTime": T0, "LastModifiedTime": T0,
            "ModelPackageDescription": f"credit-risk pipeline execution e{v}",
            "InferenceSpecification": {"Containers": [{"Image": "sklearn:1.2-1"}],
                                       "SupportedRealtimeInferenceInstanceTypes": ["ml.t2.medium"]},
            "ModelMetrics": {"ModelQuality": {"Statistics": {"S3Uri": f"s3://bucket/runs/{v}/metrics.json"}}},
        } for v in aucs}
        self.serving, self.executing = serving, executing

    def _record(self, name: str, **kwargs):
        self.calls.append((name, kwargs))

    def called(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]

    def list_model_packages(self, **kwargs):
        self._record("list_model_packages", **kwargs)
        if kwargs.get("ModelPackageGroupName") != GROUP:
            return {"ModelPackageSummaryList": []}
        return {"ModelPackageSummaryList": [{"ModelPackageArn": p["ModelPackageArn"]}
                                            for _, p in sorted(self.packages.items(), reverse=True)]}

    def describe_model_package(self, ModelPackageName: str):
        return dict(self.packages[int(ModelPackageName.rsplit("/", 1)[-1])])

    def update_model_package(self, **kwargs):
        self._record("update_model_package", **kwargs)
        self.packages[int(kwargs["ModelPackageArn"].rsplit("/", 1)[-1])]["ModelApprovalStatus"] = kwargs["ModelApprovalStatus"]

    def describe_endpoint(self, EndpointName: str):
        if self.serving is None:
            raise not_found("DescribeEndpoint", f'Could not find endpoint "{EndpointName}".')
        return {"EndpointName": EndpointName, "EndpointConfigName": f"cfg-{self.serving}"}

    def describe_endpoint_config(self, EndpointConfigName: str):
        return {"ProductionVariants": [{"ModelName": f"model-{EndpointConfigName.split('-')[-1]}"}]}

    def describe_model(self, ModelName: str):
        return {"Containers": [{"ModelPackageName": pkg_arn(int(ModelName.split("-")[-1]))}]}

    def describe_pipeline(self, PipelineName: str):
        return {"PipelineArn": f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:pipeline/{PipelineName}", "RoleArn": ROLE,
                "PipelineDefinition": json.dumps({"Steps": [], "Parameters": []})}

    def create_model(self, **kwargs):
        self._record("create_model", **kwargs)

    def create_endpoint_config(self, **kwargs):
        self._record("create_endpoint_config", **kwargs)

    def update_endpoint(self, **kwargs):
        self._record("update_endpoint", **kwargs)

    def create_endpoint(self, **kwargs):
        self._record("create_endpoint", **kwargs)

    def describe_pipeline_execution(self, PipelineExecutionArn: str):
        self._record("describe_pipeline_execution", PipelineExecutionArn=PipelineExecutionArn)
        version = int(PipelineExecutionArn.rsplit("/e", 1)[-1])
        who = self.started_by.get(version)
        return {"PipelineExecutionDescription": f"Started from the lifecycle console by {who}" if who else None}

    def list_pipeline_executions(self, **kwargs):
        arn = f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:pipeline/credit-risk/execution/abc"
        return {"PipelineExecutionSummaries": [{
            "PipelineExecutionArn": arn, "StartTime": T0,
            "PipelineExecutionDescription": "Started from the lifecycle console by dana",
            "PipelineExecutionStatus": "Executing" if self.executing else "Succeeded"}]}

    def list_pipeline_parameters_for_execution(self, **kwargs):
        return {"PipelineParameters": [{"Name": "AucThreshold", "Value": "0.75"}]}

    def list_pipeline_execution_steps(self, **kwargs):
        return {"PipelineExecutionSteps": [
            {"StepName": "Register", "StepStatus": "Succeeded", "StartTime": T0, "EndTime": T0,
             "Metadata": {"RegisterModel": {"Arn": pkg_arn(4)}}},
            {"StepName": "CheckThreshold", "StepStatus": "Succeeded", "StartTime": T0, "EndTime": T0,
             "Metadata": {"Condition": {"Outcome": "True"}}},
        ]}

    def start_pipeline_execution(self, **kwargs):
        self._record("start_pipeline_execution", **kwargs)
        return {"PipelineExecutionArn": f"arn:aws:sagemaker:us-east-1:{ACCOUNT}:pipeline/credit-risk/execution/new"}


@pytest.fixture
def aws(monkeypatch):
    def make(aucs=None, statuses=None, **kwargs):
        aucs = aucs or {3: 0.79, 4: 0.81}
        statuses = statuses or {3: "Approved", 4: "PendingManualApproval"}
        sm = FakeSageMaker(aucs, statuses, **kwargs)
        s3 = FakeS3({f"s3://bucket/runs/{v}/metrics.json": {"auc": auc, "accuracy": 0.8, "rows": 1500}
                     for v, auc in aucs.items()})
        monkeypatch.setattr(console, "MODE", "aws")
        monkeypatch.setattr(console, "MODELS", catalog.load({"MLP_MODELS": json.dumps(CATALOG)}))
        monkeypatch.setattr(console, "_login", lambda request: request.headers.get("x-test-login", "console"))
        monkeypatch.setitem(console._state, "sm", sm)
        monkeypatch.setitem(console._state, "s3", s3)
        monkeypatch.setitem(console._state, "submitters", {})
        monkeypatch.setitem(console._state, "endpoints", {})
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("CONSOLE_ROLES", ROLES)
        for key in ("MLP_ENDPOINT_AWS_ACCESS_KEY_ID", "MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY"):
            monkeypatch.delenv(key, raising=False)
        return sm, TestClient(console.app)
    return make


ROLES = "sam:senior_data_scientist,lee:senior_data_scientist,dana:data_scientist,vic:viewer"


def decide(client, version: int, decision: str, comment: str = "", login: str = "console"):
    return client.post("/api/decision", headers={"x-test-login": login},
                       json={"model_package_arn": pkg_arn(version), "decision": decision, "comment": comment})


def test_session_reports_real_aws_and_serving_version(aws):
    _, client = aws()
    s = client.get("/api/session").json()
    assert s["mode"] == "aws" and s["region"] == "us-east-1" and s["endpoint"] == "credit-risk-qa"
    assert s["deploy_parameter"] == pkg_arn(3)
    assert s["me"]["can_approve"] and s["me"]["can_train"]


def test_packages_come_from_registry_with_metrics_and_thresholds(aws):
    _, client = aws()
    pkgs = {p["version"]: p for p in client.get("/api/packages").json()}
    assert pkgs[4]["approval_status"] == "PendingManualApproval"
    assert pkgs[4]["metrics"] == {"auc": 0.81, "accuracy": 0.8, "rows": 1500}
    assert pkgs[4]["thresholds"] == {"auc": 0.75}
    assert pkgs[3]["serving"] and not pkgs[4]["serving"]
    assert all(p["platform_registered"] and not p["approved_outside_api"] for p in pkgs.values())
    assert pkgs[4]["submitted_by"] == "pipeline"


def test_approve_updates_registry_and_deploys_to_endpoint(aws):
    sm, client = aws()
    r = decide(client, 4, "approve", "looks good")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval_status"] == "Approved"
    assert "switching to v4" in body["deploy_message"]
    update = sm.called("update_model_package")[0]
    assert update["ModelApprovalStatus"] == "Approved"
    assert update["CustomerMetadataProperties"] == {"approved_by": "console"}
    assert update["ApprovalDescription"] == "Approved by console in the lifecycle console: looks good"
    model = sm.called("create_model")[0]
    assert model["ExecutionRoleArn"] == ROLE
    assert model["Containers"] == [{"ModelPackageName": pkg_arn(4)}]
    assert model["ModelName"].startswith("credit-risk-qa-v4-")
    assert sm.called("create_endpoint_config")[0]["ProductionVariants"][0]["InstanceType"] == "ml.t2.medium"
    assert sm.called("update_endpoint")[0] == {"EndpointName": "credit-risk-qa", "EndpointConfigName": model["ModelName"]}


def test_approve_creates_endpoint_when_missing(aws):
    sm, client = aws(serving=None)
    body = decide(client, 4, "approve").json()
    assert "Creating endpoint credit-risk-qa with v4" in body["deploy_message"]
    assert sm.called("create_endpoint") and not sm.called("update_endpoint")


def test_approval_below_threshold_is_refused(aws):
    sm, client = aws(aucs={3: 0.79, 4: 0.70})
    r = decide(client, 4, "approve")
    assert r.status_code == 409 and "auc below threshold" in r.json()["detail"]
    assert not sm.called("update_model_package") and not sm.called("create_model")


def test_reject_updates_registry_without_deploying(aws):
    sm, client = aws()
    body = decide(client, 4, "reject", "not now").json()
    assert body["approval_status"] == "Rejected" and body["deploy_message"] is None
    assert sm.called("update_model_package")[0]["CustomerMetadataProperties"] == {"rejected_by": "console"}
    assert not sm.called("create_model")


def test_decided_package_cannot_be_decided_again(aws):
    sm, client = aws()
    r = decide(client, 3, "reject")
    assert r.status_code == 409 and "already Approved" in r.json()["detail"]
    assert not sm.called("update_model_package")


def test_unknown_decision_is_rejected(aws):
    _, client = aws()
    assert decide(client, 4, "maybe").status_code == 400


def test_package_detail_has_history(aws):
    _, client = aws()
    detail = client.get("/api/package", params={"arn": pkg_arn(3)}).json()
    assert [h["action"] for h in detail["history"]] == ["REGISTERED", "APPROVED"]
    assert detail["lifecycle_state"] == "Serving on endpoint"


def test_start_run_starts_the_real_pipeline(aws):
    sm, client = aws()
    r = client.post("/api/train")
    assert r.status_code == 200 and r.json()["run_id"].endswith("/execution/new")
    started = sm.called("start_pipeline_execution")[0]
    assert started["PipelineName"] == "credit-risk"
    assert started["PipelineExecutionDisplayName"].startswith("console-console-")


def test_start_run_refused_while_a_run_is_executing(aws):
    sm, client = aws(executing=True)
    assert client.post("/api/train").status_code == 409
    assert not sm.called("start_pipeline_execution")


def test_pipeline_runs_link_to_registered_package(aws):
    _, client = aws()
    run = client.get("/api/pipeline").json()["runs"][0]
    assert run["model_package_arn"] == pkg_arn(4) and run["gate"] == "passed"
    assert run["submitted_by"] == "dana"


def test_session_reports_each_login_role(aws):
    _, client = aws()
    s = client.get("/api/session", headers={"x-test-login": "dana"}).json()
    assert s["me"]["roles"] == ["data_scientist"]
    assert s["me"]["can_train"] and not s["me"]["can_approve"]
    viewer = client.get("/api/session", headers={"x-test-login": "vic"}).json()["me"]
    assert not viewer["can_train"] and not viewer["can_approve"]


def test_unlisted_login_is_viewer_when_login_required(aws, monkeypatch):
    aws()
    monkeypatch.setattr(console.AUTH, "enabled", True)
    me = console._aws_identity("mallory")
    assert me["roles"] == ["viewer"] and not me["can_approve"] and not me["can_train"]
    assert console._aws_identity("SAM")["can_approve"]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_data_scientist_cannot_decide(aws, decision):
    sm, client = aws()
    r = decide(client, 4, decision, login="dana")
    assert r.status_code == 403 and "needs a senior data scientist" in r.json()["detail"]
    assert not sm.called("update_model_package")


def test_viewer_cannot_start_run_and_data_scientist_can(aws):
    sm, client = aws()
    r = client.post("/api/train", headers={"x-test-login": "vic"})
    assert r.status_code == 403 and not sm.called("start_pipeline_execution")
    assert client.post("/api/train", headers={"x-test-login": "dana"}).status_code == 200
    assert sm.called("start_pipeline_execution")[0]["PipelineExecutionDescription"] == \
        "Started from the lifecycle console by dana"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_senior_cannot_decide_on_own_run(aws, decision):
    sm, client = aws(started_by={4: "sam"})
    r = decide(client, 4, decision, login="sam")
    assert r.status_code == 403 and "separation of duties" in r.json()["detail"]
    assert not sm.called("update_model_package")


def test_another_senior_approves_and_submitter_is_shown(aws):
    sm, client = aws(started_by={4: "dana"})
    pkgs = {p["version"]: p for p in client.get("/api/packages").json()}
    assert pkgs[4]["submitted_by"] == "dana"
    r = decide(client, 4, "approve", login="lee")
    assert r.status_code == 200, r.text
    assert sm.called("update_model_package")[0]["CustomerMetadataProperties"] == {"approved_by": "lee"}
    looked_up = [c["PipelineExecutionArn"].rsplit("/", 1)[-1] for c in sm.called("describe_pipeline_execution")]
    assert sorted(looked_up) == ["e3", "e4"]


def test_aws_errors_return_502(aws, monkeypatch):
    sm, client = aws()

    def denied(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "ListModelPackages")

    monkeypatch.setattr(sm, "list_model_packages", denied)
    r = client.get("/api/packages")
    assert r.status_code == 502 and r.json()["detail"] == "AWS AccessDeniedException: not authorized"


def test_session_lists_catalog_and_defaults_to_first_model(aws):
    _, client = aws()
    s = client.get("/api/session").json()
    assert [m["id"] for m in s["models"]] == ["credit-risk", "churn"]
    assert s["model"] == "credit-risk" and s["group"] == GROUP
    assert client.get("/api/session", params={"model": "gone"}).json()["model"] == "credit-risk"


def test_selected_model_scopes_registry_pipeline_and_training(aws):
    sm, client = aws()
    s = client.get("/api/session", params={"model": "churn"}).json()
    assert s["model"] == "churn" and s["group"] == "ModelChurn" and s["endpoint"] == "churn-qa"
    assert client.get("/api/packages", params={"model": "churn"}).json() == []
    assert sm.called("list_model_packages")[-1]["ModelPackageGroupName"] == "ModelChurn"
    assert client.get("/api/pipeline", params={"model": "churn"}).json()["name"] == "churn"
    client.post("/api/train", params={"model": "churn"}, headers={"x-test-login": "dana"})
    assert sm.called("start_pipeline_execution")[0]["PipelineName"] == "churn"


def test_unknown_model_is_refused(aws):
    sm, client = aws()
    assert client.get("/api/packages", params={"model": "gone"}).status_code == 404
    assert client.post("/api/train", params={"model": "gone"}).status_code == 404
    assert not sm.called("start_pipeline_execution")


def test_package_from_another_models_group_is_refused(aws):
    sm, client = aws()
    r = client.post("/api/decision", params={"model": "churn"},
                    json={"model_package_arn": pkg_arn(4), "decision": "approve"})
    assert r.status_code == 404 and "not a Customer churn package" in r.json()["detail"]
    assert client.get("/api/package", params={"arn": pkg_arn(4), "model": "churn"}).status_code == 404
    assert not sm.called("update_model_package")


def test_endpoint_form_comes_from_the_catalog(aws):
    _, client = aws()
    ep = console.Endpoint("churn-qa", ["tenure_months", "monthly_spend"],
                          {"MLP_ENDPOINT_AWS_ACCESS_KEY_ID": "AKIA", "MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY": "s"})
    ep._send = lambda url, body, headers: (400, b'{"message": "Could not find endpoint \\"churn-qa\\"."}')
    console._state["endpoints"]["churn"] = ep
    form = client.get("/api/endpoint", params={"model": "churn"}).json()
    assert form["status"] == "NotDeployed"
    assert [f["name"] for f in form["features"]] == ["tenure_months", "monthly_spend"]
    assert form["name"] == "churn-qa" and form["model"] == "churn"


def test_train_route_is_aws_mode_only(aws, monkeypatch):
    _, client = aws()
    monkeypatch.setattr(console, "MODE", "remote")
    assert client.post("/api/train").status_code == 404
