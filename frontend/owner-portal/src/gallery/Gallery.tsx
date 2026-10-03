import {
  AccountMenu,
  Alert,
  AppShell,
  AuthLayout,
  Badge,
  Brand,
  Button,
  Card,
  Checkbox,
  CodeBlock,
  CopyField,
  CopyId,
  Icon,
  RelativeTime,
  StatusText,
  buttonClass,
  DataTable,
  Dialog,
  EmptyState,
  Field,
  Fieldset,
  Grid,
  Hint,
  IconButton,
  InlineStatus,
  Input,
  KeyValueList,
  List,
  ListItem,
  LoadingRows,
  Page,
  PageHeader,
  PasswordInput,
  Radio,
  Section,
  SegmentedControl,
  Select,
  Skeleton,
  Stack,
  Switch,
  TabNav,
  Textarea,
  ThemeToggle,
  ToastProvider,
  backLinkClass,
  menuItemClass,
  navLinkClass,
  tabClass,
  useToast,
  type Column,
  type Tone,
} from '@openstack-platform/ui';
import { useState, type ReactNode } from 'react';
import { Mark } from '../components/Mark';
import { Status } from '../components/Status';

const tones: Tone[] = ['neutral', 'info', 'success', 'warning', 'danger'];
type Row = { name: string; state: string; url: string; commit: string; when: string };
const rows: Row[] = [
  {
    name: 'student-project',
    state: 'healthy',
    url: 'student-project.apps.example.com',
    commit: 'a1b2c3d4e',
    when: '2 minutes ago',
  },
  {
    name: 'second-app-with-a-longer-name',
    state: 'creating',
    url: '—',
    commit: 'Not deployed',
    when: '—',
  },
];
type Owner = { id: string; name: string; username: string; enabled: boolean; seen: string };
const ago = (minutes: number) => new Date(Date.now() - minutes * 60000).toISOString();
const owners: Owner[] = [
  {
    id: '22222222-2222-4222-8222-222222222222',
    name: 'Alice Student',
    username: 'alice',
    enabled: true,
    seen: ago(21),
  },
  {
    id: '33333333-3333-4333-8333-333333333333',
    name: 'Bob Student',
    username: 'bob',
    enabled: false,
    seen: ago(60 * 26),
  },
];
// Compact phone rows: name and status on one line, username and time under it.
const ownerColumns: Column<Owner>[] = [
  {
    key: 'name',
    header: 'Name',
    mobile: 'title',
    cell: (owner) => (
      <a href="#data" className="ui-link ui-link--plain">
        {owner.name}
      </a>
    ),
  },
  {
    key: 'status',
    header: 'Status',
    mobile: 'trailing',
    cell: (owner) => <Status state={owner.enabled ? 'active' : 'disabled'} />,
  },
  {
    key: 'username',
    header: 'Username',
    mobile: 'secondary',
    cell: (owner) => <span className="ui-text-muted">{owner.username}</span>,
  },
  {
    key: 'id',
    header: 'ID',
    mobile: 'hidden',
    cell: (owner) => <CopyId value={owner.id} label="owner ID" />,
  },
  {
    key: 'seen',
    header: 'Last sign-in',
    mobile: 'meta',
    cell: (owner) => (
      <span className="ui-text-muted">
        <RelativeTime value={owner.seen} />
      </span>
    ),
  },
];
const deploymentStates = [
  'queued',
  'building',
  'deploying',
  'live',
  'succeeded',
  'failed',
  'recovery_required',
  'rolled_back',
];

const columns: Column<Row>[] = [
  {
    key: 'name',
    header: 'Name',
    mobile: 'title',
    cell: (row) => (
      <a href="#table" className="ui-link ui-link--plain">
        {row.name}
      </a>
    ),
  },
  {
    key: 'status',
    header: 'Status',
    mobile: 'trailing',
    cell: (row) => <Status state={row.state} />,
  },
  { key: 'url', header: 'URL', cell: (row) => <span className="ui-text-muted">{row.url}</span> },
  { key: 'commit', header: 'Deployed commit', cell: (row) => <code>{row.commit}</code> },
  {
    key: 'when',
    header: 'Last deployed',
    cell: (row) => <span className="ui-text-muted">{row.when}</span>,
  },
];

function Example({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="gallery-row">
      <span className="gallery-label">{label}</span>
      {children}
    </div>
  );
}

function ToastButton() {
  const toast = useToast();
  return <Button onClick={() => toast('Variable saved')}>Show toast</Button>;
}

export function Gallery() {
  const [method, setMethod] = useState<'a' | 'b'>('a');
  const [runtime, setRuntime] = useState('node');
  const [dialog, setDialog] = useState(false);
  return (
    <ToastProvider>
      <AppShell
        brand={
          <a href="#top">
            <Brand mark={<Mark />} name="Example Platform" />
          </a>
        }
        nav={
          <>
            <a href="#top" className={navLinkClass(true)} aria-current="page">
              Apps
            </a>
            <a href="#top" className={navLinkClass(false)}>
              Staff
            </a>
          </>
        }
        actions={
          <>
            <ThemeToggle storageKey="owner-portal-theme" />
            <AccountMenu
              name="Alice Student"
              detail="alice"
              badge={
                <Badge tone="info" dot={false}>
                  Admin
                </Badge>
              }
            >
              <button type="button" className={menuItemClass}>
                Sign out
              </button>
            </AccountMenu>
          </>
        }
        subnav={
          <TabNav label="Gallery sections">
            <a href="#type" className={tabClass(true)} aria-current="page">
              Foundations
            </a>
            <a href="#controls" className={tabClass(false)}>
              Controls
            </a>
            <a href="#data" className={tabClass(false)}>
              Data
            </a>
            <a href="#feedback" className={tabClass(false)}>
              Feedback
            </a>
          </TabNav>
        }
      >
        <Page>
          <PageHeader
            title="Design system"
            back={
              <a href="#top" className={backLinkClass}>
                ← Apps
              </a>
            }
            meta={
              <Badge tone="neutral" dot={false}>
                1 of 2
              </Badge>
            }
            actions={
              <>
                <Button variant="secondary">Secondary</Button>
                <Button variant="primary" icon="plus">
                  Create app
                </Button>
              </>
            }
          />

          <Section title="Type and color" id="type">
            <div className="gallery-type">
              <p className="gallery-t-2xl">Page title 24/32 semibold</p>
              <p className="gallery-t-xl">Title 20/28 semibold</p>
              <p className="gallery-t-lg">Section title 16/24 semibold</p>
              <p className="gallery-t-md gallery-t-medium">
                Body medium 14/20 — labels, buttons, links
              </p>
              <p className="gallery-t-md">
                Body 14/20 regular. The quick brown fox jumps over the lazy dog. 0123456789
              </p>
              <p className="ui-text-sm ui-text-muted">Small 13/20 muted — hints, meta</p>
              <p className="ui-text-xs ui-text-subtle">Extra small 12/16 subtle — table headers</p>
              <p>
                Inline code <code>DATABASE_URL</code> and commit <code>a1b2c3d4e</code>.
              </p>
            </div>
            <div className="gallery-swatches">
              {[
                ['bg', 'Page'],
                ['surface', 'Surface'],
                ['surface-subtle', 'Surface subtle'],
                ['surface-active', 'Surface active'],
                ['border', 'Border strong'],
                ['text', 'Text'],
                ['text-muted', 'Text muted'],
                ['text-subtle', 'Text subtle'],
                ['accent', 'Accent'],
                ['accent-subtle', 'Accent subtle'],
                ['success', 'Success'],
                ['warning', 'Warning'],
                ['danger', 'Danger'],
              ].map(([key, name]) => (
                <span className="gallery-swatch" key={key}>
                  <span className={`sw-${key}`} />
                  <span>{name}</span>
                </span>
              ))}
            </div>
          </Section>

          <Section title="Buttons" id="controls">
            <Example label="Medium">
              <Button variant="primary">Primary</Button>
              <Button variant="secondary">Secondary</Button>
              <Button variant="ghost">Ghost</Button>
              <Button variant="danger">Delete</Button>
              <Button variant="primary" icon="plus">
                With icon
              </Button>
              <IconButton icon="copy" label="Copy" />
            </Example>
            <Example label="Small">
              <Button variant="primary" size="sm">
                Primary
              </Button>
              <Button variant="secondary" size="sm">
                Secondary
              </Button>
              <Button variant="ghost" size="sm">
                Ghost
              </Button>
              <Button variant="danger" size="sm">
                Delete
              </Button>
            </Example>
            <Example label="Loading">
              <Button variant="primary" loading>
                Deploy
              </Button>
              <Button variant="secondary" loading>
                Save
              </Button>
            </Example>
            <Example label="Disabled">
              <Button variant="primary" disabled>
                Primary
              </Button>
              <Button variant="secondary" disabled>
                Secondary
              </Button>
              <Button variant="ghost" disabled>
                Ghost
              </Button>
              <Button variant="danger" disabled>
                Delete
              </Button>
            </Example>
            <Example label="Equal heights">
              <div className="gallery-align">
                <Button variant="secondary">Button</Button>
                <Input aria-label="Input" defaultValue="Input" />
                <Select aria-label="Select" defaultValue="a">
                  <option value="a">Select</option>
                </Select>
                <SegmentedControl
                  label="Segmented"
                  hideLabel
                  name="align"
                  value={method}
                  onChange={setMethod}
                  options={[
                    { value: 'a', label: 'One' },
                    { value: 'b', label: 'Two' },
                  ]}
                />
              </div>
            </Example>
          </Section>

          <Section
            title="Form fields"
            footer={
              <>
                <InlineStatus>Saved</InlineStatus>
                <Button variant="secondary">Cancel</Button>
                <Button variant="primary">Save settings</Button>
              </>
            }
          >
            <Stack gap={6}>
              <Grid columns={2}>
                <Field label="Repository URL" hint="Public GitHub repositories only.">
                  <Input placeholder="https://github.com/owner/repo" />
                </Field>
                <Field label="Health path" error="Start the path with a slash.">
                  <Input defaultValue="health" />
                </Field>
                <Field label="Build script" optional>
                  <Input defaultValue="build" />
                </Field>
                <Field label="Account role">
                  <Select defaultValue="owner">
                    <option value="owner">Owner</option>
                    <option value="staff">Staff</option>
                  </Select>
                </Field>
                <Field label="Password">
                  <PasswordInput defaultValue="fixture-password" />
                </Field>
                <Field label="Disabled">
                  <Input disabled defaultValue="Can't edit" />
                </Field>
              </Grid>
              <Field label="Package directories" hint="One directory per line.">
                <Textarea defaultValue="." />
              </Field>
              <CopyField label="Account setup link" value="https://example.com/setup#ID.TOKEN" />
              <Fieldset legend="Runtime" variant="cards">
                <Radio
                  name="runtime"
                  label="Node.js"
                  description="Uses package-lock.json"
                  checked={runtime === 'node'}
                  onChange={() => setRuntime('node')}
                />
                <Radio
                  name="runtime"
                  label="Bun"
                  description="Uses bun.lock"
                  checked={runtime === 'bun'}
                  onChange={() => setRuntime('bun')}
                />
              </Fieldset>
              <Fieldset legend="Options" hint="Pick any.">
                <Checkbox label="Checked" defaultChecked />
                <Checkbox label="Unchecked" description="With a description line." />
                <Checkbox label="Disabled" disabled />
                <Radio name="r" label="Radio option" defaultChecked />
              </Fieldset>
              <Switch
                label="Start automatically"
                description="Restart the app after the server reboots."
                defaultChecked
              />
              <SegmentedControl
                label="Sign-in method"
                name="method"
                value={method}
                onChange={setMethod}
                options={[
                  { value: 'a', label: 'Class account' },
                  { value: 'b', label: 'Local account' },
                ]}
              />
              <Hint>Help text in the one consistent style.</Hint>
            </Stack>
          </Section>

          <Section title="Table" flush id="data">
            <DataTable
              label="Apps"
              columns={columns}
              rows={rows}
              rowKey={(row) => row.name}
              onRowClick={() => {}}
            />
          </Section>
          <Section title="Compact list" flush actions={<Button size="sm">View all</Button>}>
            <List label="Activity" density="compact">
              <ListItem
                title="Deploy"
                meta={
                  <>
                    <a href="#data" className="ui-link">
                      student-project
                    </a>
                    <code>a1b2c3d4e</code>
                    <time>2 minutes ago</time>
                  </>
                }
              />
              <ListItem
                title="Set environment variable"
                meta={
                  <>
                    <a href="#data" className="ui-link">
                      student-project
                    </a>
                    <code>API_TOKEN</code>
                    <span>Building</span>
                    <time>just now</time>
                  </>
                }
                trailing={<Status state="accepted" />}
              />
              <ListItem
                title="Deploy"
                meta={
                  <>
                    <a href="#data" className="ui-link">
                      student-project
                    </a>
                    <time>1 hour ago</time>
                  </>
                }
                trailing={
                  <>
                    <Button size="sm">Resume</Button>
                    <Status state="failed" />
                  </>
                }
              >
                <p className="ui-text-danger ui-text-sm">The build failed. Check the build log.</p>
              </ListItem>
            </List>
          </Section>
          <Grid columns={2}>
            <Section title="Default list" flush>
              <List label="Owners">
                <ListItem
                  title="Alice Student"
                  meta={
                    <>
                      <span>alice</span>
                      <span>2 apps</span>
                    </>
                  }
                  trailing={<Badge tone="success">Enabled</Badge>}
                />
                <ListItem
                  title="Bob Student"
                  meta={
                    <>
                      <span>bob</span>
                      <span>No apps</span>
                    </>
                  }
                  trailing={<Badge>Disabled</Badge>}
                />
              </List>
            </Section>
            <Section title="Key-value list">
              <KeyValueList
                items={[
                  { label: 'Repository', value: 'github.com/example/student-app' },
                  { label: 'Branch', value: 'main' },
                  { label: 'Runtime', value: 'Node.js' },
                ]}
              />
            </Section>
          </Grid>
          <Section
            title="Owners"
            flush
            actions={
              <a href="#data" className={buttonClass({ variant: 'ghost', size: 'sm' })}>
                View all
                <Icon name="chevron-right" />
              </a>
            }
          >
            <DataTable
              label="Owners"
              columns={ownerColumns}
              rows={owners}
              rowKey={(owner) => owner.id}
              onRowClick={() => {}}
            />
          </Section>
          <Section title="Statuses and values">
            <Example label="Expected">
              <StatusText>Healthy</StatusText>
              <Status state="active" />
              <Status state="live" />
            </Example>
            <Example label="Deployments">
              {deploymentStates.map((state) => (
                <Status key={state} state={state} />
              ))}
            </Example>
            <Example label="IDs and times">
              <CopyId value="22222222-2222-4222-8222-222222222222" label="owner ID" />
              <CopyId value="a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0" label="commit" length={9} />
              <RelativeTime value={ago(21)} />
              <RelativeTime value={null} empty="Never" />
            </Example>
          </Section>
          <Section title="Code and log">
            <CodeBlock label="Example">{'npm ci\nnpm run build'}</CodeBlock>
            <CodeBlock label="Build log" variant="log">
              {Array.from(
                { length: 8 },
                (_, i) => `[00:0${i}] Step ${i + 1} of 8: building exact source snapshot`,
              ).join('\n')}
            </CodeBlock>
          </Section>

          <Section title="Feedback" id="feedback">
            <Example label="Badges">
              {tones.map((tone) => (
                <Badge key={tone} tone={tone}>
                  {tone[0]!.toUpperCase() + tone.slice(1)}
                </Badge>
              ))}
            </Example>
            {tones.slice(1).map((tone) => (
              <Alert
                key={tone}
                tone={tone}
                title={tone === 'danger' ? 'Deployment failed' : undefined}
              >
                {tone === 'danger'
                  ? 'The health check did not pass. Check the build log, then deploy again.'
                  : `An ${tone} message says what happened and what to do.`}
              </Alert>
            ))}
            <Example label="Overlays">
              <Button onClick={() => setDialog(true)}>Open dialog</Button>
              <ToastButton />
            </Example>
            <Card>
              <Stack gap={3}>
                <Skeleton variant="title" width="quarter" />
                <Skeleton width="three-quarters" />
                <Skeleton width="half" />
              </Stack>
            </Card>
            <div className="ui-card">
              <LoadingRows />
            </div>
          </Section>
          <div className="ui-card">
            <EmptyState
              title="Create your first app"
              action={
                <Button variant="primary" icon="plus">
                  Create app
                </Button>
              }
            >
              Connect a GitHub repository, then deploy any commit.
            </EmptyState>
          </div>
          <div className="ui-card">
            <AuthLayout title="Sign in to Example Platform" mark={<Mark />}>
              <Field label="Username">
                <Input />
              </Field>
              <Button variant="primary" block>
                Sign in
              </Button>
            </AuthLayout>
          </div>
        </Page>
        <Dialog
          open={dialog}
          onClose={() => setDialog(false)}
          title="Delete environment variable"
          footer={
            <>
              <Button onClick={() => setDialog(false)}>Cancel</Button>
              <Button variant="danger" onClick={() => setDialog(false)}>
                Delete variable
              </Button>
            </>
          }
        >
          <p>
            Delete <code>API_TOKEN</code>? The app restarts without it.
          </p>
        </Dialog>
      </AppShell>
    </ToastProvider>
  );
}
