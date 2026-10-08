import { buttonClass, Page, ToastProvider } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { Link, Route, Switch, useLocation } from 'wouter';
import { authOptionsQuery } from './authOptions';
import { EmptyState, ErrorNotice, PageSkeleton, QueryError } from './components/Feedback';
import { useSession } from './hooks/useSession';
import { ConfigurationPage } from './pages/Configuration';
import { Dashboard } from './pages/Dashboard';
import { DeployPage } from './pages/Deploy';
import { DeploymentPage } from './pages/Deployment';
import { HistoryPage } from './pages/History';
import { LogsPage } from './pages/Logs';
import { TeamPage } from './pages/Team';
import { NewApp } from './pages/NewApp';
import { Overview } from './pages/Overview';
import { SignIn } from './pages/SignIn';
import { StaffPages } from './pages/Staff';
import { AdminAppsPages } from './pages/AdminApps';
import { AccountsPage, AdminAuditPage } from './pages/Accounts';
import { Enrollment } from './pages/Enrollment';
import { PortalShell } from './shell/PortalShell';

function NoAccess() {
  return (
    <EmptyState title="You don't have access to this page" icon="lock">
      Ask an admin if you need access.
    </EmptyState>
  );
}

export function App() {
  const { signIn, session, logout } = useSession();
  const [location] = useLocation();
  // Read-only view of the sign-in page's query; the shell never fetches it.
  const options = useQuery({ ...authOptionsQuery, enabled: false });
  const role = session.data?.role;
  const elevated = role === 'staff' || role === 'admin';
  return (
    <ToastProvider>
      <PortalShell
        signIn={signIn}
        user={session.data?.user}
        role={role}
        platformName={session.data?.platformName ?? options.data?.platformName}
        logout={() => logout.mutate()}
        loggingOut={logout.isPending}
      >
        {location === '/setup' || location === '/activate' ? (
          <Enrollment key={location} />
        ) : signIn ? (
          <SignIn />
        ) : session.isPending ? (
          <PageSkeleton />
        ) : session.error ? (
          <Page>
            <QueryError query={session} what="your account" />
          </Page>
        ) : (
          <Switch>
            {/* Wildcards: in wouter 3 ":rest*" matches one segment only, which
                sent nested staff and admin pages to "Page not found". Staff
                have the same app-management permissions as admins. */}
            <Route path="/admin/apps/*">{elevated ? <AdminAppsPages /> : <NoAccess />}</Route>
            <Route path="/admin/apps">{elevated ? <AdminAppsPages /> : <NoAccess />}</Route>
            <Route path="/admin/accounts">
              {role === 'admin' ? <AccountsPage /> : <NoAccess />}
            </Route>
            <Route path="/admin/audit">
              {role === 'admin' ? <AdminAuditPage /> : <NoAccess />}
            </Route>
            <Route path="/staff/*">
              {elevated ? <StaffPages userId={session.data!.user.id} /> : <NoAccess />}
            </Route>
            <Route path="/apps/new">
              <NewApp />
            </Route>
            <Route path="/apps/:id/configuration">{(p) => <ConfigurationPage id={p.id} />}</Route>
            <Route path="/apps/:id/deploy">{(p) => <DeployPage id={p.id} />}</Route>
            <Route path="/apps/:id/deployments/:deployment">
              {(p) => <DeploymentPage id={p.id} deployment={p.deployment} />}
            </Route>
            <Route path="/apps/:id/deployments">{(p) => <HistoryPage id={p.id} />}</Route>
            <Route path="/apps/:id/logs">{(p) => <LogsPage id={p.id} />}</Route>
            <Route path="/apps/:id/team">{(p) => <TeamPage id={p.id} />}</Route>
            <Route path="/apps/:id">{(p) => <Overview id={p.id} />}</Route>
            <Route path="/apps">
              <Dashboard />
            </Route>
            <Route path="/">
              <Dashboard />
            </Route>
            <Route>
              <EmptyState
                title="Page not found"
                icon="search"
                action={
                  <Link href="/apps" className={buttonClass()}>
                    Go to apps
                  </Link>
                }
              >
                Check the address, or go back to your apps.
              </EmptyState>
            </Route>
          </Switch>
        )}
        <ErrorNotice error={logout.error} />
      </PortalShell>
    </ToastProvider>
  );
}
