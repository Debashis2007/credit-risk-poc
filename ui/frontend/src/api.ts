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
  mode: "local" | "remote" | "aws";
  user: string | null;
  me: Identity | null;
  group: string;
  model_id?: string;
  tenant_id?: string;
  identities: Identity[];
  deploy_parameter: string | null;
  region?: string;
  endpoint?: string | null;
  models: ModelInfo[];
  model: string;
};

export type ModelInfo = { id: string; name: string; group: string; pipeline: string; endpoint: string | null };

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
  serving?: boolean;
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

export type Feature = { name: string; label: string; hint?: string; step?: string };

export type EndpointStatus = {
  configured: boolean;
  model: string;
  features: Feature[];
  presets: Record<string, Record<string, number>>;
  outcome: { positive: string; negative: string; score: string };
  name?: string;
  region?: string;
  status?: string;
  failure_reason?: string | null;
  updated_at?: string | null;
  model_name?: string;
  instance_type?: string;
  instance_count?: number;
  model_package_arn?: string | null;
  version?: number | null;
};

export type ScoreResult = {
  endpoint: string;
  model_version?: string;
  latency_ms: number;
  results: { prediction: number; score: number | null }[];
};

export type GhRun = {
  id: number; number: number; title: string; sha: string; branch: string; event: string; actor: string;
  status: string; conclusion: string | null; started_at: string; updated_at: string; url: string; attempt: number;
};
export type GhStep = { number: number; name: string; status: string; conclusion: string | null };
export type GhJob = {
  id: number; name: string; status: string; conclusion: string | null; started_at: string | null;
  completed_at: string | null; url: string; failed_step: string | null; steps: GhStep[];
};
export type GitHubSummary = {
  configured: boolean; model: string; repo?: string; workflow?: string; branch?: string; authenticated?: boolean;
  url?: string; runs?: GhRun[]; alert?: { run: GhRun; jobs: GhJob[] } | null; active?: boolean;
};
export type GhLog = { lines: { text: string; error: boolean; gap: boolean }[]; errors: number; truncated: boolean; total_lines: number };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const USER_KEY = "mlp-console-user";
const TOKEN_KEY = "mlp-console-okta-token";
const MODEL_KEY = "mlp-console-model";

export const auth = {
  user: () => sessionStorage.getItem(USER_KEY) ?? "",
  setUser: (email: string) => sessionStorage.setItem(USER_KEY, email),
  model: () => sessionStorage.getItem(MODEL_KEY) ?? "",
  setModel: (id: string) => sessionStorage.setItem(MODEL_KEY, id),
  token: () => sessionStorage.getItem(TOKEN_KEY) ?? "",
  setToken: (token: string) => sessionStorage.setItem(TOKEN_KEY, token),
};

export type LoginState = { enabled: boolean; user: string | null; project: string };

let onUnauthorized: (() => void) | null = null;
export const setUnauthorizedHandler = (handler: () => void) => { onUnauthorized = handler; };

export const login = {
  me: async (): Promise<LoginState> => (await fetch("/api/auth/me")).json(),
  signIn: async (username: string, password: string): Promise<LoginState> => {
    const res = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(res.status, data.detail ?? res.statusText);
    return data as LoginState;
  },
  signOut: () => fetch("/api/auth/logout", { method: "POST" }),
};

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (auth.user()) headers["X-Console-User"] = auth.user();
  if (auth.token()) headers["Authorization"] = `Bearer ${auth.token()}`;
  const url = auth.model() ? `${path}${path.includes("?") ? "&" : "?"}model=${encodeURIComponent(auth.model())}` : path;
  const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && data.detail === "login required") onUnauthorized?.();
  if (!res.ok) throw new ApiError(res.status, data.detail ?? data.error ?? res.statusText);
  return data as T;
}

export const api = {
  session: () => request<Session>("GET", "/api/session"),
  packages: () => request<Package[]>("GET", "/api/packages"),
  pkg: (arn: string) => request<Package>("GET", `/api/package?arn=${encodeURIComponent(arn)}`),
  decide: (arn: string, decision: "approve" | "reject", canonicalHash: string, comment: string) =>
    request<{
      approval_status: string; approval_id: string; capture_action?: string; deploy_parameter?: string | null;
      deploy_message?: string | null;
    }>(
      "POST", "/api/decision",
      { model_package_arn: arn, decision, canonical_hash: canonicalHash, comment },
    ),
  pipeline: () => request<Pipeline>("GET", "/api/pipeline"),
  endpoint: () => request<EndpointStatus>("GET", "/api/endpoint"),
  github: () => request<GitHubSummary>("GET", "/api/github"),
  githubRun: (runId: number) => request<{ run: GhRun; jobs: GhJob[] }>("GET", `/api/github/run?run_id=${runId}`),
  githubLog: (jobId: number) => request<GhLog>("GET", `/api/github/log?job_id=${jobId}`),
  score: (applicants: Record<string, number>[]) =>
    request<ScoreResult>("POST", "/api/endpoint/score", { applicants }),
  train: () => request<{ run_id: string }>("POST", "/api/train"),
  simulateTrain: (shuffleLabels = false) =>
    request<{ run_id: string }>("POST", "/api/simulate/train", { shuffle_labels: shuffleLabels }),
  simulateConsoleApprove: (arn: string) =>
    request<{ capture_action: string; deploy_parameter: string | null }>(
      "POST", "/api/simulate/console-approve", { model_package_arn: arn },
    ),
};
