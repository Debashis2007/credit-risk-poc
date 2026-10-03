#!/usr/bin/env python3
"""Preprocess credit-risk dataset into train/validation CSVs with target column."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline as SkPipeline
from sklearn.preprocessing import OneHotEncoder


def preprocess(df: pd.DataFrame, target: str):
    if target not in df.columns:
        raise ValueError(f"Expected column '{target}' in input data")
    y = df[target]
    X = df.drop(columns=[target])
    cat_cols = X.select_dtypes(include=["object", "category"]).columns.tolist()
    num_cols = [c for c in X.columns if c not in cat_cols]
    try:
        encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:  # sklearn < 1.2
        encoder = OneHotEncoder(handle_unknown="ignore", sparse=False)
    transformer = ColumnTransformer(
        transformers=[
            ("cat", encoder, cat_cols),
            ("num", "passthrough", num_cols),
        ]
    )
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y if y.nunique() > 1 else None
    )
    pipe = SkPipeline([("prep", transformer)])
    X_train_t = pipe.fit_transform(X_train)
    X_val_t = pipe.transform(X_val)

    feature_names = [f"f{i}" for i in range(X_train_t.shape[1])]
    train_df = pd.DataFrame(X_train_t, columns=feature_names)
    train_df[target] = y_train.reset_index(drop=True)
    val_df = pd.DataFrame(X_val_t, columns=feature_names)
    val_df[target] = y_val.reset_index(drop=True)
    return pipe, train_df, val_df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", default="/opt/ml/processing/input")
    parser.add_argument("--output-dir", default="/opt/ml/processing/output")
    parser.add_argument("--target-column", default=os.environ.get("TARGET_COLUMN", "default"))
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    train_dir = output_dir / "train"
    val_dir = output_dir / "validation"
    train_dir.mkdir(parents=True, exist_ok=True)
    val_dir.mkdir(parents=True, exist_ok=True)

    csv_files = sorted(input_dir.rglob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV in {input_dir}")
    df = pd.read_csv(csv_files[0])
    pipe, train_df, val_df = preprocess(df, args.target_column)

    joblib.dump(pipe, output_dir / "preprocessor.joblib")
    train_df.to_csv(train_dir / "train.csv", index=False)
    val_df.to_csv(val_dir / "validation.csv", index=False)
    print(json.dumps({"rows": len(df), "train": len(train_df), "validation": len(val_df)}))


if __name__ == "__main__":
    main()
