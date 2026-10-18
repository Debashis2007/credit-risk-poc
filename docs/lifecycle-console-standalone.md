# Lifecycle console as a standalone app

The `ui/` folder is a self-contained web console for SageMaker models: approvals, pipeline runs,
live endpoint scoring and GitHub Actions status. This guide covers reusing it for any model repo:
copy the folder, describe your models in `models.json`, set the environment, and build the slim
image (`ui/Dockerfile.standalone`).

The standalone image runs **Real AWS mode** (`MLP_CONSOLE_MODE=aws`) only. The mocked local demo
and the management-API mode need this repo's platform code and use `ui/Dockerfile` instead.

## What the console does

| Tab | Reads | Changes |
|-----|-------|---------|
| **Approvals** | Model packages in the model's registry group, with metrics from each package's `ModelMetrics` | Approve or reject (senior data scientists only); an approval deploys the version to the model's endpoint |
| **Pipeline** | The model's SageMaker pipeline definition and recent executions | **Start run** starts an execution (data scientists and senior data scientists) |
| **Test endpoint** | Endpoint status and the model version it serves | Sends inputs to the endpoint and shows the prediction |
| **GitHub** | GitHub Actions runs of the model's workflow, failed jobs and steps, error lines from logs | Nothing |

The **Model** picker in the header lists every model in the catalog; all tabs follow the selection.

## 1. Copy the folder

```
ui/
├── Dockerfile.standalone   # slim image: console only, Real AWS mode
├── requirements.txt        # fastapi, uvicorn, boto3
├── backend/
│   ├── app.py              # API and routes
│   ├── catalog.py          # loads and validates models.json
│   ├── models.json         # ← your models
│   ├── auth.py             # logins and sessions
│   ├── endpoint.py         # Test endpoint (SigV4-signed SageMaker calls)
│   └── github_ci.py        # GitHub tab
└── frontend/               # React app, built into the image
```

Copy `ui/` into your repo (or a new one). Nothing outside it is needed for the standalone image.
`ui/Dockerfile` and `ui/run_local.sh` belong to the mocked demo and can be deleted in a copy.

## 2. Describe your models in `backend/models.json`

`models.json` is a JSON array with one entry per model. This is the only file you edit to point the
console at your endpoint, pipeline and GitHub repo.

```json
[
  {
    "id": "churn",
    "name": "Customer churn",
    "model_package_group": "ModelChurn",
    "pipeline": "churn-training",
    "endpoint": "churn-qa",
    "github": {"repo": "acme/churn-model", "workflow": "ci.yml", "branch": "main"},
    "thresholds": {"auc": 0.7},
    "features": [
      {"name": "tenure_months", "label": "Tenure", "hint": "months", "step": 1},
      {"name": "monthly_spend", "label": "Monthly spend", "hint": "USD", "step": 0.01},
      {"name": "support_tickets", "label": "Support tickets (90 days)", "hint": "count", "step": 1}
    ],
    "presets": {
      "Likely to stay": {"tenure_months": 48, "monthly_spend": 85, "support_tickets": 0},
      "At risk": {"tenure_months": 3, "monthly_spend": 20, "support_tickets": 6}
    },
    "outcome": {"positive": "Likely to churn", "negative": "Likely to stay", "score": "Churn probability"}
  }
]
```

| Field | Required | Meaning |
|-------|----------|---------|
| `id` | yes | Lowercase letters, digits and hyphens; used in URLs (`?model=churn`) |
| `name` | no | Shown in the Model picker; defaults to `id` |
| `model_package_group` | yes | SageMaker model package group the pipeline registers into |
| `pipeline` | yes | SageMaker pipeline name (Pipeline tab, **Start run**) |
| `endpoint` | no | Real-time endpoint for Test endpoint and deploy-on-approval; without it approvals don't deploy |
| `github` | no | `repo` (`owner/name`), `workflow` (file name in `.github/workflows`), optional `branch`; enables the GitHub tab |
| `thresholds` | no | `{metric: minimum}`; approval is refused when a metric is below its minimum or missing |
| `features` | yes | Model inputs **in the order the model expects**; each is a name or `{name, label, hint, step}` |
| `presets` | no | Example inputs for the Test endpoint form; each must set every feature |
| `outcome` | no | Labels for a positive prediction, a negative one, and the score |

Instead of editing the file you can put the same JSON array in the `MLP_MODELS` environment variable
(or point `MLP_MODELS_FILE` at a file). The console validates the catalog at startup and refuses to
start with a clear message when an entry is wrong.

### What the console expects from each model in AWS

- **Registry:** the pipeline registers packages into `model_package_group` with
  `ModelApprovalStatus=PendingManualApproval` and `ModelMetrics.ModelQuality.Statistics.S3Uri`
  pointing at a JSON file of metrics (for example `{"auc": 0.79, "accuracy": 0.82}`). Those metric
  names are what `thresholds` checks.
- **Registration step description:** to show who started a run and enforce separation of duties,
  the register step's `ModelPackageDescription` should end with `pipeline execution <execution id>`
  (in a pipeline definition: `{"Std:Join": {"On": " ", "Values": ["my-model pipeline execution",
  {"Get": "Execution.PipelineExecutionId"}]}}`). Packages without it show "unknown" as submitter.
- **Inference container:** accepts `{"features": [[...], ...]}` as `application/json` and returns
  `{"prediction": [0|1, ...], "score": [float, ...]}`.
- **Deploys:** an approval creates a SageMaker model and endpoint config named
  `<endpoint>-v<version>-<timestamp>` using the pipeline's execution role, on the package's first
  supported real-time instance type, then updates the endpoint in place (or creates it).

## 3. Set the environment

| Variable | Required | Value |
|----------|----------|-------|
| `CONSOLE_USERS` | yes | `username:<hash>` entries, comma-separated. Hash: `python backend/auth.py hash` |
| `CONSOLE_SESSION_SECRET` | yes | At least 32 random characters: `python backend/auth.py secret` |
| `CONSOLE_PROJECT` | yes | Short project name; part of the login cookie name, so consoles never share sessions |
| `CONSOLE_ROLES` | yes | `username:senior_data_scientist` or `username:data_scientist`, comma-separated; unlisted users can only view |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | yes | Key for Approvals, Pipeline and deploys (IAM policy below) |
| `AWS_REGION` | yes | Region of the SageMaker resources, e.g. `us-east-1` |
| `MLP_ENDPOINT_AWS_ACCESS_KEY_ID`, `MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY` | for Test endpoint | Key used to call endpoints; can be the same key |
| `MLP_ENDPOINT_REGION` | no | Endpoint region; defaults to `us-east-1` |
| `MLP_GITHUB_TOKEN` | for GitHub logs | Fine-grained token, repository permission **Actions: Read-only** on the model repos. Without it, public repos still list runs (60 GitHub requests an hour) and logs link to GitHub |
| `CONSOLE_SESSION_HOURS` | no | Login lifetime; default 12 |
| `MLP_MODELS` / `MLP_MODELS_FILE` | no | Catalog override (step 2) |
| `PORT` | no | Listen port; default 8000 (Railway sets it) |

The image sets `MLP_CONSOLE_MODE=aws` and `CONSOLE_AUTH_REQUIRED=1`, so it refuses to start
without `CONSOLE_USERS` and `CONSOLE_SESSION_SECRET`.

Roles:

| Role | Start runs | Approve or reject |
|------|------------|-------------------|
| `senior_data_scientist` | yes | yes, except versions from runs they started |
| `data_scientist` | yes | no |
| anyone else | no | no |

### IAM policy for the console's key

Create an IAM user (or role) for the console and attach a policy scoped to your models. Replace
`ACCOUNT`, `REGION`, `GROUP`, `PIPELINE`, `ENDPOINT`, `EXECUTION_ROLE` and `METRICS_BUCKET/PREFIX`;
repeat the resource ARNs for each model in the catalog. List the package group both as written and
in lowercase (`group-lowercase`), since SageMaker can report either form in model package ARNs.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ListRegistry", "Effect": "Allow", "Action": "sagemaker:ListModelPackages", "Resource": "*"},
    {"Sid": "ReviewAndDecide", "Effect": "Allow",
     "Action": ["sagemaker:DescribeModelPackage", "sagemaker:UpdateModelPackage"],
     "Resource": ["arn:aws:sagemaker:REGION:ACCOUNT:model-package/GROUP/*",
                  "arn:aws:sagemaker:REGION:ACCOUNT:model-package/group-lowercase/*"]},
    {"Sid": "Pipeline", "Effect": "Allow",
     "Action": ["sagemaker:DescribePipeline", "sagemaker:ListPipelineExecutions", "sagemaker:StartPipelineExecution",
                "sagemaker:DescribePipelineExecution", "sagemaker:ListPipelineExecutionSteps",
                "sagemaker:ListPipelineParametersForExecution"],
     "Resource": ["arn:aws:sagemaker:REGION:ACCOUNT:pipeline/PIPELINE",
                  "arn:aws:sagemaker:REGION:ACCOUNT:pipeline/PIPELINE/execution/*"]},
    {"Sid": "EndpointReadAndInvoke", "Effect": "Allow",
     "Action": ["sagemaker:InvokeEndpoint", "sagemaker:DescribeEndpoint"],
     "Resource": "arn:aws:sagemaker:REGION:ACCOUNT:endpoint/ENDPOINT"},
    {"Sid": "DescribeServingModel", "Effect": "Allow",
     "Action": ["sagemaker:DescribeEndpointConfig", "sagemaker:DescribeModel"],
     "Resource": ["arn:aws:sagemaker:REGION:ACCOUNT:endpoint-config/ENDPOINT-*",
                  "arn:aws:sagemaker:REGION:ACCOUNT:model/*"]},
    {"Sid": "DeployApprovedVersion", "Effect": "Allow",
     "Action": ["sagemaker:CreateModel", "sagemaker:CreateEndpointConfig", "sagemaker:CreateEndpoint", "sagemaker:UpdateEndpoint"],
     "Resource": ["arn:aws:sagemaker:REGION:ACCOUNT:model/ENDPOINT-*",
                  "arn:aws:sagemaker:REGION:ACCOUNT:endpoint-config/ENDPOINT-*",
                  "arn:aws:sagemaker:REGION:ACCOUNT:endpoint/ENDPOINT"]},
    {"Sid": "PassExecutionRole", "Effect": "Allow", "Action": "iam:PassRole",
     "Resource": "arn:aws:iam::ACCOUNT:role/EXECUTION_ROLE",
     "Condition": {"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}}},
    {"Sid": "ReadEvaluationMetrics", "Effect": "Allow", "Action": "s3:GetObject",
     "Resource": "arn:aws:s3:::METRICS_BUCKET/PREFIX/*"}
  ]
}
```

Use a managed policy: inline user policies are limited to 2,048 characters, which this exceeds.
Drop the `DeployApprovedVersion` and `PassExecutionRole` statements if approvals should not deploy
(and leave `endpoint` out of the catalog entry).

## 4. Build and run the slim image

From the `ui/` folder:

```bash
docker build -f Dockerfile.standalone -t lifecycle-console .

docker run --rm -p 8000:8000 --env-file console.env lifecycle-console
# open http://localhost:8000
```

`console.env` holds the variables from step 3, one `NAME=value` per line. Keep it out of git.

The image is about 300 MB: Node builds the frontend in a first stage; the final stage is Python 3.11
slim with FastAPI, uvicorn and boto3, running as a non-root user.

### Deploy on Railway

Add `railway.json` next to `Dockerfile.standalone` (if `ui/` is the repo root), or set the service's
`RAILWAY_DOCKERFILE_PATH` to `ui/Dockerfile.standalone` and its root directory to `ui`:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile.standalone"},
  "deploy": {"healthcheckPath": "/api/health", "healthcheckTimeout": 300,
             "restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 3}
}
```

Set the step 3 variables on the service, then `railway up` (or connect the GitHub repo).

## 5. Check it

1. `GET /api/health` returns `{"status":"ok"}`; `GET /api/packages` returns 401 until you sign in.
2. Sign in: the header shows **Real AWS**, your role, and the Model picker with your models.
3. **Approvals** lists your registry group's versions with metrics against your thresholds.
4. **Pipeline** shows your pipeline's steps and recent executions.
5. **Test endpoint** shows the endpoint as InService; a preset returns a prediction.
6. **GitHub** lists your workflow's runs; on a failed job, **Show error log** shows the error lines.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| Container exits: `CONSOLE_AUTH_REQUIRED=1 but CONSOLE_USERS is not set` | Set `CONSOLE_USERS` and `CONSOLE_SESSION_SECRET` |
| Container exits with a catalog error | Fix the named entry in `models.json` (missing field, bad id, preset not matching features) |
| `AWS AccessDeniedException: … not authorized to perform sagemaker:…` | Add the action and resource to the console's IAM policy |
| Approvals show no metrics, approval refused "below threshold or missing" | The package has no `ModelMetrics` S3 URI, the key cannot read it (`s3:GetObject`), or the metric names differ from `thresholds` |
| Submitter shows "unknown"; separation of duties not enforced | The register step's description does not end with `pipeline execution <id>` |
| "Approved, but the deploy … failed" | Usually missing `iam:PassRole` on the pipeline's execution role, or the endpoint was mid-update. The version stays approved but undeployed, and the console deploys only when a pending version is approved, so fix the cause and deploy that version with your own tooling (or approve the next version) |
| Test endpoint: "this console has no key for it" | Set `MLP_ENDPOINT_AWS_ACCESS_KEY_ID` and `MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY` |
| GitHub tab: "rate limit reached" or no error lines | Set `MLP_GITHUB_TOKEN` (Actions read-only) |
| `MLP_CONSOLE_MODE=local needs the model repo's dev requirements` | The standalone image runs Real AWS mode only; use `ui/Dockerfile` in the model repo for the mocked demo |
| A run started outside the page does not appear | Click **Refresh**; the page polls only runs it started |

## Known limits

- Approval deploys directly from the console; approvals made elsewhere (CLI, Studio) do not deploy.
  An EventBridge rule on "SageMaker Model Package State Change" with a deploy Lambda removes that
  limit.
- One endpoint per model, one instance; no autoscaling or staged QA/LIVE releases.
- The first page load after a restart takes several seconds while submitters are looked up; they are
  cached afterwards.
