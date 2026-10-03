import type { ReactNode } from 'react';
import { ShellFrame } from '@openstack-platform/ui';
import { Link } from 'wouter';
import type { Session } from '../api';
import { Mark } from '../components/Mark';
import { ThemeButton } from '../components/ThemeButton';

type Props = {
  signIn: boolean;
  user?: Session['user'];
  logout: () => void;
  loggingOut: boolean;
  children: ReactNode;
};
export function PortalShell({ signIn, user, logout, loggingOut, children }: Props) {
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
      {children}
    </ShellFrame>
  );
}
