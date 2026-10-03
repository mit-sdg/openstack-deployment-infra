import { EmptyState, buttonClass } from '@openstack-platform/ui';
import { Link, Route, Switch } from 'wouter';
import { StaffOperations } from './staff/Activity';
import { StaffAppPage, StaffApps } from './staff/Apps';
import { StaffContext, useActive } from './staff/common';
import { StaffDeploymentPage, StaffHistory } from './staff/Deployments';
import { StaffOwnerPage, StaffOwners } from './staff/Owners';

export function StaffPages({ userId }: { userId: string }) {
  const active = useActive();
  return (
    <StaffContext.Provider value={{ userId, active }}>
      <Switch>
        <Route path="/staff/owners/:id">{(p) => <StaffOwnerPage key={p.id} id={p.id} />}</Route>
        <Route path="/staff/owners">
          <StaffOwners />
        </Route>
        <Route path="/staff/apps/:id/deployments/:deployment">
          {(p) => <StaffDeploymentPage key={p.deployment} id={p.id} deployment={p.deployment} />}
        </Route>
        <Route path="/staff/apps/:id/deployments">
          {(p) => <StaffHistory key={p.id} id={p.id} />}
        </Route>
        <Route path="/staff/apps/:id">{(p) => <StaffAppPage key={p.id} id={p.id} />}</Route>
        <Route path="/staff/apps">
          <StaffApps />
        </Route>
        <Route path="/staff/operations">
          <StaffOperations />
        </Route>
        <Route path="/">
          <StaffOwners />
        </Route>
        <Route>
          <EmptyState
            title="Page not found"
            icon="search"
            action={
              <Link href="/staff/owners" className={buttonClass()}>
                Go to owners
              </Link>
            }
          >
            Check the address, or go back to the owner list.
          </EmptyState>
        </Route>
      </Switch>
    </StaffContext.Provider>
  );
}
