"""Post-deploy endpoint check: served revision, prediction shape and preset ordering."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "ui" / "backend"))

import catalog  # noqa: E402
import endpoint_check  # noqa: E402

PKG = "arn:aws:sagemaker:us-east-1:123456789012:model-package/ModelCreditRisk/4"
MODEL = catalog.load({"MLP_MODELS": json.dumps([{
    "id": "credit-risk", "model_package_group": "ModelCreditRisk", "pipeline": "credit-risk",
    "features": ["income", "debt_to_income"],
    "presets": {"Risky applicant": {"income": 40000, "debt_to_income": 0.7},
                "Safe applicant": {"income": 90000, "debt_to_income": 0.1}},
}])})[0]


class FakeSageMaker:
    def __init__(self, status="InService", package=PKG, primary=True):
        self.status, self.package, self.primary = status, package, primary

    def describe_endpoint(self, EndpointName):
        return {"EndpointStatus": self.status, "EndpointConfigName": f"{EndpointName}-v4"}

    def describe_endpoint_config(self, EndpointConfigName):
        return {"ProductionVariants": [{"ModelName": EndpointConfigName}]}

    def describe_model(self, ModelName):
        container = {"ModelPackageName": self.package}
        return {"PrimaryContainer": container} if self.primary else {"Containers": [container]}


class FakeRuntime:
    def __init__(self, body):
        self.body, self.requests = body, []

    def invoke_endpoint(self, **kwargs):
        self.requests.append(kwargs)
        return {"Body": io.BytesIO(json.dumps(self.body).encode())}


def run(sm=None, body=None, **kwargs):
    rt = FakeRuntime(body if body is not None else {"prediction": [1, 0], "score": [0.82, 0.07]})
    return endpoint_check.check(sm or FakeSageMaker(), rt, "credit-risk-qa", PKG, MODEL, **kwargs), rt


def test_good_release_passes_and_sends_presets_in_feature_order():
    problems, rt = run(higher="Risky applicant", lower="Safe applicant")
    assert problems == []
    req = rt.requests[0]
    assert req["EndpointName"] == "credit-risk-qa" and req["ContentType"] == "application/json"
    assert json.loads(req["Body"]) == {"features": [[40000, 0.7], [90000, 0.1]]}


def test_reads_the_package_from_containers_too():
    problems, _ = run(sm=FakeSageMaker(primary=False))
    assert problems == []


def test_endpoint_not_in_service_fails_before_scoring():
    problems, rt = run(sm=FakeSageMaker(status="Updating"))
    assert problems == ["credit-risk-qa is Updating, not InService"]
    assert rt.requests == []


def test_wrong_served_revision_fails():
    other = PKG.replace("/4", "/3")
    problems, rt = run(sm=FakeSageMaker(package=other))
    assert problems == [f"credit-risk-qa serves {other}, not {PKG}"]
    assert rt.requests == []


@pytest.mark.parametrize("body, expected", [
    ({"prediction": [1], "score": [0.8]}, "expected 2 predictions and scores"),
    ({"prediction": [2, 0], "score": [0.8, 0.1]}, "Risky applicant: prediction 2 is not 0 or 1"),
    ({"prediction": [1, 0], "score": [1.4, 0.1]}, "Risky applicant: score 1.4 is not in [0, 1]"),
    ({"prediction": [1, 0], "score": [0.8, None]}, "Safe applicant: score None is not in [0, 1]"),
])
def test_bad_predictions_fail(body, expected):
    problems, _ = run(body=body)
    assert problems and expected in problems[0]


def test_risky_must_score_above_safe():
    problems, _ = run(body={"prediction": [0, 0], "score": [0.10, 0.30]}, higher="Risky applicant", lower="Safe applicant")
    assert problems == ["Risky applicant scored 0.100, not above Safe applicant at 0.300"]


def test_unknown_preset_name_is_reported():
    problems, _ = run(higher="Risky applicant", lower="Nobody")
    assert problems == ["no preset named Nobody"]


def test_model_without_presets_cannot_be_checked():
    bare = catalog.load({"MLP_MODELS": json.dumps([{
        "id": "bare", "model_package_group": "G", "pipeline": "p", "features": ["x"]}])})[0]
    problems = endpoint_check.check(FakeSageMaker(), FakeRuntime({}), "e", PKG, bare)
    assert problems == ["model bare has no presets to score"]


def test_cli_exit_codes(monkeypatch, capsys):
    import boto3

    good = FakeRuntime({"prediction": [1, 0], "score": [0.9, 0.1]})
    clients = {"sagemaker": FakeSageMaker(), "sagemaker-runtime": good}
    monkeypatch.setattr(boto3, "client", lambda name, region_name=None: clients[name])
    monkeypatch.setattr(catalog, "load", lambda: [MODEL])
    assert endpoint_check.main(["--endpoint", "credit-risk-qa", "--package-arn", PKG]) == 0
    assert "OK credit-risk-qa serves" in capsys.readouterr().out

    clients["sagemaker"] = FakeSageMaker(status="Failed")
    assert endpoint_check.main(["--endpoint", "credit-risk-qa", "--package-arn", PKG]) == 1
    assert "FAIL credit-risk-qa is Failed" in capsys.readouterr().out


def test_cli_requires_higher_and_lower_together():
    with pytest.raises(SystemExit):
        endpoint_check.main(["--endpoint", "e", "--package-arn", PKG, "--higher", "Risky applicant"])


def test_catalog_presets_cover_the_cli_defaults_used_by_the_workflow():
    real = next(m for m in catalog.load({}) if m.id == "credit-risk")
    assert {"Risky applicant", "Safe applicant"} <= set(real.presets)
