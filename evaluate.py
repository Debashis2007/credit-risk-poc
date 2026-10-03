#!/usr/bin/env python3
"""Evaluate trained model; write metrics.json for conditional register gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score, roc_auc_score


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="/opt/ml/processing/model")
    parser.add_argument("--input-dir", default="/opt/ml/processing/input")
    parser.add_argument("--output-dir", default="/opt/ml/processing/output")
    parser.add_argument("--target-column", default=os.environ.get("TARGET_COLUMN", "default"))
    parser.add_argument("--model-name", default=os.environ.get("MODEL_NAME", "credit-risk"))
    args, _unknown = parser.parse_known_args()

    model_dir = Path(args.model_dir)
    model_path = model_dir / "model.joblib"
    if not model_path.is_file():
        # Training artifacts often arrive as model.tar.gz extracted contents.
        candidates = list(model_dir.rglob("model.joblib"))
        if not candidates:
            raise FileNotFoundError(f"model.joblib not found under {model_dir}")
        model_path = candidates[0]
    model = joblib.load(model_path)

    input_dir = Path(args.input_dir)
    csv_files = sorted(input_dir.rglob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No evaluation CSV in {input_dir}")
    df = pd.read_csv(csv_files[0])
    target = args.target_column
    if target not in df.columns:
        raise ValueError(f"Target column {target!r} missing from {csv_files[0]}")

    X = df.drop(columns=[target])
    y = df[target]
    proba = model.predict_proba(X)[:, 1]
    metrics = {
        "auc": float(roc_auc_score(y, proba)),
        "accuracy": float(accuracy_score(y, (proba >= 0.5).astype(int))),
        "model_name": args.model_name,
        "rows": int(len(df)),
    }

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
