#!/usr/bin/env python3
"""Run the v7 lifecycle locally against mocked AWS (moto): no AWS account or credentials needed.

What is real: this repo's train.py / evaluate.py, the platform's publish_candidate.py, and the
platform Lambda handlers (register_candidate, approval_api, capture_approval_event).
What is mocked: S3, DynamoDB, SageMaker (pipeline execution + model registry), ECR, SSM and
EventBridge. SageMaker training/processing jobs are replaced by running the scripts locally.

Usage (from the repo root, with the platform checked out in .platform):
    PYTHONPATH=.platform python scripts/local_e2e.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = Path(os.environ.get("MLP_PLATFORM_DIR", ROOT / ".platform")).resolve()


def force_fake_credentials() -> None:
    """Fake credentials so nothing can ever reach a real AWS account."""
    os.environ.update({
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
    })
    os.environ.pop("AWS_PROFILE", None)


import boto3  # noqa: E402

sys.path.insert(0, str(PLATFORM))
sys.path.insert(0, str(ROOT / "scripts"))
from make_synthetic_data import make_dataset  # noqa: E402
from ml_platform.config import ModelConfig  # noqa: E402

DATA_SCIENTIST = "data.scientist@example.com"
DATA_SCIENTIST_GITHUB = "ds-submitter"
APPROVER = "senior.datascientist@example.com"
APPROVER_GITHUB = "sds-approver"
SUBMITTER = "lead.datascientist@example.com"
SUBMITTER_GITHUB = "lead-sds"
VIEWER = "analyst@example.com"

# email, github login, roles, tenants (TENANT is replaced by the model's tenant)
TENANT = "<model-tenant>"
IDENTITIES = (
    (APPROVER, APPROVER_GITHUB, ["senior_data_scientist"], [TENANT]),
    (SUBMITTER, SUBMITTER_GITHUB, ["senior_data_scientist"], [TENANT]),
    (DATA_SCIENTIST, DATA_SCIENTIST_GITHUB, ["data_scientist"], [TENANT]),
    ("other.tenant.sds@example.com", "other-sds", ["senior_data_scientist"], ["other-tenant"]),
    (VIEWER, "analyst", ["viewer"], [TENANT]),
)


def _load_handler(name: str) -> Any:
    path = PLATFORM / "lambdas" / name / "handler.py"
    spec = importlib.util.spec_from_file_location(f"local_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(*args: str) -> None:
    subprocess.run([sys.executable, *args], check=True, cwd=ROOT, stdout=subprocess.DEVNULL)


def api_event(method: str, email: str, body: dict | None = None, query: dict | None = None) -> dict:
    """API Gateway proxy event as the Okta TOKEN authorizer would pass it to approval_api."""
    return {"httpMethod": method, "requestContext": {"authorizer": {"email": email, "principalId": email}},
            "body": json.dumps(body) if body is not None else None, "queryStringParameters": query}


class LocalPlatform:
    """Control-plane resources and Lambdas on moto. Call inside an active moto mock."""

    def __init__(self, work: Path):
        self.cfg = ModelConfig.load(str(ROOT / "model.yaml"))
        self.work = work
        self.account, self.region = self.cfg.account_id, self.cfg.region
        os.environ["MOTO_ACCOUNT_ID"] = self.account
        os.environ["AWS_DEFAULT_REGION"] = self.region
        self.tables = {k: f"mlp-{k}" for k in ("lifecycle", "decision_log", "identity", "locks")}
        self.data_bucket = f"ml-platform-data-{self.account}"
        self.staging_bucket = f"mlp-{self.account}-artifacts"
        self.artefact_bucket = f"mlp-{self.account}-artefacts"
        self.evidence_bucket = f"mlp-{self.account}-evidence"
        self.ecr_repo = f"ml-models/{self.cfg.model_id}"
        self.s3 = boto3.client("s3", region_name=self.region)
        self.sm = boto3.client("sagemaker", region_name=self.region)
        self.ecr = boto3.client("ecr", region_name=self.region)
        self.ssm = boto3.client("ssm", region_name=self.region)
        self.ddb = boto3.resource("dynamodb", region_name=self.region)
        self._handlers: dict[str, Any] = {}

    # ----- setup -------------------------------------------------------------------------
    def bootstrap(self) -> None:
        for bucket in (self.data_bucket, self.staging_bucket, self.artefact_bucket, self.evidence_bucket):
            self.s3.create_bucket(Bucket=bucket)
        client = boto3.client("dynamodb", region_name=self.region)
        for kind, name in self.tables.items():
            keys = [{"AttributeName": "pk", "KeyType": "HASH"}]
            attrs = [{"AttributeName": "pk", "AttributeType": "S"}]
            if kind in {"lifecycle", "decision_log"}:
                keys.append({"AttributeName": "sk", "KeyType": "RANGE"})
                attrs.append({"AttributeName": "sk", "AttributeType": "S"})
            client.create_table(TableName=name, KeySchema=keys, AttributeDefinitions=attrs,
                                BillingMode="PAY_PER_REQUEST")
        self.sm.create_model_package_group(ModelPackageGroupName=self.cfg.model_package_group)
        self.sm.create_pipeline(PipelineName=self.cfg.pipeline_name, RoleArn=self.cfg.pipeline_role_arn,
                                PipelineDefinition=json.dumps({"Version": "2020-12-01", "Steps": []}))
        self.ecr.create_repository(repositoryName=self.ecr_repo)
        identity = self.ddb.Table(self.tables["identity"])
        for email, login, roles, tenants in IDENTITIES:
            tenants = [self.cfg.tenant_id if t == TENANT else t for t in tenants]
            identity.put_item(Item={"pk": email, "roles": roles, "tenants": tenants,
                                    "status": "active", "github_login": login})
        os.environ.update({
            "LIFECYCLE_TABLE": self.tables["lifecycle"], "DECISION_LOG_TABLE": self.tables["decision_log"],
            "IDENTITY_TABLE": self.tables["identity"], "LOCKS_TABLE": self.tables["locks"],
            "ARTEFACT_BUCKET": self.artefact_bucket, "EVIDENCE_BUCKET": self.evidence_bucket,
            "ALLOWED_SOURCE_ACCOUNTS": self.account, "CONTROL_PLANE_ACCOUNT_ID": self.account,
            "SOURCE_READ_ROLE_NAME": "", "DEPLOY_PARAMETER_PREFIX": "/mlp/deploy",
        })

    def handler(self, name: str) -> Any:
        if name not in self._handlers:
            self._handlers[name] = _load_handler(name)
        return self._handlers[name]

    def identities(self) -> list[dict[str, Any]]:
        return self.ddb.Table(self.tables["identity"]).scan()["Items"]

    # ----- CI + pipeline ------------------------------------------------------------------
    def push_image(self, commit: str) -> str:
        manifest = json.dumps({
            "schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "size": 0,
                       "digest": "sha256:" + hashlib.sha256(commit.encode()).hexdigest()},
            "layers": []})
        image = self.ecr.put_image(repositoryName=self.ecr_repo, imageManifest=manifest, imageTag=commit)
        return f"{self.account}.dkr.ecr.{self.region}.amazonaws.com/{self.ecr_repo}@{image['image']['imageId']['imageDigest']}"

    def run_pipeline(self, rows: int = 5000, seed: int = 1, commit: str | None = None,
                     submitted_by: str = SUBMITTER_GITHUB, log=print) -> dict[str, Any]:
        """Image push, data upload, pipeline steps run locally, candidate publish. Returns run info."""
        cfg = self.cfg
        commit = commit or hashlib.sha1(uuid.uuid4().bytes).hexdigest()
        run_dir = self.work / commit[:12]
        image_uri = self.push_image(commit)
        log(f"image      {image_uri}")

        train_dir, eval_dir = run_dir / "train", run_dir / "validation"
        train_dir.mkdir(parents=True)
        eval_dir.mkdir()
        make_dataset(rows, seed=seed).to_csv(train_dir / "train.csv", index=False)
        make_dataset(max(rows // 4, 500), seed=seed + 1000).to_csv(eval_dir / "validation.csv", index=False)
        data_key = cfg.train_uri.split(f"s3://{self.data_bucket}/", 1)[1] + "train.csv"
        self.s3.upload_file(str(train_dir / "train.csv"), self.data_bucket, data_key)
        log(f"data       s3://{self.data_bucket}/{data_key} ({rows} rows)")

        output_prefix = f"s3://{self.staging_bucket}/pipelines/{cfg.model_id}/{commit}-1"
        params = {"ImageUri": image_uri, "DataUri": cfg.train_uri, "OutputPrefix": output_prefix,
                  "SourceCommit": commit, "SubmittedBy": submitted_by, "LineageSourcePackageArn": ""}
        execution_arn = self.sm.start_pipeline_execution(
            PipelineName=cfg.pipeline_name,
            PipelineParameters=[{"Name": k, "Value": v} for k, v in params.items()])["PipelineExecutionArn"]

        model_dir, evaluation_dir, candidate_dir = run_dir / "model", run_dir / "evaluation", run_dir / "candidate"
        _run_script("train.py", "--train", str(train_dir), "--model-dir", str(model_dir))
        packaged = run_dir / "packaged"
        packaged.mkdir()
        with tarfile.open(packaged / "model.tar.gz", "w:gz") as tar:
            for f in model_dir.iterdir():
                tar.add(f, arcname=f.name)
        staging_prefix = output_prefix.split(self.staging_bucket + "/", 1)[1]
        model_key = f"{staging_prefix}/model/train-job/output/model.tar.gz"
        self.s3.upload_file(str(packaged / "model.tar.gz"), self.staging_bucket, model_key)
        log("Train      model.tar.gz uploaded")

        _run_script("evaluate.py", "--model-dir", str(model_dir), "--input-dir", str(eval_dir),
                    "--output-dir", str(evaluation_dir))
        metrics = json.loads((evaluation_dir / "metrics.json").read_text())
        log(f"Evaluate   auc={metrics['auc']:.4f} accuracy={metrics['accuracy']:.4f}")
        failed = {m: metrics.get(m) for m, t in cfg.thresholds.items() if metrics.get(m, -1) < t}
        log(f"CheckMetric thresholds {cfg.thresholds} -> {'FAIL ' + str(failed) if failed else 'pass'}")
        info = {"execution_arn": execution_arn, "commit": commit, "image_uri": image_uri,
                "metrics": metrics, "passed": not failed}
        if failed:
            return info

        _run_script(str(PLATFORM / "ml_platform" / "scripts" / "publish_candidate.py"),
                    "--model-dir", str(packaged), "--evaluation-dir", str(evaluation_dir),
                    "--output-dir", str(candidate_dir),
                    "--model-data-url", f"s3://{self.staging_bucket}/{model_key}",
                    "--image-uri", image_uri, "--source-commit", commit, "--tenant-id", cfg.tenant_id,
                    "--model-id", cfg.model_id, "--model-package-group", cfg.model_package_group,
                    "--platform-version", cfg.platform_version, "--source-account-id", self.account,
                    "--thresholds", json.dumps(cfg.thresholds))
        self.s3.upload_file(str(candidate_dir / "candidate.json"), self.staging_bucket,
                            f"{staging_prefix}/candidate/candidate.json")
        info["candidate"] = json.loads((candidate_dir / "candidate.json").read_text())
        log("Publish    candidate.json written (no registration inside the pipeline)")
        return info

    def pipeline_succeeded_event(self, execution_arn: str) -> dict[str, Any]:
        return {"source": "aws.sagemaker", "account": self.account, "region": self.region,
                "detail-type": "SageMaker Model Building Pipeline Execution Status Change",
                "detail": {"currentPipelineExecutionStatus": "Succeeded", "pipelineExecutionArn": execution_arn}}

    def register(self, execution_arn: str) -> dict[str, Any]:
        return self.handler("register_candidate").lambda_handler(self.pipeline_succeeded_event(execution_arn), None)

    def train_and_register(self, rows: int = 5000, seed: int = 1, submitted_by: str = DATA_SCIENTIST_GITHUB,
                           log=lambda *_: None) -> dict[str, Any]:
        info = self.run_pipeline(rows=rows, seed=seed, submitted_by=submitted_by, log=log)
        if not info["passed"]:
            return {**info, "registered": False}
        reg = self.register(info["execution_arn"])
        return {**info, "registered": reg["statusCode"] == 200, "registration": json.loads(reg["body"])}

    # ----- approval + capture -------------------------------------------------------------
    def decide(self, email: str, package_arn: str, decision: str, canonical_hash: str, comment: str = "") -> dict:
        return self.handler("approval_api").lambda_handler(api_event("POST", email, {
            "model_package_arn": package_arn, "decision": decision, "canonical_hash": canonical_hash,
            "comment": comment}), None)

    def state_change_event(self, package_arn: str) -> dict[str, Any]:
        pkg = self.sm.describe_model_package(ModelPackageName=package_arn)
        return {"source": "aws.sagemaker", "detail-type": "SageMaker Model Package State Change",
                "account": self.account, "region": self.region,
                "detail": {"ModelPackageArn": package_arn, "ModelPackageGroupName": pkg["ModelPackageGroupName"],
                           "ModelPackageVersion": pkg.get("ModelPackageVersion"),
                           "ModelApprovalStatus": pkg["ModelApprovalStatus"],
                           "CustomerMetadataProperties": pkg.get("CustomerMetadataProperties")}}

    def capture(self, package_arn: str) -> dict[str, Any]:
        """What EventBridge does when a package changes state."""
        result = self.handler("capture_approval_event").lambda_handler(self.state_change_event(package_arn), None)
        return json.loads(result["body"])

    def deploy_parameter(self) -> str | None:
        try:
            return self.ssm.get_parameter(
                Name=f"/mlp/deploy/{self.cfg.model_package_group}/model_package_arn")["Parameter"]["Value"]
        except self.ssm.exceptions.ParameterNotFound:
            return None


def _step(title: str) -> None:
    print(f"\n== {title}")


def run(rows: int, work: Path) -> dict[str, Any]:
    from moto import mock_aws

    force_fake_credentials()
    with mock_aws():
        lp = LocalPlatform(work)
        cfg = lp.cfg
        _step("Platform bootstrap (mocked AWS)")
        lp.bootstrap()
        print(f"account {lp.account} ({cfg.training_target}), tables, buckets, group {cfg.model_package_group}")

        _step("CI image push + pipeline execution (SageMaker jobs run locally)")
        info = lp.run_pipeline(rows=rows, commit=hashlib.sha1(b"local-e2e").hexdigest())
        if not info["passed"]:
            raise SystemExit("evaluation gate failed; no candidate published")
        metrics = info["metrics"]

        _step("EventBridge: pipeline Succeeded -> register_candidate Lambda")
        reg = lp.register(info["execution_arn"])
        assert reg["statusCode"] == 200, reg
        reg_body = json.loads(reg["body"])
        package_arn, chash = reg_body["model_package_arn"], reg_body["canonical_hash"]
        status = lp.sm.describe_model_package(ModelPackageName=package_arn)["ModelApprovalStatus"]
        print(f"package    {package_arn}\nstatus     {status}\nhash       {chash}")
        again = json.loads(lp.register(info["execution_arn"])["body"])
        print(f"replay     idempotent={again.get('idempotent')} (same package, no duplicate)")

        _step("Management API: approval checks")
        own = lp.decide(SUBMITTER, package_arn, "approve", chash)
        print(f"submitter approves own model   -> {own['statusCode']} {json.loads(own['body'])['error']}")
        bad = lp.decide(APPROVER, package_arn, "approve", "0" * 64)
        print(f"approver with wrong hash       -> {bad['statusCode']} {json.loads(bad['body'])['error']}")
        ok = lp.decide(APPROVER, package_arn, "approve", chash, "local e2e")
        assert ok["statusCode"] == 200, ok
        print(f"senior DS approves, right hash -> {ok['statusCode']} approval_id={json.loads(ok['body'])['approval_id']}")

        _step("Package state change -> capture_approval_event Lambda")
        confirmed = lp.capture(package_arn)
        print(f"API approval     -> {confirmed['action']}; SSM deploy parameter set: {lp.deploy_parameter() == package_arn}")

        rogue = lp.sm.create_model_package(
            ModelPackageGroupName=cfg.model_package_group, ModelApprovalStatus="PendingManualApproval",
            InferenceSpecification={"Containers": [{"Image": info["image_uri"]}],
                                    "SupportedContentTypes": ["text/csv"], "SupportedResponseMIMETypes": ["text/csv"]},
            CustomerMetadataProperties={"tenant_id": cfg.tenant_id, "model_id": cfg.model_id})["ModelPackageArn"]
        lp.sm.update_model_package(ModelPackageArn=rogue, ModelApprovalStatus="Approved")
        violation = lp.capture(rogue)
        print(f"console approval -> {violation['action']}; deploy parameter unchanged: {lp.deploy_parameter() == package_arn}")

        _step("Governance records")
        lifecycle = lp.ddb.Table(lp.tables["lifecycle"]).get_item(
            Key={"pk": f"{cfg.tenant_id}#{cfg.model_id}", "sk": f"PKG#{info['candidate']['model_data_sha256']}"})["Item"]
        print(f"lifecycle  state={lifecycle['state']} decided_by={lifecycle.get('decided_by')}")
        log = sorted(lp.ddb.Table(lp.tables["decision_log"]).scan()["Items"], key=lambda i: i["sk"])
        for item in log:
            print(f"decision   {item['action']:<22} actor={item['actor']}")
        for bucket, label in ((lp.artefact_bucket, "artefacts"), (lp.evidence_bucket, "evidence")):
            keys = [o["Key"] for o in lp.s3.list_objects_v2(Bucket=bucket).get("Contents", [])]
            print(f"{label:<10} {keys}")

        return {"package_arn": package_arn, "metrics": metrics, "lifecycle_state": lifecycle["state"],
                "confirmed_action": confirmed["action"], "violation_action": violation["action"],
                "actions": [i["action"] for i in log], "idempotent": again.get("idempotent")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=5000)
    args = parser.parse_args()
    if not (PLATFORM / "ml_platform").is_dir():
        raise SystemExit(f"Platform checkout not found at {PLATFORM}; see README 'Run locally'.")
    with tempfile.TemporaryDirectory() as tmp:
        result = run(args.rows, Path(tmp))
    print("\nLocal v7 lifecycle completed:", json.dumps({k: result[k] for k in
          ("lifecycle_state", "confirmed_action", "violation_action")}))


if __name__ == "__main__":
    main()
