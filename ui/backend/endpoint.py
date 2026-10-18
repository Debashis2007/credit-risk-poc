"""Live SageMaker endpoint for the console's Test endpoint tab: status and scoring.

The endpoint name and feature order come from the model catalog (catalog.py). Credentials, per deployment:
  MLP_ENDPOINT_REGION                 default us-east-1
  MLP_ENDPOINT_AWS_ACCESS_KEY_ID      key limited to Describe* and InvokeEndpoint on the catalog's endpoints
  MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY

Requests are signed here and sent with urllib instead of boto3: in local mode moto intercepts every
botocore client and replaces AWS_* credentials with fakes, which would keep calls off the real account.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

MAX_APPLICANTS = 20


class EndpointError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Endpoint:
    def __init__(self, name: Optional[str], features: list[str], env: Optional[dict[str, str]] = None):
        env = dict(os.environ if env is None else env)
        self.name = (name or "").strip()
        self.features = list(features)
        self.region = env.get("MLP_ENDPOINT_REGION", "").strip() or "us-east-1"
        self._key = env.get("MLP_ENDPOINT_AWS_ACCESS_KEY_ID", "").strip()
        self._secret = env.get("MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY", "").strip()
        self.configured = bool(self.name and self._key and self._secret)

    def _send(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        signed = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(Credentials(self._key, self._secret), "sagemaker", self.region).add_auth(signed)
        req = urllib.request.Request(url, data=body, method="POST", headers=dict(signed.headers.items()))
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise EndpointError(502, f"cannot reach SageMaker: {exc.reason}") from exc

    def _api(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._send(f"https://api.sagemaker.{self.region}.amazonaws.com/",
                                  json.dumps(payload).encode(),
                                  {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": f"SageMaker.{action}"})
        data = json.loads(body or b"{}")
        if status != 200:
            raise EndpointError(status, data.get("message") or data.get("Message") or f"{action} failed ({status})")
        return data

    def status(self) -> dict[str, Any]:
        try:
            ep = self._api("DescribeEndpoint", {"EndpointName": self.name})
        except EndpointError as exc:
            if "Could not find endpoint" in str(exc):
                return {"name": self.name, "region": self.region, "status": "NotDeployed"}
            raise
        out: dict[str, Any] = {
            "name": self.name, "region": self.region, "status": ep.get("EndpointStatus"),
            "failure_reason": ep.get("FailureReason"), "endpoint_config": ep.get("EndpointConfigName"),
            "updated_at": _iso(ep.get("LastModifiedTime")),
        }
        variants = self._api("DescribeEndpointConfig", {"EndpointConfigName": ep["EndpointConfigName"]})["ProductionVariants"]
        if variants:
            out.update(model_name=variants[0].get("ModelName"), instance_type=variants[0].get("InstanceType"),
                       instance_count=variants[0].get("InitialInstanceCount"))
            model = self._api("DescribeModel", {"ModelName": variants[0]["ModelName"]})
            container = model.get("PrimaryContainer") or (model.get("Containers") or [{}])[0]
            package = container.get("ModelPackageName")
            out.update(model_package_arn=package, version=_version(package))
        return out

    def score(self, applicants: list[dict[str, Any]]) -> dict[str, Any]:
        if not applicants or len(applicants) > MAX_APPLICANTS:
            raise ValueError(f"send between 1 and {MAX_APPLICANTS} applicants")
        rows = [[_number(a, f, i) for f in self.features] for i, a in enumerate(applicants)]
        started = time.perf_counter()
        status, body = self._send(
            f"https://runtime.sagemaker.{self.region}.amazonaws.com/endpoints/{urllib.parse.quote(self.name, safe='')}/invocations",
            json.dumps({"features": rows}).encode(), {"Content-Type": "application/json", "Accept": "application/json"})
        latency_ms = round((time.perf_counter() - started) * 1000)
        data = json.loads(body or b"{}")
        if status != 200:
            raise EndpointError(status, data.get("message") or data.get("Message") or f"InvokeEndpoint failed ({status})")
        scores = data.get("score") or [None] * len(rows)
        return {"endpoint": self.name, "model_version": data.get("model_version"), "latency_ms": latency_ms,
                "results": [{"prediction": p, "score": s} for p, s in zip(data.get("prediction") or [], scores)]}


def _number(applicant: dict[str, Any], field: str, index: int) -> float:
    try:
        value = float(applicant[field])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"applicant {index + 1}: {field} must be a number") from None
    if not math.isfinite(value):
        raise ValueError(f"applicant {index + 1}: {field} must be a finite number")
    return value


def _version(package_arn: Optional[str]) -> Optional[int]:
    tail = (package_arn or "").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _iso(value: Any) -> Optional[str]:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if isinstance(value, (int, float)) else value
