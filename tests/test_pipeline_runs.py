"""Pipeline run records shown in the console, against mocked AWS (subprocess isolates env changes)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("moto")
pytest.importorskip("ml_platform.config")

SCRIPT = """
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "scripts")
from local_e2e import LocalPlatform, force_fake_credentials
from moto import mock_aws

force_fake_credentials()
with mock_aws():
    lp = LocalPlatform(Path(tempfile.mkdtemp()))
    lp.bootstrap()
    lp.train_and_register(rows=2000, seed=1)
    lp.train_and_register(rows=2000, seed=99, shuffle_labels=True)
    definition = json.loads(lp.sm.describe_pipeline(PipelineName=lp.cfg.pipeline_name)["PipelineDefinition"])
    print(json.dumps({"steps": [s["Name"] for s in definition["Steps"]], "runs": lp.runs}))
"""


@pytest.fixture(scope="module")
def result():
    proc = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ}, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _steps(run):
    return {s["name"]: s for s in run["steps"]}


def test_registered_pipeline_is_the_platform_definition(result):
    assert result["steps"] == ["Train", "Evaluate", "CheckMetric"]


def test_passing_run_records_every_step_and_the_package(result):
    run = result["runs"][1]
    steps = _steps(run)
    assert run["status"] == "Succeeded" and run["gate"] == "passed" and run["run_id"]
    assert list(steps) == ["BuildImage", "UploadData", "Train", "Evaluate", "CheckMetric", "PublishCandidate",
                           "RegisterCandidate"]
    assert all(s["status"] == "Succeeded" for s in steps.values())
    assert steps["CheckMetric"]["outputs"]["outcome"] == "True"
    assert run["model_package_arn"] == steps["RegisterCandidate"]["outputs"]["model_package_arn"]


def test_shuffled_labels_fail_the_gate_and_register_nothing(result):
    run = result["runs"][0]
    steps = _steps(run)
    assert run["shuffle_labels"] and run["gate"] == "failed"
    assert steps["CheckMetric"]["outputs"]["outcome"] == "False"
    assert steps["PublishCandidate"]["status"] == "NotExecuted"
    assert steps["RegisterCandidate"]["status"] == "Skipped"
    assert run["model_package_arn"] is None
