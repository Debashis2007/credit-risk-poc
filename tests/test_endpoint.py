"""Console Test endpoint tab: SageMaker status, scoring, request signing, and isolation from moto."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui" / "backend"))

from endpoint import MAX_APPLICANTS, Endpoint, EndpointError  # noqa: E402

FEATURES = ["income", "debt_to_income", "credit_utilization", "late_payments_12m",
            "credit_history_years", "loan_amount", "open_accounts"]
ENV = {"MLP_ENDPOINT_AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY": "secret"}


def make(env=None, name="credit-risk-qa", features=FEATURES) -> Endpoint:
    return Endpoint(name, features, ENV if env is None else env)
PACKAGE = "arn:aws:sagemaker:us-east-1:123456789012:model-package/ModelCreditRisk/3"
RISKY = {"income": 52000, "debt_to_income": 0.62, "credit_utilization": 0.81, "late_payments_12m": 3,
         "credit_history_years": 2, "loan_amount": 18000, "open_accounts": 6}


class FakeSageMaker:
    """Stands in for Endpoint._send; records each call and answers by X-Amz-Target or path."""

    def __init__(self, responses: dict[str, tuple[int, dict]]):
        self.responses, self.calls = responses, []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        key = headers.get("X-Amz-Target", "").split(".")[-1] or "Invoke"
        self.calls.append({"key": key, "url": url, "body": json.loads(body)})
        status, payload = self.responses[key]
        return status, json.dumps(payload).encode()


def _endpoint(fake: FakeSageMaker, **env) -> Endpoint:
    ep = make({**ENV, **env})
    ep._send = fake
    return ep


def test_unconfigured_without_name_or_keys():
    assert not make({}).configured
    assert not make(name=None).configured
    assert make().configured


def test_ignores_standard_aws_credentials():
    """moto overwrites AWS_ACCESS_KEY_ID in local mode, so only the dedicated variables count."""
    assert not make({"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}).configured


@pytest.fixture
def isolated_chain(monkeypatch):
    """Default credential chain that sees only what a test sets: no profiles, files or instance metadata."""
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    for key in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.setenv(key, "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    return monkeypatch


def signed_headers(ep: Endpoint, monkeypatch) -> dict[str, str]:
    seen = {}

    def urlopen(req, timeout):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return io.BytesIO(json.dumps({"prediction": [0], "score": [0.1]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _as_response(urlopen))
    ep.score([RISKY])
    return seen


def test_default_chain_signs_with_task_role_credentials(isolated_chain):
    """On ECS the task role arrives as temporary credentials, including a session token."""
    isolated_chain.setenv("AWS_ACCESS_KEY_ID", "ASIATASKROLE")
    isolated_chain.setenv("AWS_SECRET_ACCESS_KEY", "s")
    isolated_chain.setenv("AWS_SESSION_TOKEN", "tok")
    ep = make({"AWS_REGION": "eu-west-1"}, features=FEATURES)
    assert not ep.configured
    ep = Endpoint("credit-risk-qa", FEATURES, {"AWS_REGION": "eu-west-1"}, default_credentials=True)
    assert ep.configured and ep.region == "eu-west-1"
    headers = signed_headers(ep, isolated_chain)
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=ASIATASKROLE/")
    assert "/eu-west-1/sagemaker/" in headers["authorization"]
    assert headers["x-amz-security-token"] == "tok"


def test_dedicated_keys_win_over_default_chain(isolated_chain):
    isolated_chain.setenv("AWS_ACCESS_KEY_ID", "ASIATASKROLE")
    isolated_chain.setenv("AWS_SECRET_ACCESS_KEY", "s")
    ep = Endpoint("credit-risk-qa", FEATURES, ENV, default_credentials=True)
    assert "Credential=AKIAEXAMPLE/" in signed_headers(ep, isolated_chain)["authorization"]


def test_default_chain_without_credentials_explains(isolated_chain):
    ep = Endpoint("credit-risk-qa", FEATURES, {}, default_credentials=True)
    with pytest.raises(EndpointError, match="no AWS credentials") as exc:
        ep.score([RISKY])
    assert exc.value.status == 500


def test_score_uses_the_models_own_feature_order():
    fake = FakeSageMaker({"Invoke": (200, {"prediction": [0], "score": [0.2]})})
    ep = make(name="churn-qa", features=["tenure_months", "monthly_spend"])
    ep._send = fake
    ep.score([{"monthly_spend": 80, "tenure_months": 14}])
    assert fake.calls[0]["body"] == {"features": [[14.0, 80.0]]}
    assert fake.calls[0]["url"].endswith("/endpoints/churn-qa/invocations")


def test_status_follows_endpoint_to_model_package():
    fake = FakeSageMaker({
        "DescribeEndpoint": (200, {"EndpointStatus": "InService", "EndpointConfigName": "credit-risk-qa-1",
                                   "LastModifiedTime": 1_792_350_000.0}),
        "DescribeEndpointConfig": (200, {"ProductionVariants": [
            {"ModelName": "credit-risk-1", "InstanceType": "ml.t2.medium", "InitialInstanceCount": 1}]}),
        "DescribeModel": (200, {"Containers": [{"ModelPackageName": PACKAGE}]}),
    })
    status = _endpoint(fake).status()
    assert status["status"] == "InService"
    assert status["version"] == 3 and status["model_package_arn"] == PACKAGE
    assert status["instance_type"] == "ml.t2.medium"
    assert status["updated_at"].startswith("2026-")
    assert [c["key"] for c in fake.calls] == ["DescribeEndpoint", "DescribeEndpointConfig", "DescribeModel"]
    assert fake.calls[2]["body"] == {"ModelName": "credit-risk-1"}


def test_status_reports_deleted_endpoint():
    fake = FakeSageMaker({"DescribeEndpoint": (400, {"message": "Could not find endpoint \"credit-risk-qa\"."})})
    assert _endpoint(fake).status()["status"] == "NotDeployed"


def test_status_surfaces_access_errors():
    fake = FakeSageMaker({"DescribeEndpoint": (400, {"Message": "not authorized to perform sagemaker:DescribeEndpoint"})})
    with pytest.raises(EndpointError, match="not authorized"):
        _endpoint(fake).status()


def test_score_sends_features_in_training_order():
    fake = FakeSageMaker({"Invoke": (200, {"prediction": [1], "score": [0.84], "model_version": "credit-risk"})})
    out = _endpoint(fake).score([dict(reversed(list(RISKY.items())))])
    assert fake.calls[0]["body"] == {"features": [[RISKY[f] for f in FEATURES]]}
    assert fake.calls[0]["url"] == "https://runtime.sagemaker.us-east-1.amazonaws.com/endpoints/credit-risk-qa/invocations"
    assert out["results"] == [{"prediction": 1, "score": 0.84}]
    assert out["model_version"] == "credit-risk" and out["latency_ms"] >= 0


@pytest.mark.parametrize("applicants, message", [
    ([], "between 1 and"),
    ([RISKY] * (MAX_APPLICANTS + 1), "between 1 and"),
    ([{k: v for k, v in RISKY.items() if k != "income"}], "income must be a number"),
    ([{**RISKY, "loan_amount": "lots"}], "loan_amount must be a number"),
    ([{**RISKY, "income": float("nan")}], "income must be a finite number"),
])
def test_score_rejects_bad_input_before_calling_sagemaker(applicants, message):
    fake = FakeSageMaker({})
    with pytest.raises(ValueError, match=message):
        _endpoint(fake).score(applicants)
    assert fake.calls == []


def test_score_surfaces_endpoint_errors():
    fake = FakeSageMaker({"Invoke": (424, {"Message": "Received server error (500) from primary"})})
    with pytest.raises(EndpointError, match="server error") as exc:
        _endpoint(fake).score([RISKY])
    assert exc.value.status == 424


def test_requests_are_sigv4_signed_with_dedicated_keys(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen.update(url=req.full_url, headers={k.lower(): v for k, v in req.header_items()})
        return io.BytesIO(json.dumps({"prediction": [0], "score": [0.0]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _as_response(urlopen))
    make().score([RISKY])
    auth = seen["headers"]["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
    assert "/us-east-1/sagemaker/aws4_request" in auth
    assert "x-amz-date" in seen["headers"]


def test_real_calls_bypass_moto(monkeypatch):
    """Local mode runs inside mock_aws; the endpoint must still go to the network, not to moto."""
    from moto import mock_aws

    sent = []
    monkeypatch.setattr(urllib.request, "urlopen", _as_response(
        lambda req, timeout: sent.append(req.full_url) or io.BytesIO(b'{"prediction": [0], "score": [0.1]}')))
    with mock_aws():
        assert os.environ.get("AWS_ACCESS_KEY_ID") != ENV["MLP_ENDPOINT_AWS_ACCESS_KEY_ID"]
        make().score([RISKY])
    assert sent == ["https://runtime.sagemaker.us-east-1.amazonaws.com/endpoints/credit-risk-qa/invocations"]


def _as_response(fn):
    """Give BytesIO results the status attribute and context-manager protocol of an HTTP response."""
    def wrapped(req, timeout=None):
        body = fn(req, timeout)
        body.status = 200
        return body
    return wrapped
