"""Lifecycle console backend (FastAPI): training pipeline view and approvals.

MLP_CONSOLE_MODE=local  (default) mocked AWS via scripts/local_e2e.LocalPlatform; the signed-in
                        user is chosen in the UI (X-Console-User header) instead of Okta.
MLP_CONSOLE_MODE=remote lists packages and pipeline executions from SageMaker (caller's AWS
                        credentials, read-only) and forwards decisions to the private management
                        API (MLP_API_URL) with the browser's Okta bearer token. Approval rules are
                        enforced by the management API in both modes, never by this backend.
MLP_CONSOLE_MODE=aws    single-account POC with no management API, for every model in the catalog
                        (catalog.py; requests pick one with ?model=): packages and pipeline executions
                        from SageMaker (AWS_* credentials); the signed-in console user approves or
                        rejects in the registry directly, an approval deploys to the model's endpoint,
                        and Start run starts the model's pipeline.
In every mode the Test endpoint tab scores against a real SageMaker endpoint (see endpoint.py).
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

MODE = os.environ.get("MLP_CONSOLE_MODE", "local").lower()
SEED_CANDIDATES = int(os.environ.get("MLP_CONSOLE_SEED_CANDIDATES", "2"))
STEP_PACE_S = float(os.environ.get("MLP_CONSOLE_STEP_PACE_S", "0.8"))
DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog  # noqa: E402
from auth import Auth, install  # noqa: E402
from endpoint import Endpoint, EndpointError  # noqa: E402
from github_ci import GitHubCI, GitHubError  # noqa: E402

AUTH = Auth()
MODELS = catalog.load()
GITHUB = GitHubCI()
FAILED = {"failure", "timed_out", "startup_failure"}
app = FastAPI(title="ML Platform lifecycle console",
              **({"docs_url": None, "redoc_url": None, "openapi_url": None} if AUTH.enabled else {}))
install(app, AUTH)
_lock = threading.Lock()
_state: dict[str, Any] = {}


@app.exception_handler(ClientError)
def aws_error(_request: Request, exc: ClientError) -> JSONResponse:
    err = exc.response.get("Error", {})
    return JSONResponse({"detail": f"AWS {err.get('Code', 'error')}: {err.get('Message', str(exc))}"}, status_code=502)


@app.exception_handler(BotoCoreError)
def aws_client_error(_request: Request, exc: BotoCoreError) -> JSONResponse:
    return JSONResponse({"detail": f"AWS: {exc}"}, status_code=502)


class Decision(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_package_arn: str
    decision: str
    canonical_hash: str = ""
    comment: str = ""


class Arn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_package_arn: str


class TrainRequest(BaseModel):
    shuffle_labels: bool = False


class ScoreRequest(BaseModel):
    applicants: list[dict[str, Any]]


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


def _ref(value: Any) -> Any:
    """Render pipeline expressions ({"Get": "Parameters.X"}, JsonGet) as readable text."""
    if isinstance(value, dict):
        if "Get" in value:
            return "${" + str(value["Get"]).replace("Parameters.", "") + "}"
        if "Std:JsonGet" in value:
            get = value["Std:JsonGet"]
            return f"{get['PropertyFile']['Get'].split('.')[-1]}{get['Path'].lstrip('$')}"
        if "Std:Join" in value:
            return value["Std:Join"].get("On", "").join(str(_ref(v)) for v in value["Std:Join"].get("Values", []))
    return value


_CONDITION_OPS = {"GreaterThanOrEqualTo": ">=", "GreaterThan": ">", "LessThanOrEqualTo": "<=", "LessThan": "<",
                  "Equals": "=="}


def _step_details(step: dict[str, Any]) -> dict[str, Any]:
    args = step.get("Arguments") or {}
    if step["Type"] == "Training":
        res = args.get("ResourceConfig") or {}
        return {"image": _ref((args.get("AlgorithmSpecification") or {}).get("TrainingImage")),
                "instance": f"{res.get('InstanceCount', 1)} × {_ref(res.get('InstanceType'))}",
                "max_runtime_s": (args.get("StoppingCondition") or {}).get("MaxRuntimeInSeconds"),
                "inputs": [c.get("ChannelName") for c in args.get("InputDataConfig") or []]}
    if step["Type"] == "Processing":
        res = (args.get("ProcessingResources") or {}).get("ClusterConfig") or {}
        app_spec = args.get("AppSpecification") or {}
        return {"image": _ref(app_spec.get("ImageUri")),
                "instance": f"{res.get('InstanceCount', 1)} × {_ref(res.get('InstanceType'))}",
                "entrypoint": " ".join(str(_ref(v)) for v in app_spec.get("ContainerEntrypoint") or []),
                "outputs": [o.get("OutputName") for o in (args.get("ProcessingOutputConfig") or {}).get("Outputs", [])]}
    if step["Type"] == "Condition":
        return {"conditions": [f"{_ref(c.get('LeftValue'))} {_CONDITION_OPS.get(c['Type'], c['Type'])} "
                               f"{_ref(c.get('RightValue'))}" for c in args.get("Conditions") or []],
                "if_steps": [s["Name"] for s in args.get("IfSteps") or []],
                "else_steps": [s["Name"] for s in args.get("ElseSteps") or []]}
    return {}


def _definition_view(definition: str) -> dict[str, Any]:
    doc = json.loads(definition)
    steps: list[dict[str, Any]] = []

    def visit(items: list[dict[str, Any]], parent: str | None, branch: str | None) -> None:
        for step in items:
            refs = set(re.findall(r"Steps\.([A-Za-z0-9_-]+)\.", json.dumps(step.get("Arguments") or {})))
            depends = sorted((refs | set(step.get("DependsOn") or [])) - {step["Name"]})
            if parent and parent not in depends:
                depends.append(parent)
            steps.append({"name": step["Name"], "type": step["Type"], "depends_on": depends,
                          "parent": parent, "branch": branch, "details": _step_details(step)})
            args = step.get("Arguments") or {}
            visit(args.get("IfSteps") or [], step["Name"], "if")
            visit(args.get("ElseSteps") or [], step["Name"], "else")

    visit(doc.get("Steps") or [], None, None)
    params = [{"name": p["Name"], "type": p.get("Type"), "default": p.get("DefaultValue")}
              for p in doc.get("Parameters") or []]
    return {"steps": steps, "parameters": params}


def _remote_runs(sm: Any, pipeline: str, limit: int = 10) -> list[dict[str, Any]]:
    summaries = sm.list_pipeline_executions(PipelineName=pipeline, SortBy="CreationTime", SortOrder="Descending",
                                            MaxResults=limit).get("PipelineExecutionSummaries", [])
    runs = []
    for s in summaries:
        arn = s["PipelineExecutionArn"]
        params = {p["Name"]: p["Value"] for p in
                  sm.list_pipeline_parameters_for_execution(PipelineExecutionArn=arn).get("PipelineParameters", [])}
        steps, registered = [], None
        for st in reversed(sm.list_pipeline_execution_steps(PipelineExecutionArn=arn).get("PipelineExecutionSteps", [])):
            start, end = st.get("StartTime"), st.get("EndTime")
            meta = st.get("Metadata") or {}
            outcome = (meta.get("Condition") or {}).get("Outcome")
            steps.append({"name": st["StepName"], "kind": "pipeline", "type": next(iter(meta), ""),
                          "status": st.get("StepStatus"), "started_at": str(start or ""),
                          "duration_ms": round((end - start).total_seconds() * 1000) if start and end else None,
                          "detail": st.get("FailureReason", ""), "outputs": {"outcome": outcome} if outcome else {}})
            registered = registered or (meta.get("RegisterModel") or {}).get("Arn")
        gate = next((s_["outputs"].get("outcome") for s_ in steps if s_["type"] == "Condition"), None)
        submitted_by = params.get("SubmittedBy") or _started_by(s.get("PipelineExecutionDescription"))
        runs.append({"run_id": arn, "execution_arn": arn, "commit": params.get("SourceCommit"), "submitted_by": submitted_by,
                     "status": s.get("PipelineExecutionStatus"), "started_at": str(s.get("StartTime", "")),
                     "ended_at": None, "gate": {"True": "passed", "False": "failed"}.get(gate or ""),
                     "parameters": params, "metrics": {}, "thresholds": {}, "steps": steps, "model_package_arn": registered})
    return runs


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
    try:
        from moto import mock_aws
    except ImportError as exc:
        raise RuntimeError("MLP_CONSOLE_MODE=local needs the model repo's dev requirements and scripts/; "
                           "the standalone image (ui/Dockerfile.standalone) runs MLP_CONSOLE_MODE=aws only") from exc

    from local_e2e import (APPROVER, DATA_SCIENTIST_GITHUB, SUBMITTER_GITHUB, LocalPlatform,
                           force_fake_credentials)

    force_fake_credentials()
    mock = mock_aws()
    mock.start()
    work = Path(tempfile.mkdtemp(prefix="mlp-console-"))
    lp = LocalPlatform(work)
    lp.bootstrap()
    submitters = [DATA_SCIENTIST_GITHUB, SUBMITTER_GITHUB]
    if SEED_CANDIDATES:
        lp.train_and_register(rows=3000, seed=99, submitted_by=DATA_SCIENTIST_GITHUB, shuffle_labels=True)
    for seed in range(1, SEED_CANDIDATES + 1):
        lp.train_and_register(rows=3000, seed=seed, submitted_by=submitters[(seed - 1) % len(submitters)])
    lp.step_pace_s = STEP_PACE_S
    desc = lp.sm.describe_pipeline(PipelineName=lp.cfg.pipeline_name)
    _state.update({"mock": mock, "lp": lp, "default_user": APPROVER, "active_run": None,
                   "pipeline_desc": desc, "pipeline_view": _definition_view(desc["PipelineDefinition"])})


def _snapshot(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy run records that a training thread may be updating."""
    for _ in range(5):
        try:
            return json.loads(json.dumps(runs, default=str))
        except RuntimeError:
            continue
    return []


def _train_in_background(lp: Any, run_id: str, submitted_by: str, shuffle: bool) -> None:
    try:
        with _lock:
            lp.train_and_register(rows=3000, seed=random.randint(10, 10_000), submitted_by=submitted_by,
                                  shuffle_labels=shuffle, run_id=run_id)
    except Exception as exc:  # recorded on the run as a Failed step
        print(f"training run {run_id} failed: {exc}", file=sys.stderr)
    finally:
        _state["active_run"] = None


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


def _pipeline_name() -> str:
    name = os.environ.get("MLP_PIPELINE_NAME")
    if not name:
        from ml_platform.config import ModelConfig

        name = ModelConfig.load(str(ROOT / "model.yaml")).pipeline_name
    return name


# ----- model catalog -----------------------------------------------------------------------
def _model(model_id: Optional[str], fallback: bool = False) -> catalog.Model:
    """Catalog entry for a request's ?model=; the first entry when omitted (or unknown, if fallback)."""
    if not model_id:
        return MODELS[0]
    found = next((m for m in MODELS if m.id == model_id), None)
    if found:
        return found
    if fallback:
        return MODELS[0]
    raise HTTPException(404, f"unknown model {model_id!r}")


def _endpoint(m: catalog.Model) -> Endpoint:
    cache = _state.setdefault("endpoints", {})
    if m.id not in cache:
        cache[m.id] = Endpoint(m.endpoint, m.feature_names, default_credentials=MODE == "aws")
    return cache[m.id]


def _own_package(pkg: dict[str, Any], m: catalog.Model) -> dict[str, Any]:
    """Refuse ARNs from other groups, so a model's routes only ever touch that model's registry."""
    if pkg.get("ModelPackageGroupName") != m.model_package_group:
        raise HTTPException(404, f"{pkg.get('ModelPackageArn')} is not a {m.name} package")
    return pkg


# ----- aws mode ----------------------------------------------------------------------------
def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def _login(request: Request) -> str:
    return getattr(request.state, "login", None) or "console"


def _roles() -> dict[str, list[str]]:
    """CONSOLE_ROLES: comma-separated user:role[+role] entries, e.g. alice:senior_data_scientist."""
    out: dict[str, list[str]] = {}
    for entry in filter(None, (e.strip() for e in os.environ.get("CONSOLE_ROLES", "").split(","))):
        name, _, roles = entry.partition(":")
        out[name.strip().lower()] = [r.strip() for r in roles.split("+") if r.strip()]
    return out


def _aws_identity(user: str) -> dict[str, Any]:
    """Unlisted users are viewers when the console requires a login; open local runs act as an approver."""
    roles = _roles().get(user.lower()) or (["viewer"] if AUTH.enabled else [APPROVER_ROLE])
    return {"email": user, "github_login": None, "roles": roles, "tenants": ["*"], "active": True, "in_tenant": True,
            "can_approve": APPROVER_ROLE in roles, "can_train": bool(TRAINER_ROLES & set(roles))}


def _started_by(description: Optional[str]) -> Optional[str]:
    match = re.search(r"lifecycle console by (\S+)$", description or "")
    return match.group(1).lower() if match else None


def _submitter(sm: Any, pkg: dict[str, Any], m: catalog.Model) -> Optional[str]:
    """Console user who started the run that registered pkg, "pipeline" for other runs, None if unknown."""
    meta = pkg.get("CustomerMetadataProperties") or {}
    if meta.get("submitted_by"):
        return meta["submitted_by"]
    match = re.search(r"pipeline execution (\S+)$", pkg.get("ModelPackageDescription") or "")
    if not match:
        return None
    cache = _state.setdefault("submitters", {})
    execution = match.group(1)
    if execution not in cache:
        account = pkg["ModelPackageArn"].split(":")[4]
        arn = f"arn:aws:sagemaker:{_region()}:{account}:pipeline/{m.pipeline}/execution/{execution}"
        try:
            description = sm.describe_pipeline_execution(PipelineExecutionArn=arn).get("PipelineExecutionDescription")
        except ClientError:
            return "pipeline"
        cache[execution] = _started_by(description) or "pipeline"
    return cache[execution]


def _serving_package(sm: Any, m: catalog.Model) -> Optional[str]:
    """Model package behind the model's endpoint, or None if it has no endpoint."""
    if not m.endpoint:
        return None
    try:
        config = sm.describe_endpoint(EndpointName=m.endpoint)["EndpointConfigName"]
        model = sm.describe_endpoint_config(EndpointConfigName=config)["ProductionVariants"][0]["ModelName"]
        desc = sm.describe_model(ModelName=model)
    except ClientError:
        return None
    container = desc.get("PrimaryContainer") or (desc.get("Containers") or [{}])[0]
    return container.get("ModelPackageName")


def _aws_package(sm: Any, s3: Any, pkg: dict[str, Any], serving: Optional[str], m: catalog.Model) -> dict[str, Any]:
    stats = ((pkg.get("ModelMetrics") or {}).get("ModelQuality") or {}).get("Statistics") or {}
    candidate = {"metrics": _read_json_s3(s3, stats.get("S3Uri", "")), "thresholds": m.thresholds}
    serving_now = serving == pkg["ModelPackageArn"]
    summary = _summarise(pkg, candidate, "Serving on endpoint" if serving_now else None)
    return {**summary, "model_id": m.id, "submitted_by": _submitter(sm, pkg, m), "platform_registered": True,
            "approved_outside_api": False, "serving": serving_now}


def _aws_history(pkg: dict[str, Any]) -> list[dict[str, Any]]:
    meta = pkg.get("CustomerMetadataProperties") or {}
    rows = [{"at": str(pkg.get("CreationTime", "")), "action": "REGISTERED",
             "actor": (pkg.get("CreatedBy") or {}).get("UserProfileName") or "pipeline",
             "comment": pkg.get("ModelPackageDescription")}]
    if pkg.get("ModelApprovalStatus") in ("Approved", "Rejected"):
        rows.append({"at": str(pkg.get("LastModifiedTime", "")), "action": pkg["ModelApprovalStatus"].upper(),
                     "actor": meta.get("approved_by") or meta.get("rejected_by") or "outside the console",
                     "comment": pkg.get("ApprovalDescription")})
    return rows


def _aws_deploy(sm: Any, pkg: dict[str, Any], m: catalog.Model) -> str:
    """Point the model's endpoint at an approved package; returns a message, never raises."""
    endpoint = m.endpoint
    if not endpoint:
        return f"{m.name} has no endpoint in the model catalog; nothing deployed."
    version = pkg.get("ModelPackageVersion")
    try:
        role = sm.describe_pipeline(PipelineName=m.pipeline)["RoleArn"]
        supported = (pkg.get("InferenceSpecification") or {}).get("SupportedRealtimeInferenceInstanceTypes") or []
        instance = supported[0] if supported else "ml.t2.medium"
        name = f"{endpoint}-v{version}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        sm.create_model(ModelName=name, ExecutionRoleArn=role, Containers=[{"ModelPackageName": pkg["ModelPackageArn"]}])
        sm.create_endpoint_config(EndpointConfigName=name, ProductionVariants=[{
            "VariantName": "AllTraffic", "ModelName": name, "InstanceType": instance, "InitialInstanceCount": 1}])
        try:
            sm.describe_endpoint(EndpointName=endpoint)
            sm.update_endpoint(EndpointName=endpoint, EndpointConfigName=name)
            return f"Endpoint {endpoint} is switching to v{version}; the current model serves until it is ready (about 5 minutes)."
        except ClientError as exc:
            if "Could not find endpoint" not in str(exc):
                raise
            sm.create_endpoint(EndpointName=endpoint, EndpointConfigName=name)
            return f"Creating endpoint {endpoint} with v{version} (about 5 minutes)."
    except (ClientError, BotoCoreError) as exc:
        return f"Approved, but the deploy to {endpoint} failed: {exc}"


def _aws_decide(req: Decision, user: str, m: catalog.Model) -> dict[str, Any]:
    if req.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    me = _aws_identity(user)
    if not me["can_approve"]:
        raise HTTPException(403, f"{user} ({', '.join(me['roles'])}) cannot approve or reject; that needs a senior data scientist")
    sm, s3 = _remote_clients()
    pkg = _own_package(sm.describe_model_package(ModelPackageName=req.model_package_arn), m)
    if pkg.get("ModelApprovalStatus") != "PendingManualApproval":
        raise HTTPException(409, f"v{pkg.get('ModelPackageVersion')} is already {pkg.get('ModelApprovalStatus')}")
    if _submitter(sm, pkg, m) == user.lower():
        raise HTTPException(403, f"separation of duties: {user} started the run that produced "
                                 f"v{pkg.get('ModelPackageVersion')}; another senior data scientist must decide")
    if req.decision == "approve":
        metrics = _aws_package(sm, s3, pkg, None, m)["metrics"]
        failing = [name for name, minimum in m.thresholds.items() if metrics.get(name, -1) < minimum]
        if failing:
            raise HTTPException(409, f"cannot approve: {', '.join(failing)} below threshold or missing")
    status = "Approved" if req.decision == "approve" else "Rejected"
    note = f"{status} by {user} in the lifecycle console" + (f": {req.comment.strip()}" if req.comment.strip() else "")
    sm.update_model_package(ModelPackageArn=req.model_package_arn, ModelApprovalStatus=status, ApprovalDescription=note[:1024],
                            CustomerMetadataProperties={("approved_by" if status == "Approved" else "rejected_by"): user})
    message = _aws_deploy(sm, pkg, m) if status == "Approved" else None
    return {"approval_status": status, "approval_id": uuid.uuid4().hex, "deploy_message": message,
            "deploy_parameter": _serving_package(sm, m)}


# ----- routes ------------------------------------------------------------------------------
@app.on_event("startup")
def startup() -> None:
    if MODE == "local":
        _start_local()


@app.get("/api/session")
def session(request: Request, model: Optional[str] = None,
            x_console_user: Optional[str] = Header(default=None)) -> dict[str, Any]:
    m = _model(model, fallback=True)
    catalog_view = {"models": [x.public() for x in MODELS], "model": m.id}
    if MODE == "aws":
        user = _login(request)
        me = _aws_identity(user)
        identities = [_aws_identity(u) for u in sorted(AUTH.users)] or [me]
        with _lock:
            sm, _ = _remote_clients()
            serving = _serving_package(sm, m)
        return {"mode": "aws", "user": user, "me": me, "group": m.model_package_group, "identities": identities,
                "deploy_parameter": serving, "region": _region(), "endpoint": m.endpoint, **catalog_view}
    if MODE == "local":
        lp = _local()
        identities = [_identity_view(i, lp.cfg.tenant_id) for i in lp.identities()]
        user = _local_user(x_console_user)
        me = next((i for i in identities if i["email"] == user), None)
        return {"mode": "local", "user": user, "me": me, "group": lp.cfg.model_package_group,
                "model_id": lp.cfg.model_id, "tenant_id": lp.cfg.tenant_id,
                "identities": sorted(identities, key=lambda i: (not i["can_approve"], i["email"])),
                "deploy_parameter": lp.deploy_parameter(), **catalog_view}
    return {"mode": "remote", "user": None, "me": None, "group": _group(), "identities": [], "deploy_parameter": None,
            **catalog_view}


@app.get("/api/packages")
def packages(model: Optional[str] = None) -> list[dict[str, Any]]:
    with _lock:
        if MODE == "local":
            lp = _local()
            return [_local_package(lp, p) for p in _list_packages(lp.sm, lp.cfg.model_package_group)]
        sm, s3 = _remote_clients()
        if MODE == "aws":
            m = _model(model)
            serving = _serving_package(sm, m)
            return [_aws_package(sm, s3, p, serving, m) for p in _list_packages(sm, m.model_package_group)]
        out = []
        for pkg in _list_packages(sm, _group()):
            meta = pkg.get("CustomerMetadataProperties") or {}
            out.append(_summarise(pkg, _read_json_s3(s3, meta.get("evidence_uri", "")), None))
        return out


@app.get("/api/package")
def package(arn: str, model: Optional[str] = None, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    with _lock:
        if MODE == "local":
            lp = _local()
            pkg = lp.sm.describe_model_package(ModelPackageName=arn)
            return {**_local_package(lp, pkg), "history": _local_history(lp, pkg)}
        sm, s3 = _remote_clients()
        pkg = sm.describe_model_package(ModelPackageName=arn)
        if MODE == "aws":
            m = _model(model)
            _own_package(pkg, m)
            return {**_aws_package(sm, s3, pkg, _serving_package(sm, m), m), "history": _aws_history(pkg)}
        meta = pkg.get("CustomerMetadataProperties") or {}
        summary = _summarise(pkg, _read_json_s3(s3, meta.get("evidence_uri", "")), None)
        if authorization:
            status, body = _remote_api("GET", "/approvals", authorization, query={"model_package_arn": arn})
            if status == 200:
                summary["lifecycle_state"] = body.get("lifecycle_state")
        return {**summary, "history": []}


@app.post("/api/decision")
def decision(req: Decision, request: Request, model: Optional[str] = None,
             x_console_user: Optional[str] = Header(default=None),
             authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    with _lock:
        if MODE == "aws":
            return _aws_decide(req, _login(request), _model(model))
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


@app.get("/api/pipeline")
def pipeline(model: Optional[str] = None) -> dict[str, Any]:
    if MODE == "local":
        # No _lock: polled while a training thread holds it; reads only the cached definition and run copies.
        lp = _local()
        desc = _state["pipeline_desc"]
        return {"name": lp.cfg.pipeline_name, "arn": desc.get("PipelineArn"), "role_arn": desc.get("RoleArn"),
                **_state["pipeline_view"], "runs": _snapshot(lp.runs), "active_run_id": _state["active_run"]}
    with _lock:
        sm, _ = _remote_clients()
        name = _model(model).pipeline if MODE == "aws" else _pipeline_name()
        runs = _remote_runs(sm, name)
        desc = sm.describe_pipeline(PipelineName=name)
        active = next((r["run_id"] for r in runs if r["status"] == "Executing"), None)
        return {"name": name, "arn": desc.get("PipelineArn"), "role_arn": desc.get("RoleArn"),
                **_definition_view(desc["PipelineDefinition"]), "runs": runs, "active_run_id": active}


@app.post("/api/train")
def train(request: Request, model: Optional[str] = None) -> dict[str, Any]:
    """Start the model's real pipeline (aws mode); follow it with GET /api/pipeline."""
    if MODE != "aws":
        raise HTTPException(404, "aws mode only")
    me = _aws_identity(_login(request))
    if not me["can_train"]:
        raise HTTPException(403, f"{me['email']} ({', '.join(me['roles'])}) cannot start training; that needs a data scientist role")
    m = _model(model)
    with _lock:
        sm, _ = _remote_clients()
        name = m.pipeline
        running = sm.list_pipeline_executions(PipelineName=name, SortBy="CreationTime", SortOrder="Descending",
                                              MaxResults=5).get("PipelineExecutionSummaries", [])
        if any(r.get("PipelineExecutionStatus") == "Executing" for r in running):
            raise HTTPException(409, "a training run is already in progress")
        user = re.sub(r"[^a-zA-Z0-9]+", "-", _login(request)).strip("-")[:40] or "console"
        arn = sm.start_pipeline_execution(
            PipelineName=name, PipelineExecutionDisplayName=f"console-{user}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}",
            PipelineExecutionDescription=f"Started from the lifecycle console by {_login(request)}",
        )["PipelineExecutionArn"]
    return {"run_id": arn}


@app.post("/api/simulate/train")
def simulate_train(req: Optional[TrainRequest] = None,
                   x_console_user: Optional[str] = Header(default=None)) -> dict[str, Any]:
    if MODE != "local":
        raise HTTPException(404, "local mode only")
    """Start a run in the background; follow it with GET /api/pipeline (run_id, active_run_id)."""
    shuffle = bool(req and req.shuffle_labels)
    with _lock:
        lp = _local()
        user = _local_user(x_console_user)
        item = next((i for i in lp.identities() if i["pk"] == user), None)
        if not item or not _identity_view(item, lp.cfg.tenant_id)["can_train"]:
            raise HTTPException(403, f"{user} cannot start training for tenant {lp.cfg.tenant_id}")
        if _state["active_run"]:
            raise HTTPException(409, "a training run is already in progress")
        run_id = uuid.uuid4().hex
        _state["active_run"] = run_id
    threading.Thread(target=_train_in_background, daemon=True,
                     args=(lp, run_id, item.get("github_login") or user, shuffle)).start()
    return {"run_id": run_id}


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


@app.get("/api/endpoint")
def endpoint_status(model: Optional[str] = None) -> dict[str, Any]:
    """Real SageMaker endpoint in every mode; local mode mocks only the lifecycle, not this."""
    m = _model(model)
    form = {"model": m.id, "features": m.features, "presets": m.presets, "outcome": m.outcome}
    ep = _endpoint(m)
    if not ep.configured:
        return {"configured": False, "name": m.endpoint, **form}
    try:
        return {"configured": True, **form, **ep.status()}
    except EndpointError as exc:
        raise HTTPException(502, f"SageMaker: {exc}")


@app.post("/api/endpoint/score")
def endpoint_score(req: ScoreRequest, model: Optional[str] = None) -> dict[str, Any]:
    ep = _endpoint(_model(model))
    if not ep.configured:
        raise HTTPException(404, "no endpoint configured")
    try:
        return ep.score(req.applicants)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except EndpointError as exc:
        raise HTTPException(502, f"SageMaker: {exc}")


def _github_error(exc: GitHubError) -> HTTPException:
    return HTTPException(409 if exc.status == 401 else 502, f"GitHub: {exc}")


@app.get("/api/github")
def github_runs(model: Optional[str] = None) -> dict[str, Any]:
    """Workflow runs of the model's repo; "alert" is the newest finished run when it failed."""
    m = _model(model)
    if not m.github:
        return {"configured": False, "model": m.id}
    gh = m.github
    base = {"configured": True, "model": m.id, "repo": gh["repo"], "workflow": gh["workflow"], "branch": gh["branch"],
            "authenticated": GITHUB.authenticated,
            "url": f"https://github.com/{gh['repo']}/actions/workflows/{gh['workflow']}"}
    try:
        runs = GITHUB.runs(gh["repo"], gh["workflow"], gh["branch"] or None)
        latest = next((r for r in runs if r["status"] == "completed"), None)
        alert = None
        if latest and latest["conclusion"] in FAILED:
            failed_jobs = [j for j in GITHUB.jobs(gh["repo"], latest["id"], True) if j["conclusion"] in FAILED]
            alert = {"run": latest, "jobs": failed_jobs}
    except GitHubError as exc:
        raise _github_error(exc)
    return {**base, "runs": runs, "alert": alert, "active": any(r["status"] != "completed" for r in runs)}


@app.get("/api/github/run")
def github_run(run_id: int, model: Optional[str] = None) -> dict[str, Any]:
    m = _model(model)
    if not m.github:
        raise HTTPException(404, f"{m.name} has no GitHub repository in the model catalog")
    try:
        run = next((r for r in GITHUB.runs(m.github["repo"], m.github["workflow"], m.github["branch"] or None)
                    if r["id"] == run_id), None)
        if not run:
            raise HTTPException(404, f"run {run_id} is not one of {m.name}'s recent runs")
        return {"run": run, "jobs": GITHUB.jobs(m.github["repo"], run_id, run["status"] == "completed")}
    except GitHubError as exc:
        raise _github_error(exc)


@app.get("/api/github/log")
def github_log(job_id: int, model: Optional[str] = None) -> dict[str, Any]:
    """Error lines of a job's log; the path is built from the catalog repo, so only that repo is readable."""
    m = _model(model)
    if not m.github:
        raise HTTPException(404, f"{m.name} has no GitHub repository in the model catalog")
    try:
        return GITHUB.failure_log(m.github["repo"], job_id)
    except GitHubError as exc:
        raise _github_error(exc)


if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        return FileResponse(DIST / "index.html")
