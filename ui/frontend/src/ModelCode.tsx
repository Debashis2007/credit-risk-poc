import { useEffect, useMemo, useState } from "react";
import { api, ApiError, ModelCode, Proposal, Session } from "./api";

const PURPOSE: Record<string, string> = {
  "train.py": "Training: algorithm, features, hyperparameters",
  "evaluate.py": "Evaluation metrics and how they are computed",
  "preprocess.py": "Optional data preparation (steps.prepare.enabled)",
  "inference.py": "How a request becomes a prediction",
  "requirements.txt": "Python packages the model needs",
  "model.yaml": "Model configuration; only some fields can change here",
};

export default function ModelCodeView({ session }: { session: Session }) {
  const [code, setCode] = useState<ModelCode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [path, setPath] = useState("train.py");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);

  const load = () => {
    setError(null);
    api.code().then((c) => { setCode(c); setDrafts({}); }).catch((e) => setError((e as Error).message));
  };
  useEffect(load, []);

  const files = code?.files ?? [];
  const file = files.find((f) => f.path === path) ?? files[0];
  const original = (p: string) => files.find((f) => f.path === p)?.text ?? "";
  const changed = useMemo(
    () => Object.keys(drafts).filter((p) => drafts[p] !== original(p)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [drafts, files],
  );
  const canEdit = !!session.me?.can_train;
  const canPropose = canEdit && !!code?.can_propose;

  if (error) return <div className="notice error">{error}</div>;
  if (!code) return <div className="detail muted">Loading model code…</div>;
  if (!code.configured) {
    return <section className="card"><h3>Model code</h3><p className="muted small">This model has no GitHub repository in the model catalog.</p></section>;
  }

  const propose = async () => {
    setBusy(true);
    setRefusal(null);
    try {
      const body = Object.fromEntries(changed.map((p) => [p, drafts[p]]));
      setProposal(await api.proposeCode(body, title, description));
      setDrafts({});
      setTitle("");
      setDescription("");
    } catch (e) {
      const err = e as ApiError;
      setRefusal(`Refused (${err.status}): ${err.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h2>Model code</h2>
          <p className="muted small">
            <a href={code.url} target="_blank" rel="noreferrer">{code.repo}</a> · {code.branch}. Saving opens a pull request;
            nothing is committed to {code.branch} directly. The build workflow tests it and a reviewer merges it.
          </p>
        </div>
        <button className="ghost small" onClick={load} disabled={busy}>Reload</button>
      </header>

      {!canEdit && (
        <div className="notice warn">Your role can read the model code but not change it. Changing code needs a data scientist role.</div>
      )}
      {canEdit && !code.can_propose && (
        <div className="notice warn">
          The console has no GitHub token for pull requests (<code>MLP_GITHUB_PR_TOKEN</code>), so changes cannot be proposed yet.
        </div>
      )}
      {proposal && (
        <div className="notice ok">
          Pull request <a href={proposal.url} target="_blank" rel="noreferrer">#{proposal.number}</a> opened on branch{" "}
          <code>{proposal.branch}</code> with {proposal.files.join(", ")}. After it is reviewed and merged, the model trains and the new
          version appears under Approvals.
        </div>
      )}

      <div className="code-layout">
        <ul className="code-files">
          {files.map((f) => (
            <li key={f.path} className={f.path === file?.path ? "active" : ""} onClick={() => setPath(f.path)}>
              <div className="row">
                <code>{f.path}</code>
                {changed.includes(f.path) && <span className="pill pending">edited</span>}
                {!f.exists && !changed.includes(f.path) && <span className="pill rejected">not in repo</span>}
              </div>
              <div className="muted small">{PURPOSE[f.path]}</div>
            </li>
          ))}
        </ul>

        {file && (
          <section className="card code-editor">
            <div className="card-head">
              <h3><code>{file.path}</code></h3>
              {changed.includes(file.path) && (
                <button className="ghost small" onClick={() => setDrafts(({ [file.path]: _, ...rest }) => rest)}>Undo changes</button>
              )}
            </div>
            {file.path === "model.yaml" && (
              <p className="muted small">
                Editable here: {code.editable_config?.map((k) => <code key={k}>{k} </code>)}. Thresholds can only be raised.
                Everything else (ids, accounts, roles, registry group, deployment targets, approvers) is managed by the platform.
              </p>
            )}
            <textarea className="code" spellCheck={false} readOnly={!canEdit}
              value={drafts[file.path] ?? file.text}
              onChange={(e) => setDrafts((d) => ({ ...d, [file.path]: e.target.value }))} />
          </section>
        )}
      </div>

      {canEdit && (
        <section className="card">
          <h3>Propose {changed.length ? `${changed.length} change${changed.length === 1 ? "" : "s"}` : "changes"}</h3>
          <input className="wide" placeholder="Pull request title (optional)" value={title} onChange={(e) => setTitle(e.target.value)} />
          <textarea placeholder="What changed and why (optional)" value={description} onChange={(e) => setDescription(e.target.value)} />
          <div className="actions">
            <button className="primary" disabled={!canPropose || !changed.length || busy} onClick={propose}>
              {busy ? "Opening pull request…" : "Open pull request"}
            </button>
          </div>
          {refusal && <div className="notice error inline">{refusal}</div>}
        </section>
      )}
    </div>
  );
}
