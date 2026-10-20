"""Deploy trigger: the SageMaker approval event, the platform Lambda and the GitHub dispatch fit together."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / ".platform" / "lambdas" / "trigger_github_deploy" / "handler.py"
TRIGGER_TF = (ROOT / "infra" / "deploy-trigger" / "main.tf").read_text()
PKG = "arn:aws:sagemaker:us-east-1:848973819860:model-package/ModelCreditRisk/7"

# Shape of the event SageMaker sends when a package's approval status changes.
APPROVED_EVENT = {
    "version": "0", "source": "aws.sagemaker", "detail-type": "SageMaker Model Package State Change",
    "account": "848973819860", "region": "us-east-1",
    "detail": {"ModelPackageGroupName": "ModelCreditRisk", "ModelPackageVersion": 7, "ModelPackageArn": PKG,
               "ModelApprovalStatus": "Approved"},
}


def test_event_rule_matches_approved_packages_in_the_credit_risk_group():
    assert 'source        = ["aws.sagemaker"]' in TRIGGER_TF
    assert '"detail-type" = ["SageMaker Model Package State Change"]' in TRIGGER_TF
    assert "ModelPackageGroupName = [var.model_package_group]" in TRIGGER_TF
    assert 'ModelApprovalStatus   = ["Approved"]' in TRIGGER_TF
    defaults = (ROOT / "infra" / "deploy-trigger" / "variables.tf").read_text()
    assert re.search(r'variable "model_package_group" \{\s*type\s*=\s*string\s*default\s*=\s*"ModelCreditRisk"', defaults)


def test_deploy_role_is_assumable_only_from_the_deploy_environments():
    boot = (ROOT / "infra" / "bootstrap" / "main.tf").read_text()
    variables = (ROOT / "infra" / "bootstrap" / "variables.tf").read_text()
    assert '"repo:${var.github_repository}:environment:${env}"' in boot
    assert '"${var.github_immutable_subject_prefix}:environment:${env}"' in boot
    assert re.search(r'default\s*=\s*"repo:Debashis2007@33207849/credit-risk-poc@1403610949"', variables)
    assert '"token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"' in boot
    assert re.search(r'default\s*=\s*\["ml-staging", "ml-prod"\]', variables)
    assert re.search(r'default\s*=\s*"Debashis2007/credit-risk-poc"', variables)
    # PassRole only to SageMaker, for the one serving role
    assert '"iam:PassedToService" = "sagemaker.amazonaws.com"' in boot


def test_lambda_reads_the_token_from_secrets_manager_only():
    assert "GITHUB_TOKEN_SECRET_ARN = aws_secretsmanager_secret.github_token.arn" in TRIGGER_TF
    assert "secretsmanager:GetSecretValue" in TRIGGER_TF
    assert "secret_string" not in TRIGGER_TF  # the token value is never in Terraform state


@pytest.fixture
def handler(monkeypatch):
    if not HANDLER.is_file():
        pytest.skip("ML-Platform is not checked out at .platform")
    spec = importlib.util.spec_from_file_location("trigger_github_deploy", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("GITHUB_OWNER", "Debashis2007")
    monkeypatch.setenv("GITHUB_REPO", "credit-risk-poc")
    monkeypatch.setenv("GITHUB_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:848973819860:secret:t")

    class Secrets:
        def get_secret_value(self, SecretId):
            return {"SecretString": "ghp_test"}

    monkeypatch.setattr(module, "_secrets", lambda: Secrets())
    sent = []

    class Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        sent.append(req)
        return Resp()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    return module, sent


def test_platform_lambda_dispatches_model_approved_to_this_repo(handler):
    module, sent = handler
    result = module.lambda_handler(APPROVED_EVENT, None)
    assert result["statusCode"] == 204
    req = sent[0]
    assert req.full_url == "https://api.github.com/repos/Debashis2007/credit-risk-poc/dispatches"
    assert req.get_header("Authorization") == "Bearer ghp_test"
    body = json.loads(req.data)
    assert body["event_type"] == "model-approved"
    assert body["client_payload"]["model_package_arn"] == PKG
    assert body["client_payload"]["model_package_version"] == 7


def test_dispatch_event_type_matches_the_deploy_workflow(handler):
    import yaml

    module, sent = handler
    module.lambda_handler(APPROVED_EVENT, None)
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "model-deploy.yml").read_text())
    on = workflow.get("on", workflow.get(True))
    assert json.loads(sent[0].data)["event_type"] in on["repository_dispatch"]["types"]
