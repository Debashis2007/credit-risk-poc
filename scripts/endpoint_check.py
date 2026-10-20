"""Post-deploy check for a model endpoint: served revision and test predictions.

    python scripts/endpoint_check.py --endpoint credit-risk-qa --package-arn <arn> \
        [--model credit-risk] [--higher "Risky applicant" --lower "Safe applicant"]

Fails (exit 1) unless the endpoint is InService, serves exactly the given model package (a
healthy status says nothing about which model is serving), and every catalog preset scores
with a 0/1 prediction and a score in [0, 1]. With --higher/--lower, the first preset must
also score above the second.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui" / "backend"))
import catalog  # noqa: E402


def served_package(sm: Any, endpoint: str) -> tuple[str, Optional[str]]:
    """Endpoint status and the model package behind its current configuration."""
    desc = sm.describe_endpoint(EndpointName=endpoint)
    config = sm.describe_endpoint_config(EndpointConfigName=desc["EndpointConfigName"])
    model = sm.describe_model(ModelName=config["ProductionVariants"][0]["ModelName"])
    container = model.get("PrimaryContainer") or (model.get("Containers") or [{}])[0]
    return desc["EndpointStatus"], container.get("ModelPackageName")


def score(runtime: Any, endpoint: str, rows: list[list[float]]) -> dict[str, Any]:
    resp = runtime.invoke_endpoint(EndpointName=endpoint, ContentType="application/json", Accept="application/json",
                                   Body=json.dumps({"features": rows}).encode())
    return json.loads(resp["Body"].read())


def check(sm: Any, runtime: Any, endpoint: str, package_arn: str, model: catalog.Model,
          higher: Optional[str] = None, lower: Optional[str] = None) -> list[str]:
    """Problems found; an empty list means the release is good."""
    status, serving = served_package(sm, endpoint)
    if status != "InService":
        return [f"{endpoint} is {status}, not InService"]
    if serving != package_arn:
        return [f"{endpoint} serves {serving}, not {package_arn}"]
    if not model.presets:
        return [f"model {model.id} has no presets to score"]
    names = list(model.presets)
    rows = [[model.presets[n][f] for f in model.feature_names] for n in names]
    body = score(runtime, endpoint, rows)
    predictions, scores = body.get("prediction") or [], body.get("score") or []
    problems = []
    if len(predictions) != len(rows) or len(scores) != len(rows):
        return [f"expected {len(rows)} predictions and scores, got {body}"]
    for name, pred, s in zip(names, predictions, scores):
        if pred not in (0, 1):
            problems.append(f"{name}: prediction {pred!r} is not 0 or 1")
        if not isinstance(s, (int, float)) or not 0 <= s <= 1:
            problems.append(f"{name}: score {s!r} is not in [0, 1]")
    if higher and lower and not problems:
        by_name = dict(zip(names, scores))
        missing = [n for n in (higher, lower) if n not in by_name]
        if missing:
            problems.append(f"no preset named {', '.join(missing)}")
        elif by_name[higher] <= by_name[lower]:
            problems.append(f"{higher} scored {by_name[higher]:.3f}, not above {lower} at {by_name[lower]:.3f}")
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--package-arn", required=True)
    parser.add_argument("--model", default="credit-risk", help="model id in the catalog (ui/backend/models.json)")
    parser.add_argument("--higher")
    parser.add_argument("--lower")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    if bool(args.higher) != bool(args.lower):
        parser.error("--higher and --lower go together")

    import boto3

    model = next((m for m in catalog.load() if m.id == args.model), None)
    if model is None:
        parser.error(f"model {args.model!r} is not in the catalog")
    problems = check(boto3.client("sagemaker", region_name=args.region),
                     boto3.client("sagemaker-runtime", region_name=args.region),
                     args.endpoint, args.package_arn, model, args.higher, args.lower)
    for p in problems:
        print(f"FAIL {p}")
    if not problems:
        print(f"OK {args.endpoint} serves {args.package_arn} and scores {len(model.presets)} presets as expected")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
