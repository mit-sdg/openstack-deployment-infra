import type { ReactNode } from 'react';
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
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <Link href="/apps" className="brand">
            <span className="brand-mark">
              <Mark />
            </span>
            <span>
              <strong>Owner portal</strong>
              <small>Your ideas, running.</small>
            </span>
          </Link>
          <div className="topbar-actions">
            <ThemeButton />
            {user && !signIn && (
              <>
                <span className="owner-name">{user.displayName}</span>
                <button className="button button-small" onClick={logout} disabled={loggingOut}>
                  Sign out
                </button>
              </>
            )}
          </div>
        </div>
      </header>
      <main id="main" className={`page ${signIn ? 'page-sign-in' : ''}`} tabIndex={-1}>
        {children}
      </main>
      <footer className="footer">
        <span>Owner workspace</span>
        <span>Built for your next idea.</span>
      </footer>
    </>
  );
}
