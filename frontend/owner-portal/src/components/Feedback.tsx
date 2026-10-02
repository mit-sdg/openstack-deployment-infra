import { useEffect, useRef, type ReactNode } from 'react';

export function ErrorNotice({ error }: { error: unknown }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (error) ref.current?.focus();
  }, [error]);
  return error ? (
    <div ref={ref} tabIndex={-1} className="notice notice-error" role="alert">
      {error instanceof Error ? error.message : String(error)}
    </div>
  ) : null;
}

export function Loading() {
  return (
    <div className="card empty" role="status" aria-busy="true">
      <span className="spinner" />
      Loading your application workspace…
    </div>
  );
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-mark" aria-hidden="true">
        ◇
      </span>
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}
