import { Link, Route, Switch, useLocation } from 'wouter';
import { Empty, ErrorNotice, Loading } from './components/Feedback';
import { useSession } from './hooks/useSession';
import { ConfigurationPage } from './pages/Configuration';
import { Dashboard } from './pages/Dashboard';
import { DeployPage } from './pages/Deploy';
import { DeploymentPage } from './pages/Deployment';
import { HistoryPage } from './pages/History';
import { NewApp } from './pages/NewApp';
import { Overview } from './pages/Overview';
import { SignIn } from './pages/SignIn';
import { StaffPages } from './pages/Staff';
import { AccountsPage, AdminAuditPage } from './pages/Accounts';
import { Enrollment } from './pages/Enrollment';
import { PortalShell } from './shell/PortalShell';
export function App() {
  const { signIn, session, logout } = useSession();
  const [location] = useLocation();
  const elevated = session.data?.role === 'staff' || session.data?.role === 'admin';
  return (
    <PortalShell
      signIn={signIn}
      user={session.data?.user}
      role={session.data?.role}
      expiresAt={session.data?.expiresAt}
      logout={() => logout.mutate()}
      loggingOut={logout.isPending}
    >
      {location === '/setup' || location === '/activate' ? (
        <Enrollment key={location} />
      ) : signIn ? (
        <SignIn />
      ) : session.isPending ? (
        <Loading />
      ) : session.error ? (
        <ErrorNotice error={session.error} />
      ) : (
        <Switch>
          <Route path="/admin/accounts">
            {session.data?.role === 'admin' ? (
              <AccountsPage />
            ) : (
              <Empty title="Admin access unavailable">
                This account cannot manage portal accounts.
              </Empty>
            )}
          </Route>
          <Route path="/admin/audit">
            {session.data?.role === 'admin' ? (
              <AdminAuditPage />
            ) : (
              <Empty title="Admin access unavailable">
                This account cannot read the admin audit.
              </Empty>
            )}
          </Route>
          <Route path="/staff/:rest*">
            {elevated ? (
              <StaffPages userId={session.data!.user.id} />
            ) : (
              <Empty title="Staff access unavailable">
                This account cannot read the staff catalog.
              </Empty>
            )}
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
          <Route path="/apps/:id">{(p) => <Overview id={p.id} />}</Route>
          <Route path="/apps">
            <Dashboard />
          </Route>
          <Route path="/">
            <Dashboard />
          </Route>
          <Route>
            <Empty title="Page not found">
              Return to <Link href="/apps">My applications</Link>.
            </Empty>
          </Route>
        </Switch>
      )}
      <ErrorNotice error={logout.error} />
    </PortalShell>
  );
}
