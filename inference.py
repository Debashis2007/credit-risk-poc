"""SageMaker inference entrypoint for credit-risk sklearn models."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import joblib
import numpy as np

_MODEL = None
_MODEL_NAME = os.environ.get("MODEL_NAME", "credit-risk")


def model_fn(model_dir: str):
    global _MODEL
    path = Path(model_dir) / "model.joblib"
    if not path.is_file():
        matches = list(Path(model_dir).rglob("model.joblib"))
        if not matches:
            raise FileNotFoundError(f"model.joblib not found in {model_dir}")
        path = matches[0]
    _MODEL = joblib.load(path)
    return _MODEL


def input_fn(request_body: str, content_type: str = "application/json"):
    if content_type and "json" not in content_type:
        raise ValueError(f"Unsupported content type: {content_type}")
    payload = json.loads(request_body)
    if isinstance(payload, dict) and "features" in payload:
        payload = payload["features"]
    return np.asarray(payload, dtype=float)


def predict_fn(input_data: Any, model: Any):
    arr = np.atleast_2d(input_data)
    if hasattr(model, "predict_proba"):
        scores = model.predict_proba(arr)[:, 1]
        preds = (scores >= 0.5).astype(int)
        return {"prediction": preds.tolist(), "score": scores.tolist()}
    preds = model.predict(arr)
    return {"prediction": np.asarray(preds).tolist(), "score": None}


def output_fn(prediction: dict[str, Any], accept: str = "application/json") -> str:
    body = {
        "prediction": prediction.get("prediction"),
        "score": prediction.get("score"),
        "model_version": os.environ.get("MODEL_VERSION") or _MODEL_NAME,
    }
    return json.dumps(body)
