import { Link, Route, Switch } from 'wouter';
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
import { PortalShell } from './shell/PortalShell';
export function App() {
  const { signIn, session, logout } = useSession();
  return (
    <PortalShell
      signIn={signIn}
      user={session.data?.user}
      staff={session.data?.kind === 'staff_read'}
      expiresAt={session.data?.expiresAt}
      logout={() => logout.mutate()}
      loggingOut={logout.isPending}
    >
      {signIn ? (
        <SignIn />
      ) : session.isPending ? (
        <Loading />
      ) : session.error ? (
        <ErrorNotice error={session.error} />
      ) : session.data?.kind === 'staff_read' ? (
        <StaffPages userId={session.data.user.id} />
      ) : (
        <Switch>
          <Route path="/staff/:rest*">
            <Empty title="Staff access unavailable">
              Re-enter your credentials through <Link href="/signin?mode=staff">Staff sign-in</Link>{' '}
              to access the read-only view.
            </Empty>
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
