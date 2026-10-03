#!/usr/bin/env python3
"""Generate a synthetic, numeric credit-risk dataset with a learnable `default` target.

Usage:
    python scripts/make_synthetic_data.py --rows 5000 --out data/train/train.csv
    aws s3 cp data/train/train.csv s3://<data-bucket>/credit-risk/train/train.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def make_dataset(rows: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    income = rng.lognormal(mean=10.8, sigma=0.5, size=rows)
    debt_to_income = np.clip(rng.normal(0.35, 0.15, rows), 0.0, 1.5)
    credit_utilization = np.clip(rng.beta(2, 5, rows), 0.0, 1.0)
    late_payments_12m = rng.poisson(0.6, rows)
    credit_history_years = np.clip(rng.normal(9, 5, rows), 0, 40)
    loan_amount = rng.lognormal(mean=9.5, sigma=0.7, size=rows)
    open_accounts = rng.integers(1, 20, rows)

    logit = (
        -4.5
        + 6.0 * debt_to_income
        + 5.0 * credit_utilization
        + 1.0 * late_payments_12m
        - 0.10 * credit_history_years
        + 0.6 * np.log(loan_amount / income)
        + rng.normal(0, 0.3, rows)
    )
    default = (rng.random(rows) < 1 / (1 + np.exp(-logit))).astype(int)

    return pd.DataFrame(
        {
            "income": income.round(2),
            "debt_to_income": debt_to_income.round(4),
            "credit_utilization": credit_utilization.round(4),
            "late_payments_12m": late_payments_12m,
            "credit_history_years": credit_history_years.round(1),
            "loan_amount": loan_amount.round(2),
            "open_accounts": open_accounts,
            "default": default,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="data/train/train.csv")
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = make_dataset(args.rows, args.seed)
    df.to_csv(out, index=False)
    print(f"wrote {len(df)} rows to {out} (default rate {df['default'].mean():.2%})")


if __name__ == "__main__":
    main()
