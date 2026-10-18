"""Model catalog loading and validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui" / "backend"))

import catalog  # noqa: E402

ENTRY = {"id": "churn", "model_package_group": "ModelChurn", "pipeline": "churn", "features": ["a", "b"]}


def load(*entries):
    return catalog.load({"MLP_MODELS": json.dumps(list(entries))})


def test_default_catalog_is_the_credit_risk_poc():
    [m] = catalog.load({})
    assert (m.id, m.model_package_group, m.pipeline, m.endpoint) == ("credit-risk", "ModelCreditRisk", "credit-risk", "credit-risk-qa")
    assert m.thresholds == {"auc": 0.75}
    assert m.feature_names[0] == "income" and len(m.feature_names) == 7
    assert set(m.presets) == {"Risky applicant", "Safe applicant"}
    assert m.github == {"repo": "Debashis2007/credit-risk-poc", "workflow": "ml-lifecycle.yml", "branch": ""}


def test_minimal_entry_gets_defaults():
    [m] = load(ENTRY)
    assert m.name == "churn" and m.endpoint is None and m.thresholds == {}
    assert m.features == [{"name": "a", "label": "a"}, {"name": "b", "label": "b"}]
    assert m.outcome == catalog.DEFAULT_OUTCOME


def test_file_source(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps([ENTRY]))
    assert catalog.load({"MLP_MODELS_FILE": str(path)})[0].id == "churn"


@pytest.mark.parametrize("entries, message", [
    ([], "empty"),
    ([{**ENTRY, "pipeline": ""}], "missing pipeline"),
    ([{**ENTRY, "id": "Churn Model"}], "lowercase"),
    ([ENTRY, ENTRY], "unique"),
    ([{**ENTRY, "presets": {"x": {"a": 1}}}], "exactly the features"),
    ([{**ENTRY, "github": {"repo": "not a repo", "workflow": "ci.yml"}}], "owner/name"),
    ([{**ENTRY, "github": {"repo": "acme/churn", "workflow": "../../etc"}}], "workflow file name"),
])
def test_invalid_catalogs_fail_at_startup(entries, message):
    with pytest.raises(ValueError, match=message):
        load(*entries)
