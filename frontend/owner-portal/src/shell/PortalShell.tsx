import type { ReactNode } from 'react';
import { ShellFrame } from '@openstack-platform/ui';
import { Link, useLocation } from 'wouter';
import type { Session } from '../api';
import { Mark } from '../components/Mark';
import { ThemeButton } from '../components/ThemeButton';
import { time } from '../utils/presentation';

type Props = {
  signIn: boolean;
  user?: Session['user'];
  role?: Session['role'];
  expiresAt?: string;
  logout: () => void;
  loggingOut: boolean;
  children: ReactNode;
};
export function PortalShell({
  signIn,
  user,
  role,
  expiresAt,
  logout,
  loggingOut,
  children,
}: Props) {
  const [location] = useLocation();
  const elevated = role === 'staff' || role === 'admin';
  return (
    <ShellFrame
      mainClass={`page ${signIn ? 'page-sign-in' : ''}`}
      brand={
        <Link href="/apps" className="brand">
          <span className="brand-mark">
            <Mark />
          </span>
          <span>
            <strong>Owner portal</strong>
            <small>Your ideas, running.</small>
          </span>
        </Link>
      }
      actions={
        <>
          <ThemeButton />
          {user && !signIn && (
            <>
              <span className="owner-name">{user.displayName}</span>
              <button className="button button-small" onClick={logout} disabled={loggingOut}>
                Sign out
              </button>
            </>
          )}
        </>
      }
      footer={
        <>
          <span>Owner workspace</span>
          <span>Built for your next idea.</span>
        </>
      }
    >
      {user && !signIn && (
        <>
          <nav className="app-nav" aria-label="Portal pages">
            <Link href="/apps">My applications</Link>
            {elevated && <Link href="/staff/owners">Staff catalog</Link>}
            {role === 'admin' && (
              <>
                <Link href="/admin/accounts">Accounts</Link>
                <Link href="/admin/audit">Audit</Link>
              </>
            )}
          </nav>
          {location.startsWith('/staff') && elevated && (
            <nav className="app-nav" aria-label="Staff pages">
              <Link href="/staff/owners">Owners</Link>
              <Link href="/staff/apps">Applications</Link>
              <Link href="/staff/operations">Operations</Link>
            </nav>
          )}
          <p className="field-help">
            {role} session · Expires {time(expiresAt)}
            {location.startsWith('/staff') && ' · Catalog reads are read only'}
          </p>
        </>
      )}
      {children}
    </ShellFrame>
  );
}
