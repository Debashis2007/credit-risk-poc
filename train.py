#!/usr/bin/env python3
"""Train credit-risk classifier (sklearn)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default=os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    parser.add_argument("--train", default=os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"))
    parser.add_argument("--model-name", default=os.environ.get("MODEL_NAME", "credit-risk"))
    parser.add_argument("--target-column", default=os.environ.get("TARGET_COLUMN", "default"))
    args, _unknown = parser.parse_known_args()

    train_path = Path(args.train)
    csv_files = sorted(train_path.rglob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No training CSV in {train_path}")
    df = pd.read_csv(csv_files[0])
    target = args.target_column
    if target not in df.columns:
        raise ValueError(f"Target column {target!r} missing from {csv_files[0]}")

    X = df.drop(columns=[target])
    y = df[target]
    # Hold-out for train-time diagnostics only; gate metric comes from Evaluate on validation.
    X_fit, X_hold, y_fit, y_hold = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y if y.nunique() > 1 else None
    )
    model = RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1)
    model.fit(X_fit, y_fit)
    preds = model.predict_proba(X_hold)[:, 1]
    auc = float(roc_auc_score(y_hold, preds))

    model_dir = Path(args.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_dir / "model.joblib")
    meta = {
        "auc_holdout": auc,
        "model_name": args.model_name,
        "target_column": target,
        "feature_columns": list(X.columns),
    }
    (model_dir / "metrics.json").write_text(json.dumps(meta), encoding="utf-8")
    print(json.dumps({"auc_holdout": auc, "model_name": args.model_name}))


if __name__ == "__main__":
    main()
