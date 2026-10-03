"""Full v7 lifecycle against mocked AWS (moto), run in a subprocess to isolate env changes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("moto")
pytest.importorskip("ml_platform.config")


def test_local_lifecycle_against_mocked_aws():
    result = subprocess.run(
        [sys.executable, "scripts/local_e2e.py", "--rows", "2000"],
        cwd=ROOT, capture_output=True, text=True, env={**os.environ}, timeout=300,
    )
    out = result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pipeline   demo-credit-risk: Train -> Evaluate -> CheckMetric -> [PublishCandidate]" in out
    assert "status     PendingManualApproval" in out
    assert "idempotent=True" in out
    assert "submitter approves own model   -> 403" in out
    assert "approver with wrong hash       -> 409" in out
    assert "senior DS approves, right hash -> 200" in out
    assert "APPROVAL_CONFIRMED; SSM deploy parameter set: True" in out
    assert "APPROVAL_VIOLATION; deploy parameter unchanged: True" in out
    assert "lifecycle  state=APPROVED" in out
