# credit-risk POC

A small model repository that exercises the ML Platform v7 lifecycle end to end with a
credit-risk classifier trained on synthetic data. The platform code (`ml_platform`, Lambdas,
Terraform) lives in [ML-Platform](https://github.com/Debashis2007/ML-Platform); this repo only
holds the model, its configuration and the train workflow.

> **POC deviation:** training runs in the control plane NonProd account
> (`aws.training_target: control_plane`). The v7 target state trains in the BU account; only the
> `aws` block in `model.yaml` changes.

## Layout

| Path | Purpose |
|------|---------|
| `model.yaml` | Model configuration: accounts, data, thresholds, registry group, deployment targets, approvers |
| `train.py` / `evaluate.py` | Training and evaluation scripts run by the SageMaker pipeline |
| `preprocess.py` | Optional prepare step (`steps.prepare.enabled`) |
| `inference.py` / `Dockerfile` | Serving image, pushed to ECR and pinned by digest |
| `scripts/make_synthetic_data.py` | Synthetic numeric dataset with a `default` target |
| `tests/` | Local smoke test: data, train, evaluate, threshold gate, `model.yaml` validation |
| `.github/workflows/ml-lifecycle.yml` | Test on every PR; build image and start the pipeline on `main` |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone --branch feature/v7-alignment https://github.com/Debashis2007/ML-Platform.git .platform
pip install -r .platform/requirements.txt -r requirements.txt
export PYTHONPATH=$PWD/.platform

python -m pytest tests/ -q                                  # local lifecycle smoke test
python -m ml_platform.build_pipeline --config model.yaml    # pipeline definition, no AWS calls
```

## Lifecycle

1. **Data:** generate and upload the training set to `data.train_uri`:
   ```bash
   python scripts/make_synthetic_data.py --rows 5000 --out data/train/train.csv
   aws s3 cp data/train/train.csv s3://ml-platform-data-848973819860/credit-risk/train/train.csv
   ```
2. **Train (this repo):** a push to `main` builds the image, pins it by digest, upserts pipeline
   `demo-credit-risk` and starts it. The pipeline runs Train, Evaluate, a gate on every threshold,
   then publishes `candidate.json`.
3. **Register (platform):** on pipeline success the `register_candidate` Lambda copies the model
   and evidence into the control plane with checksum verification and creates a
   `PendingManualApproval` package in `ModelCreditRisk`.
4. **Approve (platform):** a senior data scientist other than the submitter approves through the
   private management API (`POST /approvals`). Approvals made any other way are flagged as
   violations and never deploy.
5. **Promote and release (platform):** run the ML-Platform `ml-lifecycle` workflow for promotion
   by digest, Prod retrain and approval, then the QA and LIVE blue/green releases.

## GitHub setup

| Kind | Name | Value |
|------|------|-------|
| Environment | `ml-nonprod` | Add required reviewers if training should be gated |
| Secret (`ml-nonprod`) | `MLP_GHA_PIPELINE_ROLE_ARN` | OIDC role in the control plane NonProd account (client-managed) |
| Variable | `AWS_REGION` | `us-east-1` |
| Variable | `MLP_ARTIFACTS_BUCKET` | Control plane artifacts bucket for pipeline staging output |
| Variable (optional) | `MLP_PLATFORM_REF` | Platform branch or tag; defaults to `feature/v7-alignment` |
| Variable (optional) | `MLP_MODEL_REPOSITORY_PREFIX` | ECR prefix; defaults to `ml-models` |

The OIDC role trust policy must allow `repo:Debashis2007/credit-risk-poc:environment:ml-nonprod`.
IAM roles are client-managed; see `docs/CLIENT_MANAGED_IAM_ROLES.md` in ML-Platform.
