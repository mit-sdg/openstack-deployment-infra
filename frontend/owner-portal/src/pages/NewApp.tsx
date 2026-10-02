import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link, useLocation } from 'wouter';
import { api } from '../api';
import { ErrorNotice } from '../components/Feedback';

export function NewApp() {
  const [slug, setSlug] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [, navigate] = useLocation();
  const client = useQueryClient();
  const create = useMutation({
    mutationFn: ({ key }: { key: string }) => api.create(slug, key),
    onSuccess: (result) => {
      client.invalidateQueries({ queryKey: ['apps'] });
      navigate(`/apps/${result.app.applicationId}/configuration`);
    },
  });
  return (
    <>
      <Link href="/apps" className="back-link">
        ← My applications
      </Link>
      <div className="page-heading">
        <div>
          <span className="eyebrow">New project</span>
          <h1>Create an application</h1>
          <p>Give your project a permanent name. Connect your code next.</p>
        </div>
      </div>
      <form
        className="card form-card narrow"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          if (!/^[a-z][a-z0-9-]{1,38}[a-z0-9]$/.test(slug) || slug.includes('--')) {
            setError(
              'Use 3–40 lowercase letters, numbers, and interior hyphens. Start with a letter.',
            );
            return;
          }
          create.mutate({ key: crypto.randomUUID() });
        }}
      >
        <div className="form-section">
          <label htmlFor="slug">Application name</label>
          <input
            id="slug"
            autoComplete="off"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="my-next-project"
            required
            maxLength={40}
          />
          <p className="field-help">
            This name becomes part of your public URL and cannot be changed.
          </p>
          <div className="url-preview">
            <span className="overline">Your application name</span>
            <code>{slug || 'my-next-project'}</code>
            <small>The platform assigns your permanent address after creation.</small>
          </div>
          <ErrorNotice error={error ?? create.error} />
        </div>
        <div className="form-footer">
          <Link href="/apps" className="button">
            Cancel
          </Link>
          <button className="button button-primary" disabled={create.isPending}>
            {create.isPending ? 'Creating…' : 'Create application'}
          </button>
        </div>
      </form>
    </>
  );
}
