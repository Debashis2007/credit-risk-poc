"""Model deploy workflow: triggers, staging → test → approval → Prod order, and the Terraform release."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML reads the bare key `on` as True.
    data["on"] = data.pop(True, data.get("on"))
    return data


DEPLOY = load("model-deploy.yml")
RELEASE = load("release-endpoint.yml")


def test_starts_on_approval_dispatch_or_by_hand():
    assert DEPLOY["on"]["repository_dispatch"]["types"] == ["model-approved"]
    assert DEPLOY["on"]["workflow_dispatch"]["inputs"]["model_package_arn"]["required"] is True


def test_jobs_run_staging_then_test_then_prod():
    jobs = DEPLOY["jobs"]
    assert list(jobs) == ["deploy-staging", "test-staging", "deploy-prod"]
    assert jobs["test-staging"]["needs"] == "deploy-staging"
    assert jobs["deploy-prod"]["needs"] == "test-staging"


def test_staging_and_prod_use_the_terraform_release_with_their_settings():
    staging, prod = DEPLOY["jobs"]["deploy-staging"], DEPLOY["jobs"]["deploy-prod"]
    for job in (staging, prod):
        assert job["uses"] == "./.github/workflows/release-endpoint.yml"
        assert job["secrets"] == "inherit"
        assert "client_payload.model_package_arn" in job["with"]["model_package_arn"]
        assert "inputs.model_package_arn" in job["with"]["model_package_arn"]
    assert staging["with"]["environment"] == "staging" and staging["with"]["instance_count"] == 1
    assert prod["with"]["environment"] == "prod" and prod["with"]["instance_count"] == 2
    assert "credit-risk-qa" in staging["with"]["endpoint_name"]
    assert "credit-risk-prod" in prod["with"]["endpoint_name"]


def test_test_staging_checks_the_staging_endpoint():
    job = DEPLOY["jobs"]["test-staging"]
    assert job["environment"] == "ml-staging"
    script = job["steps"][-1]["run"]
    assert "scripts/endpoint_check.py" in script and "credit-risk-qa" in script
    assert '--higher "Risky applicant" --lower "Safe applicant"' in script


def test_releases_never_overlap():
    assert DEPLOY["concurrency"]["cancel-in-progress"] is False


def test_prod_waits_for_its_github_environment():
    # Required reviewers on ml-prod are the manual approval before Prod.
    assert RELEASE["jobs"]["release"]["environment"] == "ml-${{ inputs.environment }}"


def test_release_verifies_the_package_then_plans_and_applies():
    steps = RELEASE["jobs"]["release"]["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]
    order = [names.index(n) for n in (
        "Verify the package is approved in this model's registry group",
        "Terraform init",
        "Adopt an endpoint created before Terraform managed it",
        "Terraform plan",
        "Terraform apply (blue/green; staging all at once, Prod 10% canary for 15 minutes)",
        "Wait for the endpoint",
        "Verify served revision and predictions",
    )]
    assert order == sorted(order)
    verify = steps[names.index("Verify the package is approved in this model's registry group")]["run"]
    assert '"$STATUS" = "Approved"' in verify and '"$GROUP" = "$MODEL_PACKAGE_GROUP"' in verify
    apply = steps[names.index("Terraform apply (blue/green; staging all at once, Prod 10% canary for 15 minutes)")]
    assert apply["run"] == "terraform apply -input=false tfplan"


def test_endpoint_terraform_never_replaces_a_live_endpoint_for_deployment_settings():
    tf = (WORKFLOWS.parents[1] / "infra" / "endpoint" / "main.tf").read_text()
    endpoint = tf[tf.index('resource "aws_sagemaker_endpoint" "this"'):tf.index('resource "aws_appautoscaling_target"')]
    assert "ignore_changes = [deployment_config]" in endpoint
    assert 'type                     = local.is_prod ? "CANARY" : "ALL_AT_ONCE"' in endpoint


def test_release_state_is_separate_per_environment():
    init = next(s for s in RELEASE["jobs"]["release"]["steps"] if s.get("name") == "Terraform init")["run"]
    assert "key=credit-risk/endpoint-${{ inputs.environment }}.tfstate" in init
    assert "use_lockfile=true" in init


def test_release_uses_oidc_not_stored_keys():
    job = RELEASE["jobs"]["release"]
    creds = next(s for s in job["steps"] if "configure-aws-credentials" in s.get("uses", ""))
    assert creds["with"]["role-to-assume"] == "${{ secrets.MLP_GHA_DEPLOY_ROLE_ARN }}"
    assert RELEASE["permissions"]["id-token"] == "write"
    text = (WORKFLOWS / "release-endpoint.yml").read_text()
    assert "aws-access-key-id" not in text and "AWS_SECRET_ACCESS_KEY" not in text


@pytest.mark.parametrize("name", ["model-deploy.yml", "release-endpoint.yml", "ml-lifecycle.yml"])
def test_actions_are_pinned_to_full_commit_shas(name):
    for ref in re.findall(r"uses:\s*([\w.-]+/[\w./-]+)@(\S+)", (WORKFLOWS / name).read_text()):
        assert re.fullmatch(r"[0-9a-f]{40}", ref[1]), f"{ref[0]} is pinned to {ref[1]!r}, not a 40-character SHA"
