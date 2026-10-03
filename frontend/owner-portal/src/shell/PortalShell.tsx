import type { ReactNode } from 'react';
import { ShellFrame } from '@openstack-platform/ui';
import { Link } from 'wouter';
import type { Session } from '../api';
import { Mark } from '../components/Mark';
import { ThemeButton } from '../components/ThemeButton';
import { time } from '../utils/presentation';

type Props = {
  signIn: boolean;
  user?: Session['user'];
  logout: () => void;
  loggingOut: boolean;
  children: ReactNode;
  staff?: boolean;
  expiresAt?: string;
};
export function PortalShell({
  signIn,
  user,
  logout,
  loggingOut,
  children,
  staff,
  expiresAt,
}: Props) {
  return (

    <ShellFrame
      mainClass={`page ${signIn ? 'page-sign-in' : ''}`}
      brand={
        <Link href={staff ? '/staff/owners' : '/apps'} className="brand">
          <span className="brand-mark">
            <Mark />
          </span>
          <span>
            <strong>Owner portal</strong>
            <small>{staff ? 'Staff view · Read only' : 'Your ideas, running.'}</small>
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
          <span>{staff ? 'Staff view · Read only' : 'Owner workspace'}</span>
          <span>Built for your next idea.</span>
        </>
      }
    >
      {staff && !signIn && <><nav className="app-nav" aria-label="Staff pages"><Link href="/staff/owners">Owners</Link><Link href="/staff/apps">Applications</Link><Link href="/staff/operations">Operations</Link></nav><p className="field-help">Read-only catalog · Session expires {time(expiresAt)}</p></>}
      {children}
    </ShellFrame>

  );
}
