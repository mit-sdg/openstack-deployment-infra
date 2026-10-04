import {
  AccountMenu,
  AppShell,
  Badge,
  Brand,
  Icon,
  TabNav,
  menuItemClass,
  navLinkClass,
  tabClass,
} from '@openstack-platform/ui';
import { useEffect, type ReactNode } from 'react';
import { Link, useLocation } from 'wouter';
import type { Session } from '../api';
import { Mark } from '../components/Mark';
import { ThemeButton } from '../components/ThemeButton';

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

const appsItem: Item = {
  label: 'Apps',
  href: '/apps',
  active: (path) => path === '/' || path.startsWith('/apps'),
};
const staffItem: Item = {
  label: 'Staff',
  href: '/staff/owners',
  active: (path) => path.startsWith('/staff'),
};
const adminItem: Item = {
  label: 'Admin',
  href: '/admin/apps',
  active: (path) => path.startsWith('/admin'),
};

// Section tabs shown under the header. Labels differ from the main "Apps"
// link so every link on a page has a unique name.
const staffTabs: Item[] = [
  { label: 'Owners', href: '/staff/owners', active: (path) => path.startsWith('/staff/owners') },
  { label: 'All apps', href: '/staff/apps', active: (path) => path.startsWith('/staff/apps') },
  {
    label: 'Activity',
    href: '/staff/operations',
    active: (path) => path.startsWith('/staff/operations'),
  },
];
const adminTabs: Item[] = [
  { label: 'All apps', href: '/admin/apps', active: (path) => path.startsWith('/admin/apps') },
  {
    label: 'Accounts',
    href: '/admin/accounts',
    active: (path) => path.startsWith('/admin/accounts'),
  },
  { label: 'Audit log', href: '/admin/audit', active: (path) => path.startsWith('/admin/audit') },
];
const roleNames = { owner: 'Owner', staff: 'Staff', admin: 'Admin' } as const;

/** Document title for a route: "<section> · <platform>". */
export function pageTitle(path: string, platformName?: string) {
  const section =
    path === '/sign-in' || path === '/signin'
      ? 'Sign in'
      : path === '/setup' || path === '/activate'
        ? 'Set up your account'
        : path.startsWith('/staff')
          ? 'Staff'
          : path.startsWith('/admin')
            ? 'Admin'
            : 'Apps';
  if (!platformName) return section;
  return section === 'Apps' ? platformName : `${section} · ${platformName}`;
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
  const signedIn = !!user && !signIn;
  const items = signedIn
    ? [
        appsItem,
        ...(role === 'staff' || role === 'admin' ? [staffItem] : []),
        ...(role === 'admin' ? [adminItem] : []),
      ]
    : [];
  const tabs = !signedIn
    ? null
    : location.startsWith('/staff') && role !== 'owner'
      ? { label: 'Staff', items: staffTabs }
      : location.startsWith('/admin') && role === 'admin'
        ? { label: 'Admin', items: adminTabs }
        : null;
  useEffect(() => {
    document.title = pageTitle(location, platformName);
  }, [location, platformName]);
  const links = (onlyMultiple: boolean) =>
    items.length > (onlyMultiple ? 1 : 0)
      ? items.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={navLinkClass(item.active(location))}
            aria-current={item.active(location) ? 'page' : undefined}
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
      nav={links(true)}
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
      subnav={
        tabs && (
          <TabNav label={`${tabs.label} pages`}>
            {tabs.items.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={tabClass(item.active(location))}
                aria-current={item.active(location) ? 'page' : undefined}
              >
                {item.label}
              </Link>
            ))}
          </TabNav>
        )
      }
    >
      {children}
    </AppShell>
  );
}
