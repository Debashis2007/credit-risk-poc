import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, auth, Identity, Package, Pipeline, Session } from "./api";
import PipelineView from "./Pipeline";

type Notice = { kind: "ok" | "error" | "warn"; text: string } | null;

const ROLE_LABELS: Record<string, string> = {
  senior_data_scientist: "Senior data scientist",
  data_scientist: "Data scientist",
  viewer: "Viewer",
};
const roleLabel = (i: Identity) =>
  (i.roles.map((r) => ROLE_LABELS[r] ?? r).join(", ") || "No role") + (i.in_tenant ? "" : " · other tenant");

function permission(i: Identity | null): { cls: string; text: string } | null {
  if (!i) return null;
  if (i.can_approve) return { cls: "approved", text: "Can approve" };
  if (i.can_train) return { cls: "pending", text: "Can train, cannot approve" };
  return { cls: "rejected", text: "View only" };
}

/** Same order as the management API checks; display only, the API is the enforcer. */
function blockedReason(me: Identity | null, pkg: Package, tenant?: string): string | null {
  if (!me) return null;
  if (!me.active) return "Your identity is not active.";
  if (!me.in_tenant) return `Your approver scope does not include tenant ${tenant ?? pkg.tenant_id}.`;
  if (!me.roles.includes("senior_data_scientist"))
    return `Your role (${roleLabel(me)}) can review but not approve. Approval requires Senior data scientist.`;
  const submitter = (pkg.submitted_by ?? "").toLowerCase();
  if (submitter && [me.email, me.github_login ?? ""].map((v) => v.toLowerCase()).includes(submitter))
    return "You submitted this candidate. Separation of duties: another senior data scientist must approve it.";
  return null;
}

function submitterName(login: string | undefined, session: Session) {
  if (!login) return "unknown submitter";
  const who = session.identities.find((i) => i.github_login === login || i.email === login);
  return who ? `${login} (${who.email})` : login;
}

const short = (value?: string | null, n = 12) => (value ? (value.length > n ? `${value.slice(0, n)}…` : value) : "—");
const digest = (image?: string) => (image?.includes("@sha256:") ? image.split("@")[1] : image);
const when = (iso?: string) => (iso ? new Date(iso).toLocaleString() : "—");

function StatusPill({ pkg }: { pkg: Package }) {
  if (pkg.approved_outside_api) return <span className="pill violation">Approved outside API</span>;
  const cls = { PendingManualApproval: "pending", Approved: "approved", Rejected: "rejected" }[pkg.approval_status] ?? "";
  const label = pkg.approval_status === "PendingManualApproval" ? "Pending approval" : pkg.approval_status;
  return <span className={`pill ${cls}`}>{label}</span>;
}

function MetricBars({ pkg }: { pkg: Package }) {
  const names = Array.from(new Set([...Object.keys(pkg.thresholds), ...Object.keys(pkg.metrics)])).filter(
    (m) => m !== "rows",
  );
  if (!names.length) return <p className="muted">No evaluation evidence attached to this package.</p>;
  return (
    <div className="metrics">
      {names.map((m) => {
        const value = pkg.metrics[m];
        const min = pkg.thresholds[m];
        const pass = min === undefined || (value ?? -1) >= min;
        return (
          <div key={m} className="metric">
            <div className="metric-head">
              <span className="metric-name">{m}</span>
              <span className={pass ? "pass" : "fail"}>
                {value !== undefined ? value.toFixed(4) : "—"}
                {min !== undefined && <span className="muted"> / min {min}</span>}
              </span>
            </div>
            <div className="bar">
              <div className={`fill ${pass ? "pass-bg" : "fail-bg"}`} style={{ width: `${Math.min(100, (value ?? 0) * 100)}%` }} />
              {min !== undefined && <div className="threshold" style={{ left: `${min * 100}%` }} title={`threshold ${min}`} />}
            </div>
          </div>
        );
      })}
      {pkg.metrics.rows !== undefined && <p className="muted small">Evaluated on {pkg.metrics.rows} validation rows.</p>}
    </div>
  );
}

function DecisionPanel({ pkg, session, onDone }: { pkg: Package; session: Session; onDone: (n: Notice) => void }) {
  const [comment, setComment] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  useEffect(() => { setComment(""); setReviewed(false); setRefusal(null); }, [pkg.model_package_arn]);

  if (pkg.approval_status !== "PendingManualApproval") return null;
  if (!pkg.platform_registered) {
    return <div className="notice warn">This package was not registered by the platform and cannot be approved.</div>;
  }
  const blocked = blockedReason(session.me, pkg, session.tenant_id);

  const decide = async (decision: "approve" | "reject") => {
    setBusy(true);
    setRefusal(null);
    try {
      const r = await api.decide(pkg.model_package_arn, decision, pkg.canonical_hash!, comment);
      const capture = r.capture_action ? ` Capture: ${r.capture_action}.` : "";
      onDone({ kind: "ok", text: `Version ${pkg.version} ${r.approval_status.toLowerCase()} (approval ${short(r.approval_id, 8)}).${capture}` });
    } catch (e) {
      const err = e as ApiError;
      const text = `Refused by the management API (${err.status}): ${err.message}`;
      setRefusal(text);
      onDone({ kind: "error", text });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h3>Decision</h3>
      <p className="muted small">
        Signed in as <b>{session.mode === "local" ? session.me?.email ?? session.user : "Okta user"}</b>
        {session.me && <> · {roleLabel(session.me)}</>}. The management API checks role, tenant scope, separation of
        duties and the hash binding.
      </p>
      {blocked && (
        <div className="notice warn blocked">
          {blocked}
          {session.mode === "local" && (
            <button className="ghost small" disabled={busy} onClick={() => decide("approve")}>
              Send approval anyway to see the API refusal
            </button>
          )}
        </div>
      )}
      <textarea placeholder="Comment (recorded in the decision log)" value={comment} disabled={!!blocked}
        onChange={(e) => setComment(e.target.value)} />
      <label className="check">
        <input type="checkbox" checked={reviewed} disabled={!!blocked} onChange={(e) => setReviewed(e.target.checked)} />
        I reviewed the evidence for candidate <code>{short(pkg.canonical_hash, 16)}</code>
      </label>
      <div className="actions">
        <button className="approve" disabled={!!blocked || !reviewed || busy} onClick={() => decide("approve")}>Approve</button>
        <button className="reject" disabled={!!blocked || !reviewed || busy} onClick={() => decide("reject")}>Reject</button>
      </div>
      {refusal && <div className="notice error inline">{refusal}</div>}
    </section>
  );
}

function Detail({ arn, session, refreshKey, onNotice, onChanged, onOpenPipeline }: {
  arn: string; session: Session; refreshKey: number; onNotice: (n: Notice) => void; onChanged: () => void;
  onOpenPipeline: (commit?: string) => void;
}) {
  const [pkg, setPkg] = useState<Package | null>(null);
  useEffect(() => { api.pkg(arn).then(setPkg).catch((e) => onNotice({ kind: "error", text: e.message })); }, [arn, refreshKey, onNotice]);
  if (!pkg) return <div className="detail muted">Loading…</div>;

  const consoleApprove = async () => {
    const r = await api.simulateConsoleApprove(arn);
    onNotice({ kind: "warn", text: `Approved directly in the registry. Capture: ${r.capture_action}; deploy parameter unchanged.` });
    onChanged();
  };

  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h2>{pkg.model_id ?? pkg.group} · version {pkg.version}</h2>
          <p className="muted small">{pkg.model_package_arn}</p>
        </div>
        <StatusPill pkg={pkg} />
      </header>

      {pkg.approved_outside_api && (
        <div className="notice error">
          Approved without a management API approval. Logged as APPROVAL_VIOLATION; no deploy signal was published.
        </div>
      )}

      <div className="grid">
        <section className="card">
          <h3>Evaluation vs thresholds</h3>
          <MetricBars pkg={pkg} />
        </section>
        <section className="card">
          <h3>Provenance</h3>
          <dl>
            <dt>Submitted by</dt><dd>{submitterName(pkg.submitted_by, session)}</dd>
            <dt>Source commit</dt>
            <dd>
              <code>{short(pkg.source_commit, 12)}</code>{" "}
              <button className="link" onClick={() => onOpenPipeline(pkg.source_commit)}>view pipeline run</button>
            </dd>
            <dt>Image digest</dt><dd><code title={pkg.image_uri}>{short(digest(pkg.image_uri), 23)}</code></dd>
            <dt>Artefact SHA-256</dt><dd><code title={pkg.model_data_sha256}>{short(pkg.model_data_sha256, 16)}</code></dd>
            <dt>Canonical hash</dt><dd><code title={pkg.canonical_hash}>{short(pkg.canonical_hash, 16)}</code></dd>
            <dt>Lifecycle state</dt><dd>{pkg.lifecycle_state ?? "—"}</dd>
            <dt>Decided by</dt><dd>{pkg.approved_by ?? "—"}</dd>
            <dt>Evidence</dt><dd className="small">{pkg.evidence_uri ?? "—"}</dd>
          </dl>
        </section>
      </div>

      <DecisionPanel pkg={pkg} session={session} onDone={(n) => { onNotice(n); onChanged(); }} />

      <section className="card">
        <h3>Decision log</h3>
        {pkg.history?.length ? (
          <ol className="timeline">
            {pkg.history.map((h, i) => (
              <li key={i} className={h.action.includes("VIOLATION") || h.action.includes("MISMATCH") ? "bad" : ""}>
                <span className="action">{h.action}</span>
                <span className="muted small"> · {h.actor} · {when(h.at)}</span>
                {h.comment && <div className="small">“{h.comment}”</div>}
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted small">{session.mode === "remote" ? "Decision log is read from DynamoDB in local mode only." : "No entries."}</p>
        )}
      </section>

      {session.mode === "local" && pkg.approval_status === "PendingManualApproval" && (
        <section className="card demo">
          <h3>Demo: bypass the API</h3>
          <p className="muted small">Approve this package directly in the registry (as from the console or CLI) to see violation handling.</p>
          <button className="ghost" onClick={consoleApprove}>Simulate console approval</button>
        </section>
      )}
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [packages, setPackages] = useState<Package[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [training, setTraining] = useState(false);
  const [token, setToken] = useState(auth.token());
  const [tab, setTab] = useState<"approvals" | "pipeline">("approvals");
  const [focusCommit, setFocusCommit] = useState<string | null>(null);
  const openPipeline = useCallback((commit?: string) => { setFocusCommit(commit ?? null); setTab("pipeline"); }, []);
  const openPackage = useCallback((arn: string) => { setSelected(arn); setTab("approvals"); }, []);
  const load = useCallback(async () => {
    try {
      let [s, p] = await Promise.all([api.session(), api.packages()]);
      if (s.mode === "local" && !s.me && auth.user()) {
        auth.setUser("");
        s = await api.session();
      }
      setSession(s);
      setPackages(p);
      if (s.mode === "local" && !auth.user() && s.user) auth.setUser(s.user);
      setSelected((cur) => cur ?? p[0]?.model_package_arn ?? null);
    } catch (e) {
      setNotice({ kind: "error", text: (e as Error).message });
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  const refresh = useCallback(() => { setRefreshKey((k) => k + 1); load(); }, [load]);
  const pending = useMemo(() => packages.filter((p) => p.approval_status === "PendingManualApproval").length, [packages]);

  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [watchRunId, setWatchRunId] = useState<string | null>(null);
  const loadPipeline = useCallback(
    () => api.pipeline().then(setPipeline).catch((e) => setNotice({ kind: "error", text: (e as Error).message })),
    [],
  );
  useEffect(() => { if (tab === "pipeline" || watchRunId) loadPipeline(); }, [tab, refreshKey, watchRunId, loadPipeline]);
  useEffect(() => {
    if (!pipeline?.active_run_id && !watchRunId) return;
    const timer = setTimeout(loadPipeline, 600);
    return () => clearTimeout(timer);
  }, [pipeline, watchRunId, loadPipeline]);

  useEffect(() => {
    if (!watchRunId || !pipeline || pipeline.active_run_id === watchRunId) return;
    const run = pipeline.runs.find((r) => r.run_id === watchRunId);
    if (!run) return;
    const auc = run.metrics.auc !== undefined ? ` (auc ${run.metrics.auc.toFixed(4)})` : "";
    setNotice(run.model_package_arn
      ? { kind: "ok", text: `Run by ${run.submitted_by} passed${auc}; v${run.model_package_arn.split("/").pop()} registered and waiting for approval.` }
      : run.gate === "failed"
        ? { kind: "warn", text: `Run by ${run.submitted_by} stopped at the evaluation gate${auc}; nothing registered.` }
        : { kind: "error", text: `Run by ${run.submitted_by} did not complete (${run.status}).` });
    setWatchRunId(null);
    setTraining(false);
    setSelected(null);
    load();
  }, [pipeline, watchRunId, load]);

  const simulateTrain = async (shuffleLabels = false) => {
    setTraining(true);
    setFocusCommit(null);
    try {
      const r = await api.simulateTrain(shuffleLabels);
      setWatchRunId(r.run_id);
      setNotice(null);
      setTab("pipeline");
    } catch (e) {
      setNotice({ kind: "error", text: (e as Error).message });
      setTraining(false);
    }
  };

  if (!session) return <div className="loading">{notice ? notice.text : "Starting console…"}</div>;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span>
          <div>
            <div className="title">Model lifecycle console</div>
            <div className="muted small">{session.tenant_id ? `${session.tenant_id} · ` : ""}{session.group}</div>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`mode ${session.mode}`}>{session.mode === "local" ? "Local · mocked AWS" : "Remote · management API"}</span>
          {session.mode === "local" ? (
            <label className="user">
              Signed in as
              <select value={session.me?.email ?? session.user ?? ""}
                onChange={(e) => { auth.setUser(e.target.value); setNotice(null); refresh(); }}>
                {session.identities.map((i) => (
                  <option key={i.email} value={i.email}>{i.email} — {roleLabel(i)}</option>
                ))}
              </select>
              {permission(session.me) && (
                <span className={`pill ${permission(session.me)!.cls}`}>{permission(session.me)!.text}</span>
              )}
            </label>
          ) : (
            <input className="token" type="password" placeholder="Okta access token" value={token}
              onChange={(e) => { setToken(e.target.value); auth.setToken(e.target.value); }} />
          )}
        </div>
      </header>

      {notice && (
        <div className={`notice banner ${notice.kind}`} onClick={() => setNotice(null)} title="Dismiss">{notice.text}</div>
      )}

      <nav className="tabs">
        <button className={tab === "approvals" ? "active" : ""} onClick={() => setTab("approvals")}>
          Approvals {pending > 0 && <span className="count">{pending}</span>}
        </button>
        <button className={tab === "pipeline" ? "active" : ""} onClick={() => { setFocusCommit(null); setTab("pipeline"); }}>
          Pipeline
        </button>
      </nav>

      {tab === "pipeline" ? (
        <main className="layout single">
          <PipelineView session={session} pipeline={pipeline} packages={packages} focusCommit={focusCommit}
            followRunId={watchRunId} onTrain={simulateTrain} onOpenPackage={openPackage} />
        </main>
      ) : (
      <main className="layout">
        <aside className="list">
          <div className="list-head">
            <span><b>{packages.length}</b> packages · <b>{pending}</b> pending</span>
            <button className="ghost small" onClick={refresh}>Refresh</button>
          </div>
          {session.mode === "local" && (
            <>
              <button className="primary wide" disabled={training || !session.me?.can_train} onClick={() => simulateTrain()}>
                {training ? "Training…" : `Simulate training run as ${session.me?.github_login ?? session.user}`}
              </button>
              {!session.me?.can_train && (
                <p className="muted small">Training needs a data scientist role in tenant {session.tenant_id}.</p>
              )}
            </>
          )}
          {session.deploy_parameter && (
            <div className="deploy small">Deploy signal → version {session.deploy_parameter.split("/").pop()}</div>
          )}
          <ul>
            {packages.map((p) => (
              <li key={p.model_package_arn} className={p.model_package_arn === selected ? "active" : ""}
                onClick={() => setSelected(p.model_package_arn)}>
                <div className="row">
                  <b>v{p.version}</b>
                  <StatusPill pkg={p} />
                </div>
                <div className="row muted small">
                  <span>auc {p.metrics.auc !== undefined ? p.metrics.auc.toFixed(3) : "—"}</span>
                  <span>by {p.submitted_by || "unknown"}</span>
                </div>
                <div className="muted small">commit {short(p.source_commit, 8)} · {when(p.created_at)}</div>
              </li>
            ))}
          </ul>
          {!packages.length && <p className="muted small">No model packages in {session.group}.</p>}
        </aside>
        {selected ? (
          <Detail arn={selected} session={session} refreshKey={refreshKey} onNotice={setNotice} onChanged={refresh}
            onOpenPipeline={openPipeline} />
        ) : (
          <div className="detail muted">Select a package.</div>
        )}
      </main>
      )}
    </div>
  );
}
