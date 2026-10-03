"""Approval console backend (FastAPI).

MLP_CONSOLE_MODE=local  (default) mocked AWS via scripts/local_e2e.LocalPlatform; the signed-in
                        user is chosen in the UI (X-Console-User header) instead of Okta.
MLP_CONSOLE_MODE=remote lists packages from the real SageMaker registry (caller's AWS
                        credentials, read-only) and forwards decisions to the private management
                        API (MLP_API_URL) with the browser's Okta bearer token. Approval rules are
                        enforced by the management API in both modes, never by this backend.
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

MODE = os.environ.get("MLP_CONSOLE_MODE", "local").lower()
SEED_CANDIDATES = int(os.environ.get("MLP_CONSOLE_SEED_CANDIDATES", "2"))
DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"

app = FastAPI(title="ML Platform approval console")
_lock = threading.Lock()
_state: dict[str, Any] = {}


class Decision(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_package_arn: str
    decision: str
    canonical_hash: str
    comment: str = ""


class Arn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_package_arn: str


# ----- shared helpers ----------------------------------------------------------------------
def _split_s3(uri: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(uri)
    return parsed.netloc, parsed.path.lstrip("/")


def _read_json_s3(s3: Any, uri: str) -> dict[str, Any]:
    if not uri:
        return {}
    bucket, key = _split_s3(uri)
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return {}


def _summarise(pkg: dict[str, Any], candidate: dict[str, Any], lifecycle_state: str | None) -> dict[str, Any]:
    meta = pkg.get("CustomerMetadataProperties") or {}
    return {
        "model_package_arn": pkg["ModelPackageArn"],
        "version": pkg.get("ModelPackageVersion"),
        "group": pkg.get("ModelPackageGroupName"),
        "approval_status": pkg.get("ModelApprovalStatus"),
        "created_at": str(pkg.get("CreationTime", "")),
        "tenant_id": meta.get("tenant_id"),
        "model_id": meta.get("model_id"),
        "canonical_hash": meta.get("canonical_hash"),
        "model_data_sha256": meta.get("model_data_sha256"),
        "source_commit": meta.get("source_commit"),
        "submitted_by": meta.get("submitted_by"),
        "approved_by": meta.get("approved_by") or meta.get("rejected_by"),
        "approval_id": meta.get("approval_id"),
        "evidence_uri": meta.get("evidence_uri"),
        "platform_registered": bool(meta.get("canonical_hash")),
        "metrics": {k: v for k, v in (candidate.get("metrics") or {}).items() if isinstance(v, (int, float))},
        "thresholds": candidate.get("thresholds") or {},
        "image_uri": candidate.get("image_uri") or _image(pkg),
        "lifecycle_state": lifecycle_state,
        "approved_outside_api": pkg.get("ModelApprovalStatus") == "Approved" and not meta.get("approval_id"),
    }


def _image(pkg: dict[str, Any]) -> str | None:
    containers = (pkg.get("InferenceSpecification") or {}).get("Containers") or [{}]
    return containers[0].get("Image")


def _list_packages(sm: Any, group: str) -> list[dict[str, Any]]:
    arns, kwargs = [], {"ModelPackageGroupName": group, "SortBy": "CreationTime", "SortOrder": "Descending"}
    while True:
        page = sm.list_model_packages(**kwargs)
        arns += [p["ModelPackageArn"] for p in page.get("ModelPackageSummaryList", [])]
        if not page.get("NextToken"):
            return [sm.describe_model_package(ModelPackageName=a) for a in arns]
        kwargs["NextToken"] = page["NextToken"]


# ----- local mode --------------------------------------------------------------------------
def _local() -> Any:
    if "lp" not in _state:
        raise HTTPException(503, "local platform not ready")
    return _state["lp"]


def _local_user(user: str | None) -> str:
    return (user or "").strip().lower() or _state["default_user"]


def _local_lifecycle(lp: Any, meta: dict[str, str]) -> dict[str, Any]:
    if not meta.get("model_data_sha256"):
        return {}
    return lp.ddb.Table(lp.tables["lifecycle"]).get_item(
        Key={"pk": f"{meta['tenant_id']}#{meta['model_id']}", "sk": f"PKG#{meta['model_data_sha256']}"}
    ).get("Item") or {}


def _local_package(lp: Any, pkg: dict[str, Any]) -> dict[str, Any]:
    meta = pkg.get("CustomerMetadataProperties") or {}
    candidate = _read_json_s3(lp.s3, meta.get("evidence_uri", ""))
    return _summarise(pkg, candidate, _local_lifecycle(lp, meta).get("state"))


def _local_history(lp: Any, pkg: dict[str, Any]) -> list[dict[str, Any]]:
    meta = pkg.get("CustomerMetadataProperties") or {}
    key = f"{meta.get('tenant_id', lp.cfg.tenant_id)}#{meta.get('model_id', lp.cfg.model_id)}"
    items = lp.ddb.Table(lp.tables["decision_log"]).query(
        KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": key})["Items"]
    arn = pkg["ModelPackageArn"]
    rows = [i for i in items if i.get("model_package_arn") == arn]
    return [{"at": i["sk"].split("#")[0], "action": i["action"], "actor": i.get("actor"),
             "comment": i.get("comment") or i.get("reason")} for i in sorted(rows, key=lambda i: i["sk"])]


APPROVER_ROLE = "senior_data_scientist"
TRAINER_ROLES = {"senior_data_scientist", "data_scientist"}


def _identity_view(item: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """Mirror of the management API checks, for display only; the API remains the enforcer."""
    roles = list(item.get("roles") or [])
    tenants = list(item.get("tenants") or [])
    active = item.get("status", "active") == "active"
    in_tenant = tenant_id in tenants or "*" in tenants
    return {"email": item["pk"], "github_login": item.get("github_login"), "roles": roles, "tenants": tenants,
            "active": active, "in_tenant": in_tenant,
            "can_approve": active and in_tenant and APPROVER_ROLE in roles,
            "can_train": active and in_tenant and bool(TRAINER_ROLES & set(roles))}


def _start_local() -> None:
    from moto import mock_aws

    from local_e2e import (APPROVER, DATA_SCIENTIST_GITHUB, SUBMITTER_GITHUB, LocalPlatform,
                           force_fake_credentials)

    force_fake_credentials()
    mock = mock_aws()
    mock.start()
    work = Path(tempfile.mkdtemp(prefix="mlp-console-"))
    lp = LocalPlatform(work)
    lp.bootstrap()
    submitters = [DATA_SCIENTIST_GITHUB, SUBMITTER_GITHUB]
    for seed in range(1, SEED_CANDIDATES + 1):
        lp.train_and_register(rows=3000, seed=seed, submitted_by=submitters[(seed - 1) % len(submitters)])
    _state.update({"mock": mock, "lp": lp, "default_user": APPROVER})


# ----- remote mode -------------------------------------------------------------------------
def _remote_clients() -> tuple[Any, Any]:
    import boto3

    if "sm" not in _state:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        _state["sm"], _state["s3"] = boto3.client("sagemaker", region_name=region), boto3.client("s3", region_name=region)
    return _state["sm"], _state["s3"]


def _remote_api(method: str, path: str, token: str | None, body: dict | None = None,
                query: dict | None = None) -> tuple[int, dict[str, Any]]:
    base = os.environ.get("MLP_API_URL", "").rstrip("/")
    if not base:
        raise HTTPException(500, "MLP_API_URL is not set")
    if not token:
        raise HTTPException(401, "Okta access token required")
    url = f"{base}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _group() -> str:
    if MODE == "local":
        return _local().cfg.model_package_group
    group = os.environ.get("MLP_MODEL_PACKAGE_GROUP")
    if not group:
        from ml_platform.config import ModelConfig

        group = ModelConfig.load(str(ROOT / "model.yaml")).model_package_group
    return group


# ----- routes ------------------------------------------------------------------------------
@app.on_event("startup")
def startup() -> None:
    if MODE == "local":
        _start_local()


@app.get("/api/session")
def session(x_console_user: Optional[str] = Header(default=None)) -> dict[str, Any]:
    if MODE == "local":
        lp = _local()
        identities = [_identity_view(i, lp.cfg.tenant_id) for i in lp.identities()]
        user = _local_user(x_console_user)
        me = next((i for i in identities if i["email"] == user), None)
        return {"mode": "local", "user": user, "me": me, "group": lp.cfg.model_package_group,
                "model_id": lp.cfg.model_id, "tenant_id": lp.cfg.tenant_id,
                "identities": sorted(identities, key=lambda i: (not i["can_approve"], i["email"])),
                "deploy_parameter": lp.deploy_parameter()}
    return {"mode": "remote", "user": None, "me": None, "group": _group(), "identities": [], "deploy_parameter": None}


@app.get("/api/packages")
def packages() -> list[dict[str, Any]]:
    with _lock:
        if MODE == "local":
            lp = _local()
            return [_local_package(lp, p) for p in _list_packages(lp.sm, lp.cfg.model_package_group)]
        sm, s3 = _remote_clients()
        out = []
        for pkg in _list_packages(sm, _group()):
            meta = pkg.get("CustomerMetadataProperties") or {}
            out.append(_summarise(pkg, _read_json_s3(s3, meta.get("evidence_uri", "")), None))
        return out


@app.get("/api/package")
def package(arn: str, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    with _lock:
        if MODE == "local":
            lp = _local()
            pkg = lp.sm.describe_model_package(ModelPackageName=arn)
            return {**_local_package(lp, pkg), "history": _local_history(lp, pkg)}
        sm, s3 = _remote_clients()
        pkg = sm.describe_model_package(ModelPackageName=arn)
        meta = pkg.get("CustomerMetadataProperties") or {}
        summary = _summarise(pkg, _read_json_s3(s3, meta.get("evidence_uri", "")), None)
        if authorization:
            status, body = _remote_api("GET", "/approvals", authorization, query={"model_package_arn": arn})
            if status == 200:
                summary["lifecycle_state"] = body.get("lifecycle_state")
        return {**summary, "history": []}


@app.post("/api/decision")
def decision(req: Decision, x_console_user: Optional[str] = Header(default=None),
             authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    with _lock:
        if MODE == "local":
            lp = _local()
            result = lp.decide(_local_user(x_console_user), req.model_package_arn, req.decision,
                               req.canonical_hash, req.comment)
            body = json.loads(result["body"])
            if result["statusCode"] != 200:
                raise HTTPException(result["statusCode"], body.get("error", "refused"))
            captured = lp.capture(req.model_package_arn)
            return {**body, "capture_action": captured["action"], "deploy_parameter": lp.deploy_parameter()}
        status, body = _remote_api("POST", "/approvals", authorization, body=req.model_dump())
        if status != 200:
            raise HTTPException(status, body.get("error", "refused"))
        return body


@app.post("/api/simulate/train")
def simulate_train(x_console_user: Optional[str] = Header(default=None)) -> dict[str, Any]:
    if MODE != "local":
        raise HTTPException(404, "local mode only")
    with _lock:
        lp = _local()
        user = _local_user(x_console_user)
        item = next((i for i in lp.identities() if i["pk"] == user), None)
        if not item or not _identity_view(item, lp.cfg.tenant_id)["can_train"]:
            raise HTTPException(403, f"{user} cannot start training for tenant {lp.cfg.tenant_id}")
        result = lp.train_and_register(rows=3000, seed=random.randint(10, 10_000),
                                       submitted_by=item.get("github_login") or user)
    return {"registered": result.get("registered"), "metrics": result.get("metrics"),
            "passed": result.get("passed"), "registration": result.get("registration")}


@app.post("/api/simulate/console-approve")
def simulate_console_approve(req: Arn) -> dict[str, Any]:
    """Approve directly in the registry, bypassing the management API, to show violation handling."""
    if MODE != "local":
        raise HTTPException(404, "local mode only")
    with _lock:
        lp = _local()
        meta = lp.sm.describe_model_package(ModelPackageName=req.model_package_arn).get("CustomerMetadataProperties") or {}
        # moto drops metadata on status-only updates; SageMaker keeps it, so pass it through unchanged.
        lp.sm.update_model_package(ModelPackageArn=req.model_package_arn, ModelApprovalStatus="Approved",
                                   CustomerMetadataProperties=meta)
        captured = lp.capture(req.model_package_arn)
    return {"capture_action": captured["action"], "deploy_parameter": lp.deploy_parameter()}


if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        return FileResponse(DIST / "index.html")
