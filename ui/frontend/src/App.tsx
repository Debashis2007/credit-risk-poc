import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, auth, Package, Session } from "./api";

type Notice = { kind: "ok" | "error" | "warn"; text: string } | null;

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
        Signed in as <b>{session.mode === "local" ? auth.user() || session.user : "Okta user"}</b>. The management API
        checks role, tenant scope, separation of duties and the hash binding.
      </p>
      <textarea placeholder="Comment (recorded in the decision log)" value={comment} onChange={(e) => setComment(e.target.value)} />
      <label className="check">
        <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} />
        I reviewed the evidence for candidate <code>{short(pkg.canonical_hash, 16)}</code>
      </label>
      <div className="actions">
        <button className="approve" disabled={!reviewed || busy} onClick={() => decide("approve")}>Approve</button>
        <button className="reject" disabled={!reviewed || busy} onClick={() => decide("reject")}>Reject</button>
      </div>
      {refusal && <div className="notice error inline">{refusal}</div>}
    </section>
  );
}

function Detail({ arn, session, refreshKey, onNotice, onChanged }: {
  arn: string; session: Session; refreshKey: number; onNotice: (n: Notice) => void; onChanged: () => void;
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
            <dt>Submitted by</dt><dd>{pkg.submitted_by || "—"}</dd>
            <dt>Source commit</dt><dd><code>{short(pkg.source_commit, 12)}</code></dd>
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

  const load = useCallback(async () => {
    try {
      const [s, p] = await Promise.all([api.session(), api.packages()]);
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

  const simulateTrain = async () => {
    setTraining(true);
    try {
      const r = await api.simulateTrain();
      setNotice(r.registered
        ? { kind: "ok", text: `Training run passed (auc ${r.metrics.auc.toFixed(4)}); new candidate registered as PendingManualApproval.` }
        : { kind: "warn", text: `Training run failed the evaluation gate (auc ${r.metrics.auc.toFixed(4)}); nothing registered.` });
      await load();
    } finally {
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
            <div className="title">Model approval console</div>
            <div className="muted small">{session.tenant_id ? `${session.tenant_id} · ` : ""}{session.group}</div>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`mode ${session.mode}`}>{session.mode === "local" ? "Local · mocked AWS" : "Remote · management API"}</span>
          {session.mode === "local" ? (
            <label className="user">
              Signed in as
              <select value={auth.user() || session.user || ""} onChange={(e) => { auth.setUser(e.target.value); refresh(); }}>
                {session.identities.map((i) => (
                  <option key={i.email} value={i.email}>{i.email} ({i.roles.join(", ")})</option>
                ))}
              </select>
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

      <main className="layout">
        <aside className="list">
          <div className="list-head">
            <span><b>{packages.length}</b> packages · <b>{pending}</b> pending</span>
            <button className="ghost small" onClick={refresh}>Refresh</button>
          </div>
          {session.mode === "local" && (
            <button className="primary wide" disabled={training} onClick={simulateTrain}>
              {training ? "Training…" : "Simulate training run"}
            </button>
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
                  <span>{p.submitted_by || "unknown submitter"}</span>
                </div>
                <div className="muted small">commit {short(p.source_commit, 8)} · {when(p.created_at)}</div>
              </li>
            ))}
          </ul>
          {!packages.length && <p className="muted small">No model packages in {session.group}.</p>}
        </aside>
        {selected ? (
          <Detail arn={selected} session={session} refreshKey={refreshKey} onNotice={setNotice} onChanged={refresh} />
        ) : (
          <div className="detail muted">Select a package.</div>
        )}
      </main>
    </div>
  );
}
