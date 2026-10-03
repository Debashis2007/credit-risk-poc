export type Identity = {
  email: string;
  roles: string[];
  github_login?: string;
  tenants: string[];
  active: boolean;
  in_tenant: boolean;
  can_approve: boolean;
  can_train: boolean;
};

export type Session = {
  mode: "local" | "remote";
  user: string | null;
  me: Identity | null;
  group: string;
  model_id?: string;
  tenant_id?: string;
  identities: Identity[];
  deploy_parameter: string | null;
};

export type HistoryEntry = { at: string; action: string; actor?: string; comment?: string | null };

export type Package = {
  model_package_arn: string;
  version: number;
  group: string;
  approval_status: string;
  created_at: string;
  tenant_id?: string;
  model_id?: string;
  canonical_hash?: string;
  model_data_sha256?: string;
  source_commit?: string;
  submitted_by?: string;
  approved_by?: string;
  approval_id?: string;
  evidence_uri?: string;
  platform_registered: boolean;
  metrics: Record<string, number>;
  thresholds: Record<string, number>;
  image_uri?: string;
  lifecycle_state?: string | null;
  approved_outside_api: boolean;
  history?: HistoryEntry[];
};

export type StepDef = {
  name: string;
  type: string;
  depends_on: string[];
  parent: string | null;
  branch: "if" | "else" | null;
  details: Record<string, unknown>;
};

export type RunStep = {
  name: string;
  kind: "ci" | "pipeline" | "event";
  type: string;
  status: string;
  started_at: string | null;
  duration_ms: number | null;
  detail: string;
  outputs: Record<string, unknown>;
};

export type PipelineRun = {
  run_id: string;
  execution_arn: string | null;
  commit?: string;
  submitted_by?: string;
  rows?: number;
  shuffle_labels?: boolean;
  status: string;
  gate: "passed" | "failed" | null;
  started_at: string;
  ended_at: string | null;
  parameters: Record<string, string>;
  metrics: Record<string, number>;
  thresholds: Record<string, number>;
  steps: RunStep[];
  model_package_arn: string | null;
};

export type Pipeline = {
  name: string;
  arn?: string;
  role_arn?: string;
  steps: StepDef[];
  parameters: { name: string; type?: string; default?: string }[];
  runs: PipelineRun[];
  active_run_id: string | null;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const USER_KEY = "mlp-console-user";
const TOKEN_KEY = "mlp-console-okta-token";

export const auth = {
  user: () => sessionStorage.getItem(USER_KEY) ?? "",
  setUser: (email: string) => sessionStorage.setItem(USER_KEY, email),
  token: () => sessionStorage.getItem(TOKEN_KEY) ?? "",
  setToken: (token: string) => sessionStorage.setItem(TOKEN_KEY, token),
};

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (auth.user()) headers["X-Console-User"] = auth.user();
  if (auth.token()) headers["Authorization"] = `Bearer ${auth.token()}`;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.detail ?? data.error ?? res.statusText);
  return data as T;
}

export const api = {
  session: () => request<Session>("GET", "/api/session"),
  packages: () => request<Package[]>("GET", "/api/packages"),
  pkg: (arn: string) => request<Package>("GET", `/api/package?arn=${encodeURIComponent(arn)}`),
  decide: (arn: string, decision: "approve" | "reject", canonicalHash: string, comment: string) =>
    request<{ approval_status: string; approval_id: string; capture_action?: string; deploy_parameter?: string }>(
      "POST", "/api/decision",
      { model_package_arn: arn, decision, canonical_hash: canonicalHash, comment },
    ),
  pipeline: () => request<Pipeline>("GET", "/api/pipeline"),
  simulateTrain: (shuffleLabels = false) =>
    request<{ run_id: string }>("POST", "/api/simulate/train", { shuffle_labels: shuffleLabels }),
  simulateConsoleApprove: (arn: string) =>
    request<{ capture_action: string; deploy_parameter: string | null }>(
      "POST", "/api/simulate/console-approve", { model_package_arn: arn },
    ),
};
