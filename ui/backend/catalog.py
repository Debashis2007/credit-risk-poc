"""Model catalog: the models the console manages and the AWS resources behind each one.

Source, first match wins:
  MLP_MODELS       JSON array of model entries
  MLP_MODELS_FILE  path to a JSON file with the same array
  models.json      next to this module (the credit-risk POC entry)

Entry fields: id, name, model_package_group, pipeline, endpoint (optional), thresholds
({metric: minimum}), features ([{name, label, hint, step}], in the order the model expects),
presets ({label: {feature: value}}), outcome ({positive, negative, score}).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_FILE = Path(__file__).resolve().parent / "models.json"
DEFAULT_OUTCOME = {"positive": "Positive", "negative": "Negative", "score": "Score"}


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    model_package_group: str
    pipeline: str
    endpoint: Optional[str]
    thresholds: dict[str, float]
    features: list[dict[str, Any]]
    presets: dict[str, dict[str, float]] = field(default_factory=dict)
    outcome: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_OUTCOME))

    @property
    def feature_names(self) -> list[str]:
        return [f["name"] for f in self.features]

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "group": self.model_package_group,
                "pipeline": self.pipeline, "endpoint": self.endpoint}


def _model(entry: dict[str, Any]) -> Model:
    missing = [k for k in ("id", "model_package_group", "pipeline", "features") if not entry.get(k)]
    if missing:
        raise ValueError(f"model entry {entry.get('id', '?')!r} is missing {', '.join(missing)}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", entry["id"]):
        raise ValueError(f"model id {entry['id']!r} must be lowercase letters, digits and hyphens")
    features = [{"name": f, "label": f} if isinstance(f, str) else
                {"name": f["name"], "label": f.get("label") or f["name"], "hint": f.get("hint", ""),
                 "step": str(f.get("step", "any"))} for f in entry["features"]]
    names = [f["name"] for f in features]
    for label, values in (entry.get("presets") or {}).items():
        if sorted(values) != sorted(names):
            raise ValueError(f"model {entry['id']!r} preset {label!r} must set exactly the features {names}")
    return Model(
        id=entry["id"], name=entry.get("name") or entry["id"], model_package_group=entry["model_package_group"],
        pipeline=entry["pipeline"], endpoint=entry.get("endpoint") or None,
        thresholds={k: float(v) for k, v in (entry.get("thresholds") or {}).items()}, features=features,
        presets={k: {f: float(v) for f, v in p.items()} for k, p in (entry.get("presets") or {}).items()},
        outcome={**DEFAULT_OUTCOME, **(entry.get("outcome") or {})},
    )


def load(env: Optional[dict[str, str]] = None) -> list[Model]:
    env = dict(os.environ if env is None else env)
    if env.get("MLP_MODELS", "").strip():
        raw = json.loads(env["MLP_MODELS"])
    else:
        raw = json.loads(Path(env.get("MLP_MODELS_FILE") or DEFAULT_FILE).read_text())
    models = [_model(e) for e in raw]
    if not models:
        raise ValueError("the model catalog is empty")
    ids = [m.id for m in models]
    if len(set(ids)) != len(ids):
        raise ValueError(f"model ids must be unique: {ids}")
    return models
