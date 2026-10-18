import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, EndpointStatus, ScoreResult } from "./api";

const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const statusClass = (s?: string) =>
  s === "InService" ? "approved" : s === "Failed" || s === "NotDeployed" ? "violation" : "pending";
const asText = (values: Record<string, number>) =>
  Object.fromEntries(Object.entries(values).map(([k, v]) => [k, String(v)]));

export default function EndpointView() {
  const [status, setStatus] = useState<EndpointStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<ScoreResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadStatus = useCallback(() => {
    setStatusError(null);
    api.endpoint().then((s) => {
      setStatus(s);
      setValues((cur) => (Object.keys(cur).length ? cur : asText(Object.values(s.presets)[0] ?? {})));
    }).catch((e) => setStatusError((e as Error).message));
  }, []);
  useEffect(() => { loadStatus(); }, [loadStatus]);

  const applyPreset = (name: string) => {
    setValues(asText(status?.presets[name] ?? {}));
    setResult(null);
    setError(null);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await api.score([Object.fromEntries(Object.entries(values).map(([k, v]) => [k, Number(v)]))]));
    } catch (err) {
      setResult(null);
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (statusError) return <div className="notice error">Endpoint status unavailable: {statusError}</div>;
  if (!status) return <div className="muted">Loading endpoint…</div>;
  if (!status.configured) {
    return (
      <section className="card">
        <h3>Test endpoint</h3>
        <p className="muted small">
          {status.name
            ? <>Endpoint <code>{status.name}</code> is in the catalog, but this console has no key for it. Set{" "}
              <code>MLP_ENDPOINT_AWS_ACCESS_KEY_ID</code> and <code>MLP_ENDPOINT_AWS_SECRET_ACCESS_KEY</code>.</>
            : <>This model has no endpoint in the model catalog.</>}
        </p>
      </section>
    );
  }

  const live = status.status === "InService";
  const scored = result?.results[0];
  const score = scored?.score ?? null;

  return (
    <div className="endpoint">
      <section className="card">
        <div className="card-head">
          <h3>Endpoint</h3>
          <span className={`pill ${statusClass(status.status)}`}>
            {status.status === "NotDeployed" ? "Not deployed" : status.status}
          </span>
          <button className="ghost small" onClick={loadStatus}>Refresh</button>
        </div>
        <dl>
          <dt>Name</dt><dd><code>{status.name}</code> · {status.region}</dd>
          <dt>Model version</dt><dd>{status.version ? `v${status.version}` : "—"}</dd>
          <dt>Instance</dt><dd>{status.instance_type ? `${status.instance_count ?? 1} × ${status.instance_type}` : "—"}</dd>
          <dt>Updated</dt><dd>{when(status.updated_at)}</dd>
          <dt>Model package</dt><dd className="small">{status.model_package_arn ?? "—"}</dd>
        </dl>
        {status.status === "NotDeployed" && (
          <div className="notice warn inline">The endpoint has been deleted. Deploy it again to score applicants.</div>
        )}
        {status.failure_reason && <div className="notice error inline">{status.failure_reason}</div>}
      </section>

      <div className="grid">
        <form className="card score-form" onSubmit={submit}>
          <div className="card-head">
            <h3>Input</h3>
            {Object.keys(status.presets).map((name) => (
              <button key={name} type="button" className="ghost small" onClick={() => applyPreset(name)}>{name}</button>
            ))}
          </div>
          {status.features.map((f) => (
            <label key={f.name}>
              <span>{f.label} {f.hint && <span className="muted small">{f.hint}</span>}</span>
              <input type="number" required step={f.step ?? "any"} value={values[f.name] ?? ""}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
            </label>
          ))}
          <button className="primary wide" disabled={busy || !live}>{busy ? "Scoring…" : "Score"}</button>
        </form>

        <section className="card">
          <h3>Prediction</h3>
          {error && <div className="notice error">{error}</div>}
          {!error && !scored && <p className="muted small">Score an input to see the endpoint's response.</p>}
          {scored && (
            <div className="prediction">
              <div className={`verdict ${scored.prediction ? "fail" : "pass"}`}>
                {scored.prediction ? status.outcome.positive : status.outcome.negative}
              </div>
              {score !== null && (
                <>
                  <div className="metric-head">
                    <span className="metric-name">{status.outcome.score}</span>
                    <span className={scored.prediction ? "fail" : "pass"}>{(score * 100).toFixed(1)}%</span>
                  </div>
                  <div className="bar">
                    <div className={`fill ${scored.prediction ? "fail-bg" : "pass-bg"}`} style={{ width: `${score * 100}%` }} />
                    <div className="threshold" style={{ left: "50%" }} title="decision threshold 0.5" />
                  </div>
                </>
              )}
              <p className="muted small">
                {result!.endpoint} · model {result!.model_version ?? "—"} · {result!.latency_ms} ms round trip
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
