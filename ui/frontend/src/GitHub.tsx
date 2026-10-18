import { useEffect, useState } from "react";
import { api, GhJob, GhLog, GhRun, GitHubSummary } from "./api";

const FAILED = new Set(["failure", "timed_out", "startup_failure"]);
const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const duration = (a?: string | null, b?: string | null) => {
  if (!a || !b) return "";
  const s = Math.max(0, Math.round((new Date(b).getTime() - new Date(a).getTime()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
};

function Result({ status, conclusion }: { status: string; conclusion: string | null }) {
  if (status !== "completed") return <span className="pill pending">{status.replace("_", " ")}</span>;
  if (conclusion && FAILED.has(conclusion)) return <span className="pill violation">{conclusion.replace("_", " ")}</span>;
  if (conclusion === "success") return <span className="pill approved">success</span>;
  return <span className="pill rejected">{conclusion ?? "—"}</span>;
}

function ErrorLog({ job, authenticated }: { job: GhJob; authenticated: boolean }) {
  const [log, setLog] = useState<GhLog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setLog(null); setError(null); }, [job.id]);

  if (!authenticated) {
    return (
      <p className="muted small">
        Error lines appear here once the console has a GitHub token (<code>MLP_GITHUB_TOKEN</code>).{" "}
        <a href={job.url} target="_blank" rel="noreferrer">Open the log on GitHub</a>.
      </p>
    );
  }
  const load = async () => {
    setBusy(true);
    setError(null);
    try { setLog(await api.githubLog(job.id)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="gh-log">
      {!log && <button className="ghost small" disabled={busy} onClick={load}>{busy ? "Loading log…" : "Show error log"}</button>}
      {error && <div className="notice error inline">{error}</div>}
      {log && (
        <>
          <p className="muted small">
            {log.errors ? `${log.errors} error line${log.errors === 1 ? "" : "s"}` : "No lines marked as errors; showing the end of the log"}
            {" "}of {log.total_lines}{log.truncated ? ", trimmed" : ""}.{" "}
            <a href={job.url} target="_blank" rel="noreferrer">Full log on GitHub</a>
          </p>
          <pre className="log">
            {log.lines.map((l, i) => (
              <div key={i} className={l.error ? "err" : l.gap ? "gap" : ""}>{l.text || " "}</div>
            ))}
          </pre>
        </>
      )}
    </div>
  );
}

function Jobs({ jobs, authenticated }: { jobs: GhJob[]; authenticated: boolean }) {
  if (!jobs.length) return <p className="muted small">No jobs yet.</p>;
  return (
    <div className="gh-jobs">
      {jobs.map((j) => (
        <div key={j.id} className={`gh-job ${j.conclusion && FAILED.has(j.conclusion) ? "bad" : ""}`}>
          <div className="row">
            <b>{j.name}</b>
            <span><Result status={j.status} conclusion={j.conclusion} /> <span className="muted small">{duration(j.started_at, j.completed_at)}</span></span>
          </div>
          <ol className="gh-steps">
            {j.steps.map((s) => (
              <li key={s.number} className={s.conclusion && FAILED.has(s.conclusion) ? "bad" : s.conclusion === "skipped" ? "skip" : ""}>
                {s.name} <span className="muted small">{s.status === "completed" ? s.conclusion : s.status.replace("_", " ")}</span>
              </li>
            ))}
          </ol>
          {j.conclusion && FAILED.has(j.conclusion) && <ErrorLog job={j} authenticated={authenticated} />}
        </div>
      ))}
    </div>
  );
}

export default function GitHubView({ summary, error, onRefresh }: {
  summary: GitHubSummary | null; error: string | null; onRefresh: () => void;
}) {
  const [runId, setRunId] = useState<number | null>(null);
  const [jobs, setJobs] = useState<GhJob[] | null>(null);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const runs = summary?.runs ?? [];
  const run: GhRun | undefined = runs.find((r) => r.id === runId) ?? runs[0];

  useEffect(() => {
    if (!summary?.active) return;
    const timer = setTimeout(onRefresh, 30000);
    return () => clearTimeout(timer);
  }, [summary, onRefresh]);
  useEffect(() => {
    if (!run) return;
    setJobs(null);
    setJobsError(null);
    api.githubRun(run.id).then((r) => setJobs(r.jobs)).catch((e) => setJobsError((e as Error).message));
  }, [run?.id, run?.status]);

  if (error) return <div className="notice error">{error}</div>;
  if (!summary) return <div className="detail muted">Loading GitHub runs…</div>;
  if (!summary.configured) {
    return <section className="card"><h3>GitHub</h3><p className="muted small">This model has no GitHub repository in the model catalog.</p></section>;
  }
  const alert = summary.alert;
  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h2>GitHub Actions · {summary.workflow}</h2>
          <p className="muted small">
            <a href={summary.url} target="_blank" rel="noreferrer">{summary.repo}</a>
            {summary.branch ? ` · ${summary.branch}` : ""}{summary.authenticated ? "" : " · no token: logs open on GitHub"}
          </p>
        </div>
        <button className="ghost small" onClick={onRefresh}>Refresh</button>
      </header>

      {alert ? (
        <div className="where stop">
          <span className="now stop">■ Failed</span>
          <div className="where-text">
            <b>Latest run failed:</b> #{alert.run.number} “{alert.run.title}” on {alert.run.branch} by {alert.run.actor}
            {alert.jobs.map((j) => (
              <span key={j.id} className="small">Job <b>{j.name}</b>{j.failed_step ? <> failed at step <b>{j.failed_step}</b></> : " failed"}.</span>
            ))}
            {alert.run.id !== run?.id && <button className="link" onClick={() => setRunId(alert.run.id)}>Show this run</button>}
          </div>
        </div>
      ) : runs.length > 0 && (
        <div className="where done"><span className="now done">✓ Passing</span><div className="where-text">The latest finished run succeeded.</div></div>
      )}

      {run && (
        <section className="card">
          <div className="card-head">
            <h3>Run #{run.number}</h3>
            <Result status={run.status} conclusion={run.conclusion} />
            <a className="small" href={run.url} target="_blank" rel="noreferrer">Open on GitHub</a>
          </div>
          <p className="small">“{run.title}” · <code>{run.sha}</code> · {run.branch} · {run.event} by {run.actor} · {when(run.started_at)}</p>
          {jobsError ? <div className="notice error">{jobsError}</div>
            : jobs ? <Jobs jobs={jobs} authenticated={!!summary.authenticated} /> : <p className="muted small">Loading jobs…</p>}
        </section>
      )}

      <section className="card">
        <h3>Recent runs</h3>
        {runs.length ? (
          <table className="runs">
            <thead><tr><th>Started</th><th>Run</th><th>Commit</th><th>Branch</th><th>Event</th><th>By</th><th>Result</th><th>Time</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className={r.id === run?.id ? "active" : ""} onClick={() => setRunId(r.id)}>
                  <td>{when(r.started_at)}</td>
                  <td>#{r.number}</td>
                  <td title={r.title}><code>{r.sha}</code> {r.title.length > 48 ? `${r.title.slice(0, 48)}…` : r.title}</td>
                  <td>{r.branch}</td>
                  <td>{r.event}</td>
                  <td>{r.actor}</td>
                  <td><Result status={r.status} conclusion={r.conclusion} /></td>
                  <td>{r.status === "completed" ? duration(r.started_at, r.updated_at) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted small">No runs yet.</p>}
      </section>
    </div>
  );
}
