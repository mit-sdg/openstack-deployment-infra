import {
  Button,
  ErrorAlert,
  Field,
  Fieldset,
  Hint,
  Input,
  PasswordInput,
  Radio,
  Dialog,
  LoadingRows,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useId, useState, type FormEvent } from 'react';
import { ApiError, type Session } from '../../api';
import { adminApi } from '../../adminApi';
import { appManagementApi, type AppOwner } from '../../appManagementApi';
import { authOptionsQuery } from '../../authOptions';
import './admin.css';

/** Thrown when someone closes the step-up dialog; never shown as an error. */
export class StepUpCanceled extends Error {
  constructor() {
    super('Canceled.');
  }
}

/** The error to show for a mutation, hiding a canceled step-up. */
export function shown(error: unknown) {
  return error instanceof StepUpCanceled ? null : error;
}

// Friendlier text for errors the admin pages commonly hit.
const messages: Record<string, string> = {
  ALREADY_OWNED: 'This app is already managed here. Find it in the list.',
  NOT_FOUND: 'No app or account has this ID. Check it and try again.',
  INVALID_OWNER: 'Choose an active, enabled account as the owner.',
  OWNER_CONFLICT: 'The owner changed while you were working. Reload the page and try again.',
  APP_BUSY: 'This app has changes in progress. Wait for them to finish, then try again.',
  ACCOUNT_UNAVAILABLE: 'This username is taken. Choose another one.',
  RATE_LIMITED: 'Too many requests right now. Wait a minute, then reload the page.',
};

/** Maps known error codes to plain copy; other errors keep the server's message. */
export function friendly(error: unknown) {
  const visible = shown(error);
  if (visible instanceof ApiError && messages[visible.code])
    return new Error(messages[visible.code]);
  return visible;
}

type Waiter = { resolve: () => void; reject: (error: Error) => void };

/**
 * Sensitive admin actions need a password and authentication code from the
 * last five minutes. run(action) asks for them first when the session's
 * window has passed, and again if the server answers STEP_UP_REQUIRED, then
 * continues the action. Render `dialog` once on the page.
 */
export function useStepUp() {
  const client = useQueryClient();
  const [waiter, setWaiter] = useState<Waiter | null>(null);
  const ask = useCallback(
    () => new Promise<void>((resolve, reject) => setWaiter({ resolve, reject })),
    [],
  );
  const run = useCallback(
    async <T,>(action: () => Promise<T>): Promise<T> => {
      const session = client.getQueryData<Session>(['session']);
      const until = session?.stepUpExpiresAt ? Date.parse(session.stepUpExpiresAt) : 0;
      let confirmed = false;
      if (!(until - Date.now() > 15_000)) {
        await ask();
        confirmed = true;
      }
      try {
        return await action();
      } catch (error) {
        if (confirmed || !(error instanceof ApiError) || error.code !== 'STEP_UP_REQUIRED')
          throw error;
        await ask();
        return action();
      }
    },
    [ask, client],
  );
  const dialog = (
    <StepUpDialog
      open={!!waiter}
      onCancel={() => {
        waiter?.reject(new StepUpCanceled());
        setWaiter(null);
      }}
      onConfirmed={(expiresAt) => {
        client.setQueryData<Session>(['session'], (current) =>
          current ? { ...current, stepUpExpiresAt: expiresAt } : current,
        );
        void client.invalidateQueries({ queryKey: ['session'] });
        waiter?.resolve();
        setWaiter(null);
      }}
    />
  );
  return { run, dialog };
}

function StepUpDialog({
  open,
  onCancel,
  onConfirmed,
}: {
  open: boolean;
  onCancel: () => void;
  onConfirmed: (expiresAt: string) => void;
}) {
  const [password, setPassword] = useState('');
  const [totp, setTotp] = useState('');
  const [visible, setVisible] = useState(false);
  const form = useId();
  const step = useMutation({
    mutationFn: async () => {
      try {
        return await adminApi.reauthenticate(password, totp);
      } finally {
        setPassword('');
        setTotp('');
        setVisible(false);
      }
    },
    onSuccess: (data) => onConfirmed(data.stepUpExpiresAt as string),
  });
  useEffect(() => {
    if (!open) step.reset();
  }, [open]);
  function submit(event: FormEvent) {
    event.preventDefault();
    step.mutate();
  }
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title="Confirm it’s you"
      size="sm"
      footer={
        <>
          <Button onClick={onCancel}>Cancel</Button>
          <Button type="submit" form={form} variant="primary" loading={step.isPending}>
            Confirm
          </Button>
        </>
      }
    >
      <form id={form} className="ui-stack ui-gap-4" onSubmit={submit}>
        <Hint>
          Enter your password and a new code from your authenticator app. You won’t be asked again
          for 5 minutes.
        </Hint>
        <ErrorAlert error={step.error} />
        <Field label="Password" id="step-password">
          <PasswordInput
            autoComplete="current-password"
            maxLength={1024}
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            revealed={visible}
            onRevealedChange={setVisible}
          />
        </Field>
        <Field label="Authentication code" id="step-code">
          <Input
            autoComplete="one-time-code"
            inputMode="numeric"
            maxLength={6}
            required
            value={totp}
            onChange={(event) => setTotp(event.target.value)}
          />
        </Field>
      </form>
    </Dialog>
  );
}

/** Value that settles after the person stops typing. */
export function useDebounced<T>(value: T, delay = 300) {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

/** Display name for the class sign-in method, as sentence case. */
export function useProviderLabel() {
  // Read-only view of the sign-in page's query; never fetched here.
  const options = useQuery({ ...authOptionsQuery, enabled: false });
  const label = options.data?.providerLabel ?? 'class account';
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/**
 * Search accounts by name and pick one. Only active, enabled accounts can
 * own apps, so others are shown but can't be chosen.
 */
export function OwnerPicker({
  value,
  onChange,
  optional = false,
  hint,
}: {
  value: string;
  onChange: (id: string) => void;
  optional?: boolean;
  hint?: string;
}) {
  const [search, setSearch] = useState('');
  const query = useDebounced(search.trim());
  const results = useQuery({
    queryKey: ['admin', 'owner-search', query],
    queryFn: () => appManagementApi.owners(query),
    enabled: !!query,
    retry: false,
  });
  const [chosen, setChosen] = useState<AppOwner | null>(null);
  const items = results.data?.items ?? [];
  return (
    <div className="ui-stack ui-gap-3">
      <Field label="Owner" id="owner-search" optional={optional} hint={hint}>
        <Input
          type="search"
          autoComplete="off"
          maxLength={64}
          placeholder="Search by name or username"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </Field>
      {chosen && value === chosen.userId && !items.some((a) => a.userId === value) && (
        <Hint>
          Selected: {chosen.displayName} ({chosen.username})
        </Hint>
      )}
      {query &&
        (results.isPending ? (
          <LoadingRows rows={2} label="Searching…" />
        ) : results.error ? (
          <ErrorAlert error={results.error} focus={false} />
        ) : items.length ? (
          <Fieldset legend="Matching accounts">
            {items.map((account) => {
              const usable = account.enabled && account.status === 'active';
              return (
                <Radio
                  key={account.userId}
                  name="owner"
                  label={account.displayName}
                  description={`${account.username} · ${usable ? roleNames[account.role] : account.enabled ? 'Setup pending' : 'Disabled'}`}
                  checked={value === account.userId}
                  disabled={!usable}
                  onChange={() => {
                    setChosen(account);
                    onChange(account.userId);
                  }}
                />
              );
            })}
          </Fieldset>
        ) : (
          <Hint>No accounts match “{query}”.</Hint>
        ))}
    </div>
  );
}

export const roleNames: Record<AppOwner['role'], string> = {
  owner: 'Owner',
  staff: 'Staff',
  admin: 'Admin',
};
