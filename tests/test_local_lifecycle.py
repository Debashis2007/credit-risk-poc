"""Local smoke test: synthetic data -> train.py -> evaluate.py -> model.yaml thresholds."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_synthetic_data import make_dataset  # noqa: E402


def _run(script: str, *args: str) -> None:
    subprocess.run([sys.executable, str(ROOT / script), *args], check=True, cwd=ROOT)


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    work = tmp_path_factory.mktemp("lifecycle")
    train_dir, eval_dir = work / "train", work / "eval"
    train_dir.mkdir()
    eval_dir.mkdir()
    make_dataset(4000, seed=1).to_csv(train_dir / "train.csv", index=False)
    make_dataset(1000, seed=2).to_csv(eval_dir / "validation.csv", index=False)

    model_dir, out_dir = work / "model", work / "out"
    _run("train.py", "--train", str(train_dir), "--model-dir", str(model_dir))
    _run(
        "evaluate.py",
        "--model-dir", str(model_dir),
        "--input-dir", str(eval_dir),
        "--output-dir", str(out_dir),
    )
    return json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))


def test_evaluate_reports_every_configured_metric(evaluated):
    cfg = yaml.safe_load((ROOT / "model.yaml").read_text(encoding="utf-8"))
    for metric in cfg["evaluation"]["metrics"]:
        assert metric in evaluated


def test_synthetic_model_clears_all_thresholds(evaluated):
    cfg = yaml.safe_load((ROOT / "model.yaml").read_text(encoding="utf-8"))
    for metric, minimum in cfg["evaluation"]["thresholds"].items():
        assert evaluated[metric] >= minimum, f"{metric}={evaluated[metric]} < {minimum}"


def test_model_yaml_valid_against_platform():
    config = pytest.importorskip("ml_platform.config")
    cfg = config.ModelConfig.load(str(ROOT / "model.yaml"))
    assert cfg.model_id == "credit-risk"
    assert cfg.training_target == "control_plane"
    assert cfg.pipeline_name == "demo-credit-risk"
