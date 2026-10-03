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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = Path(os.environ.get("MLP_PLATFORM_DIR", ROOT / ".platform")).resolve()

# Fake credentials so nothing can ever reach a real AWS account.
os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
})
os.environ.pop("AWS_PROFILE", None)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

sys.path.insert(0, str(PLATFORM))
sys.path.insert(0, str(ROOT / "scripts"))
from make_synthetic_data import make_dataset  # noqa: E402
from ml_platform.config import ModelConfig  # noqa: E402

SUBMITTER = "data.scientist@example.com"
SUBMITTER_GITHUB = "ds-submitter"
APPROVER = "senior.datascientist@example.com"


def _load_handler(name: str) -> Any:
    path = PLATFORM / "lambdas" / name / "handler.py"
    spec = importlib.util.spec_from_file_location(f"local_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _step(title: str) -> None:
    print(f"\n== {title}")


def _run(*args: str) -> None:
    subprocess.run([sys.executable, *args], check=True, cwd=ROOT, stdout=subprocess.DEVNULL)


def _tables(ddb: Any, names: dict[str, str]) -> None:
    for kind, name in names.items():
        keys = [{"AttributeName": "pk", "KeyType": "HASH"}]
        attrs = [{"AttributeName": "pk", "AttributeType": "S"}]
        if kind in {"lifecycle", "decision_log"}:
            keys.append({"AttributeName": "sk", "KeyType": "RANGE"})
            attrs.append({"AttributeName": "sk", "AttributeType": "S"})
        ddb.create_table(TableName=name, KeySchema=keys, AttributeDefinitions=attrs,
                         BillingMode="PAY_PER_REQUEST")


def _api_event(method: str, email: str, body: dict | None = None, query: dict | None = None) -> dict:
    return {"httpMethod": method, "requestContext": {"authorizer": {"email": email, "principalId": email}},
            "body": json.dumps(body) if body is not None else None, "queryStringParameters": query}


def _state_change_event(sm: Any, arn: str, account: str, region: str) -> dict:
    pkg = sm.describe_model_package(ModelPackageName=arn)
    return {"source": "aws.sagemaker", "detail-type": "SageMaker Model Package State Change",
            "account": account, "region": region,
            "detail": {"ModelPackageArn": arn, "ModelPackageGroupName": pkg["ModelPackageGroupName"],
                       "ModelPackageVersion": pkg.get("ModelPackageVersion"),
                       "ModelApprovalStatus": pkg["ModelApprovalStatus"],
                       "CustomerMetadataProperties": pkg.get("CustomerMetadataProperties")}}


def run(rows: int, work: Path) -> dict[str, Any]:
    cfg = ModelConfig.load(str(ROOT / "model.yaml"))
    account, region = cfg.account_id, cfg.region
    os.environ["MOTO_ACCOUNT_ID"] = account
    os.environ["AWS_DEFAULT_REGION"] = region
    prefix = f"{cfg.tenant_id}-{cfg.model_id}"
    names = {k: f"mlp-{k}" for k in ("lifecycle", "decision_log", "identity", "locks")}
    data_bucket = f"ml-platform-data-{account}"
    staging_bucket = f"mlp-{account}-artifacts"
    artefact_bucket, evidence_bucket = f"mlp-{account}-artefacts", f"mlp-{account}-evidence"
    commit = hashlib.sha1(b"local-e2e").hexdigest()

    with mock_aws():
        s3, sm, ecr = (boto3.client(n, region_name=region) for n in ("s3", "sagemaker", "ecr"))
        ssm, ddb = boto3.client("ssm", region_name=region), boto3.client("dynamodb", region_name=region)

        _step("Platform bootstrap (mocked AWS)")
        for bucket in (data_bucket, staging_bucket, artefact_bucket, evidence_bucket):
            s3.create_bucket(Bucket=bucket)
        _tables(ddb, names)
        sm.create_model_package_group(ModelPackageGroupName=cfg.model_package_group)
        boto3.resource("dynamodb", region_name=region).Table(names["identity"]).put_item(Item={
            "pk": APPROVER, "roles": ["senior_data_scientist"], "tenants": [cfg.tenant_id],
            "status": "active", "github_login": "sds-approver"})
        boto3.resource("dynamodb", region_name=region).Table(names["identity"]).put_item(Item={
            "pk": SUBMITTER, "roles": ["senior_data_scientist"], "tenants": [cfg.tenant_id],
            "status": "active", "github_login": SUBMITTER_GITHUB})
        os.environ.update({
            "LIFECYCLE_TABLE": names["lifecycle"], "DECISION_LOG_TABLE": names["decision_log"],
            "IDENTITY_TABLE": names["identity"], "LOCKS_TABLE": names["locks"],
            "ARTEFACT_BUCKET": artefact_bucket, "EVIDENCE_BUCKET": evidence_bucket,
            "ALLOWED_SOURCE_ACCOUNTS": account, "CONTROL_PLANE_ACCOUNT_ID": account,
            "SOURCE_READ_ROLE_NAME": "", "DEPLOY_PARAMETER_PREFIX": "/mlp/deploy",
        })
        print(f"account {account} ({cfg.training_target}), tables, buckets, group {cfg.model_package_group}")

        _step("Image build: push to ECR and pin by digest")
        repo = f"ml-models/{cfg.model_id}"
        ecr.create_repository(repositoryName=repo)
        manifest = json.dumps({"schemaVersion": 2,
                               "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                               "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                                          "size": 0, "digest": "sha256:" + hashlib.sha256(commit.encode()).hexdigest()},
                               "layers": []})
        digest = ecr.put_image(repositoryName=repo, imageManifest=manifest, imageTag=commit)["image"]["imageId"]["imageDigest"]
        image_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/{repo}@{digest}"
        print(image_uri)

        _step("Data: synthetic dataset uploaded to data.train_uri")
        train_dir, eval_dir = work / "train", work / "validation"
        train_dir.mkdir()
        eval_dir.mkdir()
        make_dataset(rows, seed=1).to_csv(train_dir / "train.csv", index=False)
        make_dataset(max(rows // 4, 500), seed=2).to_csv(eval_dir / "validation.csv", index=False)
        data_key = cfg.train_uri.split(f"s3://{data_bucket}/", 1)[1] + "train.csv"
        s3.upload_file(str(train_dir / "train.csv"), data_bucket, data_key)
        print(f"s3://{data_bucket}/{data_key} ({rows} rows)")

        _step("Pipeline execution (SageMaker jobs run locally)")
        output_prefix = f"s3://{staging_bucket}/pipelines/{cfg.model_id}/{commit}-1"
        sm.create_pipeline(PipelineName=cfg.pipeline_name, RoleArn=cfg.pipeline_role_arn,
                           PipelineDefinition=json.dumps({"Version": "2020-12-01", "Steps": []}))
        params = {"ImageUri": image_uri, "DataUri": cfg.train_uri, "OutputPrefix": output_prefix,
                  "SourceCommit": commit, "SubmittedBy": SUBMITTER_GITHUB, "LineageSourcePackageArn": ""}
        execution_arn = sm.start_pipeline_execution(
            PipelineName=cfg.pipeline_name,
            PipelineParameters=[{"Name": k, "Value": v} for k, v in params.items()])["PipelineExecutionArn"]

        model_dir, evaluation_dir, candidate_dir = work / "model", work / "evaluation", work / "candidate"
        _run("train.py", "--train", str(train_dir), "--model-dir", str(model_dir))
        packaged = work / "packaged"
        packaged.mkdir()
        with tarfile.open(packaged / "model.tar.gz", "w:gz") as tar:
            for f in model_dir.iterdir():
                tar.add(f, arcname=f.name)
        model_key = f"{output_prefix.split(staging_bucket + '/', 1)[1]}/model/train-job/output/model.tar.gz"
        s3.upload_file(str(packaged / "model.tar.gz"), staging_bucket, model_key)
        print("Train      model.tar.gz uploaded")

        _run("evaluate.py", "--model-dir", str(model_dir), "--input-dir", str(eval_dir),
             "--output-dir", str(evaluation_dir))
        metrics = json.loads((evaluation_dir / "metrics.json").read_text())
        print(f"Evaluate   auc={metrics['auc']:.4f} accuracy={metrics['accuracy']:.4f}")
        failed = {m: metrics.get(m) for m, t in cfg.thresholds.items() if metrics.get(m, -1) < t}
        print(f"CheckMetric thresholds {cfg.thresholds} -> {'FAIL ' + str(failed) if failed else 'pass'}")
        if failed:
            raise SystemExit("evaluation gate failed; no candidate published")

        _run(str(PLATFORM / "ml_platform" / "scripts" / "publish_candidate.py"),
             "--model-dir", str(packaged), "--evaluation-dir", str(evaluation_dir),
             "--output-dir", str(candidate_dir), "--model-data-url", f"s3://{staging_bucket}/{model_key}",
             "--image-uri", image_uri, "--source-commit", commit, "--tenant-id", cfg.tenant_id,
             "--model-id", cfg.model_id, "--model-package-group", cfg.model_package_group,
             "--platform-version", cfg.platform_version, "--source-account-id", account,
             "--thresholds", json.dumps(cfg.thresholds))
        candidate_key = f"{output_prefix.split(staging_bucket + '/', 1)[1]}/candidate/candidate.json"
        s3.upload_file(str(candidate_dir / "candidate.json"), staging_bucket, candidate_key)
        print("Publish    candidate.json written (no registration inside the pipeline)")

        _step("EventBridge: pipeline Succeeded -> register_candidate Lambda")
        register = _load_handler("register_candidate")
        succeeded = {"source": "aws.sagemaker", "account": account, "region": region,
                     "detail-type": "SageMaker Model Building Pipeline Execution Status Change",
                     "detail": {"currentPipelineExecutionStatus": "Succeeded",
                                "pipelineExecutionArn": execution_arn}}
        reg = register.lambda_handler(succeeded, None)
        assert reg["statusCode"] == 200, reg
        reg_body = json.loads(reg["body"])
        package_arn, chash = reg_body["model_package_arn"], reg_body["canonical_hash"]
        status = sm.describe_model_package(ModelPackageName=package_arn)["ModelApprovalStatus"]
        print(f"package    {package_arn}\nstatus     {status}\nhash       {chash}")
        again = json.loads(register.lambda_handler(succeeded, None)["body"])
        print(f"replay     idempotent={again.get('idempotent')} (same package, no duplicate)")

        _step("Management API: approval checks")
        api = _load_handler("approval_api")
        own = api.lambda_handler(_api_event("POST", SUBMITTER, {
            "model_package_arn": package_arn, "decision": "approve", "canonical_hash": chash}), None)
        print(f"submitter approves own model   -> {own['statusCode']} {json.loads(own['body'])['error']}")
        bad = api.lambda_handler(_api_event("POST", APPROVER, {
            "model_package_arn": package_arn, "decision": "approve", "canonical_hash": "0" * 64}), None)
        print(f"approver with wrong hash       -> {bad['statusCode']} {json.loads(bad['body'])['error']}")
        ok = api.lambda_handler(_api_event("POST", APPROVER, {
            "model_package_arn": package_arn, "decision": "approve", "canonical_hash": chash,
            "comment": "local e2e"}), None)
        assert ok["statusCode"] == 200, ok
        approval_id = json.loads(ok["body"])["approval_id"]
        print(f"senior DS approves, right hash -> {ok['statusCode']} approval_id={approval_id}")

        _step("Package state change -> capture_approval_event Lambda")
        capture = _load_handler("capture_approval_event")
        confirmed = json.loads(capture.lambda_handler(
            _state_change_event(sm, package_arn, account, region), None)["body"])
        deploy_param = ssm.get_parameter(
            Name=f"/mlp/deploy/{cfg.model_package_group}/model_package_arn")["Parameter"]["Value"]
        print(f"API approval     -> {confirmed['action']}; SSM deploy parameter set: {deploy_param == package_arn}")

        rogue = sm.create_model_package(
            ModelPackageGroupName=cfg.model_package_group, ModelApprovalStatus="PendingManualApproval",
            InferenceSpecification={"Containers": [{"Image": image_uri}],
                                    "SupportedContentTypes": ["text/csv"], "SupportedResponseMIMETypes": ["text/csv"]},
            CustomerMetadataProperties={"tenant_id": cfg.tenant_id, "model_id": cfg.model_id})["ModelPackageArn"]
        sm.update_model_package(ModelPackageArn=rogue, ModelApprovalStatus="Approved")
        violation = json.loads(capture.lambda_handler(
            _state_change_event(sm, rogue, account, region), None)["body"])
        param_after = ssm.get_parameter(
            Name=f"/mlp/deploy/{cfg.model_package_group}/model_package_arn")["Parameter"]["Value"]
        print(f"console approval -> {violation['action']}; deploy parameter unchanged: {param_after == package_arn}")

        _step("Governance records")
        res = boto3.resource("dynamodb", region_name=region)
        lifecycle = res.Table(names["lifecycle"]).get_item(
            Key={"pk": f"{cfg.tenant_id}#{cfg.model_id}", "sk": f"PKG#{json.loads((candidate_dir / 'candidate.json').read_text())['model_data_sha256']}"})["Item"]
        print(f"lifecycle  state={lifecycle['state']} decided_by={lifecycle.get('decided_by')}")
        log = sorted(res.Table(names["decision_log"]).scan()["Items"], key=lambda i: i["sk"])
        for item in log:
            print(f"decision   {item['action']:<22} actor={item['actor']}")
        evidence = [o["Key"] for o in s3.list_objects_v2(Bucket=evidence_bucket).get("Contents", [])]
        artefacts = [o["Key"] for o in s3.list_objects_v2(Bucket=artefact_bucket).get("Contents", [])]
        print(f"artefacts  {artefacts}\nevidence   {evidence}")

        return {"package_arn": package_arn, "metrics": metrics, "lifecycle_state": lifecycle["state"],
                "self_approval_status": own["statusCode"], "bad_hash_status": bad["statusCode"],
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
