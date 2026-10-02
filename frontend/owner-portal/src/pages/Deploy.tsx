import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'wouter';
import { api } from '../api';
import { AppFrame } from '../components/AppFrame';
import { BoundaryText } from '../components/BoundaryText';
import { Empty, ErrorNotice, Loading } from '../components/Feedback';
import { Operation } from '../components/Operation';
import { useIntentPolling } from '../hooks/useIntentPolling';

export function DeployPage({ id }: { id: string }) {
  const settings = useQuery({ queryKey: ['settings', id], queryFn: () => api.settings(id) });
  const [sha, setSha] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [intentId, setIntentId] = useState<string | null>(null);
  const [review, setReview] = useState(false);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const client = useQueryClient();
  const intent = useIntentPolling(intentId);
  const deploy = useMutation({
    mutationFn: (key: string) => api.deploy(id, settings.data!.revision, sha, key),
    onSuccess: (result) => {
      setIntentId(result.intentId);
      setReview(false);
      client.invalidateQueries({ queryKey: ['intents'] });
    },
  });
  useEffect(() => {
    if (review) dialog.current?.showModal();
    else dialog.current?.close();
  }, [review]);
  return (
    <AppFrame id={id} active="Deploy">
      <div className="section-heading">
        <div>
          <h2>Deploy an exact commit</h2>
          <p>Review a fixed snapshot of your code and saved settings.</p>
        </div>
      </div>
      {settings.isPending ? (
        <Loading />
      ) : settings.error ? (
        <ErrorNotice error={settings.error} />
      ) : settings.data!.revision === 0 ? (
        <div className="card">
          <Empty title="Configure your application first">
            <Link href={`/apps/${id}/configuration`}>
              Save your repository and runtime settings →
            </Link>
          </Empty>
        </div>
      ) : (
        <div className="deploy-grid">
          <section className="card form-card">
            <div className="form-section">
              <h3>Choose your code</h3>
              <p className="muted">Copy the full commit SHA from your repository.</p>
              <label htmlFor="commit">Full commit SHA</label>
              <input
                id="commit"
                className="mono commit-input"
                value={sha}
                onChange={(e) => {
                  setSha(e.target.value);
                  setPendingKey(null);
                }}
                placeholder="40 lowercase hexadecimal characters"
                autoComplete="off"
                spellCheck={false}
                maxLength={40}
              />
              <p className="field-help">
                Deployments never follow a moving branch. The platform fetches this exact commit.
              </p>
              <ErrorNotice error={error ?? deploy.error} />
              <button
                className="button button-primary"
                onClick={() => {
                  if (!/^[a-f0-9]{40}$/.test(sha)) {
                    setError('Enter the full 40-character lowercase commit SHA.');
                    return;
                  }
                  setError(null);
                  setReview(true);
                }}
                disabled={deploy.isPending}
              >
                Review deployment →
              </button>
            </div>
          </section>
          <section className="card review-settings">
            <span className="overline">Saved configuration</span>
            <h3>Revision {settings.data!.revision}</h3>
            <dl className="kv">
              <dt>Repository</dt>
              <dd className="break-text">
                <BoundaryText text={settings.data!.repository} />
              </dd>
              <dt>Branch label</dt>
              <dd>{settings.data!.branch}</dd>
              <dt>Runtime</dt>
              <dd>{settings.data!.configuration.build.runtime === 'node' ? 'Node.js' : 'Bun'}</dd>
              <dt>Start script</dt>
              <dd className="mono">{settings.data!.configuration.build.startScript}</dd>
              <dt>Health path</dt>
              <dd className="mono">{settings.data!.configuration.runtime.healthPath}</dd>
            </dl>
            <Link href={`/apps/${id}/configuration`} className="text-link">
              Edit configuration →
            </Link>
          </section>
        </div>
      )}
      {intent.data && (
        <section className="section card">
          <div className="card-header">
            <h2>Deployment progress</h2>
          </div>
          <ul className="operation-list">
            <Operation intent={intent.data} />
          </ul>
          {intent.data.state === 'succeeded' && (
            <p className="accepted-message" role="status">
              Deployment succeeded. <Link href={`/apps/${id}`}>View application health →</Link>
            </p>
          )}
        </section>
      )}
      <dialog
        ref={dialog}
        className="review-dialog"
        aria-labelledby="review-title"
        onCancel={() => setReview(false)}
        onClose={() => setReview(false)}
      >
        <div className="dialog-header">
          <div>
            <span className="eyebrow">Confirm deployment</span>
            <h2 id="review-title">Ready to deploy?</h2>
          </div>
          <button
            className="icon-button"
            aria-label="Close deployment review"
            onClick={() => setReview(false)}
          >
            ×
          </button>
        </div>
        <div className="dialog-body">
          <p>The platform will build this exact commit and accept it after health checks pass.</p>
          <dl className="kv">
            <dt>Repository</dt>
            <dd className="break-text">
              <BoundaryText text={settings.data?.repository ?? ''} />
            </dd>
            <dt>Commit</dt>
            <dd className="mono break-text">{sha}</dd>
            <dt>Settings</dt>
            <dd>Revision {settings.data?.revision}</dd>
            <dt>Branch label</dt>
            <dd>{settings.data?.branch}</dd>
          </dl>
          <div className="notice">
            The branch is a label. This review does not prove the commit is its current head.
            Ordinary deployments may briefly run two versions of your app.
          </div>
          <ErrorNotice error={deploy.error} />
        </div>
        <div className="dialog-footer">
          <button className="button" onClick={() => setReview(false)}>
            Cancel
          </button>
          <button
            className="button button-primary"
            disabled={deploy.isPending}
            onClick={() => {
              const key = pendingKey ?? crypto.randomUUID();
              setPendingKey(key);
              deploy.mutate(key);
            }}
          >
            {deploy.isPending ? 'Submitting…' : 'Deploy this commit'}
          </button>
        </div>
      </dialog>
    </AppFrame>
  );
}
