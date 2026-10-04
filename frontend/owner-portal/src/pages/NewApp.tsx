import {
  Button,
  ErrorAlert,
  Field,
  Icon,
  Input,
  Page,
  PageHeader,
  Section,
  backLinkClass,
  buttonClass,
} from '@openstack-platform/ui';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link, useLocation } from 'wouter';
import { api } from '../api';

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
  function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!/^[a-z][a-z0-9-]{1,38}[a-z0-9]$/.test(slug) || slug.includes('--')) {
      setError(
        'Use 3 to 40 lowercase letters, numbers and single hyphens, starting with a letter and ending with a letter or number.',
      );
      return;
    }
    create.mutate({ key: crypto.randomUUID() });
  }
  return (
    <Page width="narrow">
      <PageHeader
        title="Create app"
        back={
          <Link href="/apps" className={backLinkClass}>
            <Icon name="arrow-left" />
            Apps
          </Link>
        }
      />
      <form onSubmit={submit} noValidate>
        <Section
          aria-label="New app"
          footer={
            <>
              <Link href="/apps" className={buttonClass()}>
                Cancel
              </Link>
              <Button type="submit" variant="primary" loading={create.isPending}>
                Create app
              </Button>
            </>
          }
        >
          <Field
            label="App name"
            id="slug"
            error={error}
            hint="3 to 40 lowercase letters, numbers and single hyphens, starting with a letter. It becomes part of your app’s URL and can’t be changed."
          >
            <Input
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
              placeholder="my-app"
              required
              maxLength={40}
            />
          </Field>
          <ErrorAlert error={create.error} />
        </Section>
      </form>
    </Page>
  );
}
