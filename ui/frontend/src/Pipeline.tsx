import { Fragment, useEffect, useMemo, useState } from "react";
import { api, Package, Pipeline, PipelineRun, RunStep, Session, StepDef } from "./api";

type Node = { name: string; type: string; def?: StepDef; label?: string };

const STATUS_CLASS: Record<string, string> = {
  Succeeded: "ok", Failed: "bad", Executing: "running", Skipped: "skipped", NotExecuted: "skipped",
};
const short = (v?: string | null, n = 12) => (v ? (v.length > n ? `${v.slice(0, n)}…` : v) : "—");
const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const secs = (ms?: number | null) => (ms == null ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
const execId = (arn?: string | null) => (arn ? arn.split("/").pop() : "—");
const version = (arn?: string | null) => (arn ? arn.split("/").pop() : undefined);
const show = (v: unknown) => (Array.isArray(v) ? v.join(", ") : typeof v === "object" && v ? JSON.stringify(v) : String(v));

function runDuration(run: PipelineRun) {
  return run.steps.reduce((total, s) => total + (s.duration_ms ?? 0), 0);
}

function StepNode({ node, step, selected, onSelect }: {
  node: Node; step?: RunStep; selected: boolean; onSelect: () => void;
}) {
  const outcome = step?.outputs.outcome as string | undefined;
  const cls = !step ? "idle" : outcome === "False" ? "bad" : STATUS_CLASS[step.status] ?? "";
  const status = step
    ? [outcome ? `${step.status}, ${outcome}` : step.status, step.duration_ms != null ? secs(step.duration_ms) : ""]
      .filter(Boolean).join(" · ")
    : "not run";
  return (
    <button className={`node ${cls} ${selected ? "selected" : ""}`} onClick={onSelect} title={step?.detail || node.type}>
      <span className="node-name">{node.label ?? node.name}</span>
      <span className="node-type">{node.type}</span>
      <span className="node-status">{status}</span>
    </button>
  );
}

function Flow({ nodes, run, selected, onSelect }: {
  nodes: Node[]; run?: PipelineRun; selected: string; onSelect: (n: string) => void;
}) {
  return (
    <div className="flow">
      {nodes.map((n, i) => (
        <Fragment key={n.name}>
          {i > 0 && <span className={`arrow ${n.def?.branch ? "branch" : ""}`}>{n.def?.branch === "if" ? "True →" : n.def?.branch === "else" ? "False →" : "→"}</span>}
          <StepNode node={n} step={run?.steps.find((s) => s.name === n.name)} selected={selected === n.name}
            onSelect={() => onSelect(n.name)} />
        </Fragment>
      ))}
    </div>
  );
}

export default function PipelineView({
  session, packages, refreshKey, training, focusCommit, onTrain, onOpenPackage, onError,
}: {
  session: Session;
  packages: Package[];
  refreshKey: number;
  training: boolean;
  focusCommit: string | null;
  onTrain: (shuffleLabels: boolean) => Promise<string | undefined>;
  onOpenPackage: (arn: string) => void;
  onError: (text: string) => void;
}) {
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [runArn, setRunArn] = useState<string | null>(null);
  const [stepName, setStepName] = useState("Train");

  useEffect(() => {
    api.pipeline().then((p) => {
      setPipeline(p);
      const focused = focusCommit ? p.runs.find((r) => r.commit === focusCommit)?.execution_arn : undefined;
      setRunArn((cur) => focused ?? (cur && p.runs.some((r) => r.execution_arn === cur) ? cur : p.runs[0]?.execution_arn ?? null));
    }).catch((e) => onError(e.message));
  }, [refreshKey, focusCommit, onError]);

  const run = pipeline?.runs.find((r) => r.execution_arn === runArn);
  const pkg = packages.find((p) => p.model_package_arn === run?.model_package_arn);
  const pipelineNodes = useMemo<Node[]>(
    () => (pipeline?.steps ?? []).filter((s) => s.branch !== "else").map((s) => ({ name: s.name, type: s.type, def: s })),
    [pipeline],
  );
  if (!pipeline) return <div className="detail muted">Loading pipeline…</div>;

  const ciNodes: Node[] = [
    { name: "BuildImage", type: "GitHub Actions → ECR", label: "Build image" },
    { name: "UploadData", type: "S3", label: "Training data" },
  ];
  const eventNodes: Node[] = [{ name: "RegisterCandidate", type: "EventBridge → Lambda", label: "Register candidate" }];
  const allNodes = [...ciNodes, ...pipelineNodes, ...eventNodes];
  const node = allNodes.find((n) => n.name === stepName) ?? allNodes[0];
  const step = run?.steps.find((s) => s.name === node.name);
  const deployed = session.deploy_parameter && session.deploy_parameter === run?.model_package_arn;
  const elseSteps = pipeline.steps.filter((s) => s.branch === "else");

  const start = async (shuffle: boolean) => {
    const arn = await onTrain(shuffle);
    if (arn) setRunArn(arn);
  };

  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h2>Training pipeline · {pipeline.name}</h2>
          <p className="muted small">{pipeline.arn}</p>
        </div>
        {session.mode === "local" && (
          <div className="actions">
            <button className="primary" disabled={training || !session.me?.can_train} onClick={() => start(false)}>
              {training ? "Running…" : "Start run"}
            </button>
            <button className="ghost" disabled={training || !session.me?.can_train} onClick={() => start(true)}
              title="Trains on shuffled labels, so the CheckMetric gate fails">
              Start run on shuffled labels
            </button>
          </div>
        )}
      </header>
      {session.mode === "local" && !session.me?.can_train && (
        <div className="notice warn">Starting a run needs a data scientist role in tenant {session.tenant_id}.</div>
      )}

      <section className="card">
        <div className="card-head">
          <h3>Flow</h3>
          {run && <span className="muted small">execution <code>{execId(run.execution_arn)}</code> by {run.submitted_by}</span>}
        </div>
        <div className="lanes">
          <div className="lane">
            <div className="lane-title">CI</div>
            <Flow nodes={ciNodes} run={run} selected={node.name} onSelect={setStepName} />
          </div>
          <span className="arrow">→</span>
          <div className="lane sagemaker">
            <div className="lane-title">SageMaker pipeline (definition from the platform)</div>
            <Flow nodes={pipelineNodes} run={run} selected={node.name} onSelect={setStepName} />
            <div className="muted small">
              CheckMetric False → {elseSteps.length ? elseSteps.map((s) => s.name).join(", ") : "end, no candidate published"}
            </div>
          </div>
          <span className="arrow">→</span>
          <div className="lane">
            <div className="lane-title">Control plane</div>
            <div className="flow">
              <StepNode node={eventNodes[0]} step={run?.steps.find((s) => s.name === "RegisterCandidate")}
                selected={node.name === "RegisterCandidate"} onSelect={() => setStepName("RegisterCandidate")} />
              <span className="arrow">→</span>
              <button className={`node ${pkg ? (pkg.approval_status === "Approved" ? "ok" : pkg.approval_status === "Rejected" ? "skipped" : "running") : "idle"}`}
                disabled={!pkg} onClick={() => pkg && onOpenPackage(pkg.model_package_arn)}
                title={pkg ? "Open in approvals" : "No package registered"}>
                <span className="node-name">Registry {pkg ? `v${pkg.version}` : ""}</span>
                <span className="node-type">Model package</span>
                <span className="node-status">{pkg ? (pkg.approval_status === "PendingManualApproval" ? "Pending approval" : pkg.approval_status) : "none"}</span>
              </button>
              <span className="arrow">→</span>
              <div className={`node static ${deployed ? "ok" : "idle"}`}>
                <span className="node-name">Deploy signal</span>
                <span className="node-type">SSM parameter</span>
                <span className="node-status">{deployed ? "Published" : "Not published"}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <div className="grid">
        <section className="card">
          <h3>Step · {node.label ?? node.name}</h3>
          <dl>
            <dt>Type</dt><dd>{node.type}</dd>
            <dt>Status</dt><dd>{step ? <span className={`status ${STATUS_CLASS[step.status] ?? ""}`}>{step.status}</span> : "not run"}</dd>
            {step?.started_at && <><dt>Started</dt><dd>{when(step.started_at)}</dd></>}
            {step?.duration_ms != null && <><dt>Duration</dt><dd>{secs(step.duration_ms)}</dd></>}
            {step?.detail && <><dt>Detail</dt><dd>{step.detail}</dd></>}
            {Object.entries(node.def?.details ?? {}).filter(([, v]) => v !== undefined && v !== null && v !== "")
              .map(([k, v]) => <Fragment key={`d-${k}`}><dt>{k.replace(/_/g, " ")}</dt><dd><code>{show(v)}</code></dd></Fragment>)}
            {Object.entries(step?.outputs ?? {}).map(([k, v]) => (
              <Fragment key={`o-${k}`}><dt>{k.replace(/_/g, " ")}</dt><dd className="small">{show(v)}</dd></Fragment>
            ))}
          </dl>
          {session.mode === "local" && node.def && (
            <p className="muted small">Locally, SageMaker jobs run as scripts on this machine; the definition is the one the platform deploys.</p>
          )}
        </section>
        <section className="card">
          <h3>Execution parameters</h3>
          {run ? (
            <dl>
              {pipeline.parameters.map((p) => (
                <Fragment key={p.name}><dt>{p.name}</dt><dd className="small">{run.parameters[p.name] || <span className="muted">(empty)</span>}</dd></Fragment>
              ))}
            </dl>
          ) : <p className="muted small">No executions yet.</p>}
        </section>
      </div>

      <section className="card">
        <h3>Executions</h3>
        {pipeline.runs.length ? (
          <table className="runs">
            <thead>
              <tr><th>Started</th><th>Execution</th><th>Submitted by</th><th>Commit</th><th>AUC</th><th>Gate</th><th>Result</th><th>Time</th></tr>
            </thead>
            <tbody>
              {pipeline.runs.map((r) => {
                const v = version(r.model_package_arn);
                return (
                  <tr key={r.execution_arn ?? r.started_at} className={r.execution_arn === runArn ? "active" : ""}
                    onClick={() => setRunArn(r.execution_arn)}>
                    <td>{when(r.started_at)}</td>
                    <td><code>{execId(r.execution_arn)}</code></td>
                    <td>{r.submitted_by ?? "—"}</td>
                    <td><code>{short(r.commit, 8)}</code>{r.shuffle_labels && <span className="pill rejected">shuffled labels</span>}</td>
                    <td>{r.metrics.auc !== undefined ? r.metrics.auc.toFixed(3) : "—"}</td>
                    <td>{r.gate ? <span className={`status ${r.gate === "passed" ? "ok" : "bad"}`}>{r.gate}</span> : r.status}</td>
                    <td>{v ? `package v${v}` : r.gate === "failed" ? "no candidate" : "—"}</td>
                    <td>{secs(runDuration(r))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="muted small">No executions yet.</p>}
      </section>
    </div>
  );
}
