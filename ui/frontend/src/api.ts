export type Identity = { email: string; roles: string[]; github_login?: string };

export type Session = {
  mode: "local" | "remote";
  user: string | null;
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
  simulateTrain: () =>
    request<{ registered: boolean; passed: boolean; metrics: Record<string, number> }>("POST", "/api/simulate/train"),
  simulateConsoleApprove: (arn: string) =>
    request<{ capture_action: string; deploy_parameter: string | null }>(
      "POST", "/api/simulate/console-approve", { model_package_arn: arn },
    ),
};
