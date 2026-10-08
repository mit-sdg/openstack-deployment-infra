import {
  AccountMenu,
  AppShell,
  Badge,
  Brand,
  Icon,
  menuItemClass,
  navLinkClass,
} from '@openstack-platform/ui';
import { useEffect, type ReactNode } from 'react';
import { Link, useLocation } from 'wouter';
import type { Session } from '../api';
import { Mark } from '../components/Mark';
import { ThemeButton } from '../components/ThemeButton';
import { useAppDestination } from '../hooks/useAppDestination';

type Props = {
  signIn: boolean;
  user?: Session['user'];
  role?: Session['role'];
  platformName?: string;
  logout: () => void;
  loggingOut: boolean;
  children: ReactNode;
};

type Item = { label: string; href: string; active: (path: string) => boolean };

const destinations: Item[] = [
  { label: 'My apps', href: '/apps', active: (path) => path === '/' || path.startsWith('/apps') },
  { label: 'All apps', href: '/all-apps', active: (path) => path.startsWith('/all-apps') },
  { label: 'People', href: '/people', active: (path) => path.startsWith('/people') },
  { label: 'Activity', href: '/activity', active: (path) => path.startsWith('/activity') },
  {
    label: 'Platform settings',
    href: '/platform-settings',
    active: (path) => path.startsWith('/platform-settings'),
  },
  { label: 'Audit log', href: '/audit', active: (path) => path.startsWith('/audit') },
];

const roleNames = { owner: 'Owner', staff: 'Staff', admin: 'Admin' } as const;

/** Document title for a route: "<section> · <platform>". */
export function pageTitle(path: string, platformName?: string) {
  const section =
    path === '/sign-in' || path === '/signin'
      ? 'Sign in'
      : path === '/setup' || path === '/activate'
        ? 'Set up your account'
        : (destinations.find((item) => item.active(path))?.label ?? 'Page not found');
  return platformName ? `${section} · ${platformName}` : section;
}

export function PortalShell({
  signIn,
  user,
  role,
  platformName,
  logout,
  loggingOut,
  children,
}: Props) {
  const [location] = useLocation();
  const appId = /^\/apps\/([^/]+)/.exec(location)?.[1];
  const destination = useAppDestination(appId);
  const selectedLocation = appId && appId !== 'new' ? destination : location;
  const signedIn = !!user && !signIn;
  const elevated = role === 'staff' || role === 'admin';
  const items = signedIn
    ? destinations.filter(
        (item, index) =>
          index === 0 ||
          (item.href === '/audit' || item.href === '/platform-settings'
            ? role === 'admin'
            : elevated),
      )
    : [];
  useEffect(() => {
    document.title = pageTitle(selectedLocation, platformName);
  }, [selectedLocation, platformName]);
  const links = (onlyMultiple: boolean) =>
    items.length > (onlyMultiple ? 1 : 0)
      ? items.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={navLinkClass(item.active(selectedLocation))}
            aria-current={item.active(selectedLocation) ? 'page' : undefined}
          >
            {item.label}
          </Link>
        ))
      : undefined;
  return (
    <AppShell
      brand={
        <Link
          href={signedIn ? '/apps' : '/sign-in'}
          aria-label={platformName ? `${platformName} home` : 'Home'}
        >
          <Brand mark={<Mark />} name={platformName} />
        </Link>
      }
      nav={links(false)}
      actions={
        <>
          <ThemeButton />
          {signedIn && (
            <AccountMenu
              name={user.displayName}
              detail={user.username}
              badge={
                role && role !== 'owner' ? (
                  <Badge tone="info" dot={false}>
                    {roleNames[role]}
                  </Badge>
                ) : undefined
              }
            >
              <button
                type="button"
                className={menuItemClass}
                onClick={logout}
                disabled={loggingOut}
              >
                <Icon name="sign-out" />
                Sign out
              </button>
            </AccountMenu>
          )}
        </>
      }
    >
      {children}
    </AppShell>
  );
}
