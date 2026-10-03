# Portal design system

The portal UI is built from the components in `frontend/shared`
(`@openstack-platform/ui`). The aim is a calm, consistent product UI: neutral
grays with one indigo accent, plenty of whitespace, and the same few patterns
on every page. This document covers the component inventory, how to use the
components, copy rules and how to move the remaining pages over.

The reference pages are the shell (`owner-portal/src/shell/PortalShell.tsx`),
sign-in (`pages/SignIn.tsx`) and the app list (`pages/Dashboard.tsx`). Copy
their patterns.

## Foundations

All values come from `shared/src/tokens.css`. Never hardcode a color, size or
duration in a page; if a value you need is missing, ask for a token.

### Color

| Group    | Tokens                                                                                                       |
| -------- | ------------------------------------------------------------------------------------------------------------ |
| Surfaces | `--ui-bg` (page), `--ui-surface` (cards), `--ui-surface-subtle`, `--ui-surface-hover`, `--ui-surface-active` |
| Text     | `--ui-text`, `--ui-text-muted` (secondary), `--ui-text-subtle` (labels, hints). All pass WCAG AA.            |
| Lines    | `--ui-border`, `--ui-border-strong` (inputs, secondary buttons), `--ui-border-hover`                         |
| Accent   | `--ui-accent-solid` (primary buttons), `--ui-accent-text` (links), `--ui-accent-subtle`, `--ui-focus`        |
| Status   | `--ui-{success,warning,danger,info}-text`, `-subtle` and the base color for dots and icons                   |

Colors use `light-dark()`. The theme follows the OS unless the theme toggle has
set `data-theme` on `<html>`; `theme.js` restores that choice before first
paint.

### Type

Inter (UI) and JetBrains Mono (code) are self-hosted from
`@fontsource-variable/inter` and `@fontsource-variable/jetbrains-mono` (SIL Open
Font License 1.1, pinned in `frontend/package-lock.json`). `shared/src/fonts.css`
loads only the Latin variable-weight files with `font-display: swap`, plus a
metric-matched local fallback so text doesn't shift when the font arrives.

| Step  | Size / line height | Tracking | Use                                           |
| ----- | ------------------ | -------- | --------------------------------------------- |
| `2xl` | 24 / 32            | -0.019em | Page title (20 / 28 on phones)                |
| `xl`  | 20 / 28            | -0.017em | Sign-in title                                 |
| `lg`  | 16 / 24            | -0.011em | Section, dialog and empty-state titles; brand |
| `md`  | 14 / 20            | -0.006em | Body, buttons, inputs, table cells (base)     |
| `sm`  | 13 / 20            | -0.003em | Labels, hints, meta lines, small buttons      |
| `xs`  | 12 / 16            | 0        | Table headers, badges, "Optional"             |

Weights: 400 for text, 500 for labels, buttons, links and row titles, 600 for
headings. Code is JetBrains Mono at 0.93em. Use `ui-tabular` only on
cells that hold just numbers: tabular figures also widen Inter's hyphens.

### Spacing and layout

Everything sits on a 4px grid (`--ui-space-1` 4px … `--ui-space-16` 64px).
These are the only layout values pages should use:

| Token                        | Desktop                   | Phone (≤640px) | Where                                                  |
| ---------------------------- | ------------------------- | -------------- | ------------------------------------------------------ |
| `--ui-gutter`                | 32px (24px at 641–1023px) | 16px           | Left/right page edge for header, tabs, content         |
| `--ui-content-width`         | 1120px                    | —              | Max content width                                      |
| `--ui-narrow-width`          | 640px                     | —              | `Page width="narrow"` (single forms)                   |
| `--ui-header-height`         | 56px                      | 56px           | Top bar                                                |
| `--ui-tabbar-height`         | 44px                      | 44px           | Section tabs and `TabNav`                              |
| `--ui-page-padding-top`      | 32px                      | 24px           | Header/tabs to page header                             |
| `--ui-page-padding-bottom`   | 64px                      | 48px           | Below the last section                                 |
| `--ui-page-gap`              | 24px                      | 20px           | Page header to content; section to section             |
| `--ui-card-padding`          | 20px                      | 16px           | Card/section padding; first/last table cell; list rows |
| `--ui-card-gap`              | 16px                      | 16px           | Between blocks inside a section body                   |
| `--ui-section-header-height` | 56px                      | 56px           | Section header (title + actions)                       |
| `--ui-cell-padding-y` / `-x` | 12px / 16px               | stacked rows   | Table cells                                            |
| `--ui-row-padding-y`         | 12px (compact: 10px)      | same           | List rows                                              |
| `--ui-field-label-gap`       | 6px                       | 6px            | Label to control, control to hint/error                |
| `--ui-field-gap`             | 16px                      | 16px           | Field to field (`Stack gap={4}`, `Grid`)               |
| `--ui-group-gap`             | 24px                      | 24px           | Group to group in a form (`Stack gap={6}`)             |
| `--ui-control-height`        | 36px                      | 44px           | Buttons, inputs, selects, segmented control            |
| `--ui-control-height-sm`     | 32px                      | 44px           | Small buttons, header nav links                        |
| `--ui-control-padding-x`     | 14px (small: 10px)        | same           | Button sides                                           |
| `--ui-input-padding-x`       | 12px                      | 12px           | Input sides                                            |
| `--ui-badge-height`          | 22px                      | 22px           | Badges                                                 |

Touch screens of any width also get 44px controls.

Alignment rules:

- Header brand, section tabs and page content share one container: the left
  edge is always `--ui-gutter`. Don't add horizontal margins to pages.
- Inside a card, text starts at `--ui-card-padding`: section titles, table
  first-column text, list rows and form fields all line up.
- Vertical rhythm is fixed: page header, `--ui-page-gap`, section,
  `--ui-page-gap`, section. Don't add margins between sections; put them in
  a `Page`.
- Same size, same height: a medium button, input, select and segmented control
  are all `--ui-control-height` tall and line up in a row.

### Shape, focus and states

- Radii: `--ui-radius-xs` 4px (16px checkboxes only), `--ui-radius-sm` 6px
  (segments, menu items), `--ui-radius-md` 8px (controls, alerts, code),
  `--ui-radius-lg` 12px (cards, dialogs, menus), `--ui-radius-full` (badges,
  avatars).
- Borders are 1px `--ui-border` on surfaces and `--ui-border-strong` on
  controls.
- Focus: one 2px `--ui-focus` outline, 2px offset, on every control (inset
  inside menus, tabs and segmented controls).
- States: hover uses `--ui-surface-hover` (or the `-hover` solid), pressed uses
  `--ui-surface-active` (or the `-active` solid), disabled is 50% opacity with
  a not-allowed cursor and no hover change. Hover styles apply only on devices
  that can hover (`@media (hover: hover)`), so taps never leave a fill.
- Fixed-height controls keep their step's line height (buttons 14/20, small
  buttons and segments 13/20, badges 12/16); the control height centers it.
- Section headers are always `--ui-section-header-height` (56px), with or
  without an action: their padding leaves room for exactly one control.
- Motion: `--ui-duration-fast` 120ms for hovers, `--ui-duration` 200ms for
  overlays. Reduced motion turns animation off.

Styles live in cascade layers: `ui-base` (resets), `legacy` (pre-redesign page
CSS, see [Migration](#migration)) and `ui` (components). Components always win
over legacy rules.

### Gallery

`owner-portal/gallery.html` shows every component in its variants and states.
Only the Vite dev server serves it; the production build has one entry
(`index.html`), so it never ships. Run:

```sh
npm --prefix frontend/owner-portal run dev -- --port 9605 --strictPort
```

Open `http://127.0.0.1:9605/gallery.html` (it needs no backend). Use the theme
toggle in its header, or screenshot it with `colorScheme` light and dark.

## Components

Import everything from `@openstack-platform/ui`. Components take children and
plain props; routing stays in the app, so links are passed in as `<Link>`
elements and styled with the exported class helpers.

| Need                      | Use                                                                                                                                   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Page frame                | `AppShell`, `Brand`, `AccountMenu`, `navLinkClass`, `menuItemClass` (shell only)                                                      |
| Page content              | `Page` (`width="narrow"` for single forms), `PageHeader`, `backLinkClass`                                                             |
| Signed-out flows          | `AuthLayout` (sign in, account setup)                                                                                                 |
| Grouping                  | `Section` (titled card; `flush` for tables/lists; `footer` for form actions), `Card`                                                  |
| Layout                    | `Stack`, `Cluster`, `Grid` (`columns={2 \| 3}`, one column on phones)                                                                 |
| Actions                   | `Button` (`primary`, `secondary`, `ghost`, `danger`; `size="sm"`; `loading`), `IconButton`, `buttonClass()` for links                 |
| Forms                     | `Field` + `Input` / `Textarea` / `Select` / `PasswordInput`; `Checkbox`, `Radio`, `Switch`, `Fieldset` (`variant="cards"`), `Hint`    |
| Choice in place           | `SegmentedControl` (two to four options, not navigation)                                                                              |
| Navigation between routes | `TabNav` + `tabClass()`                                                                                                               |
| Status                    | `Badge` (needs attention) and `StatusText` (expected state). In the portal use `components/Status.tsx`, which applies the badge rule. |
| Collections               | `DataTable` (stacks on phones), `List` + `ListItem` (feeds), `KeyValueList`                                                           |
| Text values               | `CodeBlock` (`variant="log"` for build output), `CopyField`, `CopyId` (short ID + copy), `RelativeTime`, `BoundaryText` for URLs      |
| Feedback                  | `Alert`, `ErrorAlert`, `InlineStatus`, `useToast()` (inside `ToastProvider`), `EmptyState`                                            |
| Loading                   | `PageSkeleton` (whole page), `LoadingRows` (inside a section), `Skeleton`, `Spinner`                                                  |
| Overlays                  | `Dialog` (centered; a bottom sheet on phones)                                                                                         |
| Utilities (class names)   | `ui-link`, `ui-mono`, `ui-text-muted`, `ui-text-subtle`, `ui-text-danger`, `ui-text-sm`, `ui-truncate`, `ui-break`, `ui-sr-only`      |

The portal adds a few shared pieces in `owner-portal/src/components`:
`Status` (state to badge), `Operation` + `OperationList` (activity rows),
`Feedback` (re-exports), `ThemeButton`, `Mark`.

### Usage rules

- **One page, one `PageHeader`.** Title and actions only. Put status next to the
  title with `meta`. A back link goes in `back` as
  `<Link className={backLinkClass}>`. No eyebrow labels and no subtitle.
- **Counts and limits** ("1 of 2") go in `PageHeader` `meta` as a neutral
  `Badge` without a dot, never as a caption under the title. Keep header
  actions to one or two buttons so the row fits at 390px.
- **One primary button per view.** Other actions are `secondary` or `ghost`.
  Destructive actions use `danger` and a confirming `Dialog` that names the
  thing being removed.
- **Forms:** wrap every control in `Field`. Set `id` on the `Field`, not the
  control. Put form actions in the `Section` `footer`, primary last. Use
  `loading` on the submit button while the request runs. Show field problems
  with `error`, and request failures with `ErrorAlert` above the actions.
- **Tables:** use `DataTable` for records people compare across rows. When a
  row opens a detail page, pass `onRowClick` and keep a link in the first
  column. Choose each column's phone role: `title` and `trailing` (status)
  share the first line; with only one or two extra values use `secondary`
  (muted line, e.g. username) and `meta` (subtle, e.g. a time) instead of
  labelled `field` lines; `hidden` drops a column (e.g. IDs) on phones.
- **"View all":** a section that previews a longer list puts the link in
  `Section` `actions` as a small ghost button:
  `<Link className={buttonClass({ variant: "ghost", size: "sm" })}>View all <Icon name="chevron-right" /></Link>`.
  Show it only when there is more; there is no footer variant.
- **Details:** use `KeyValueList`. On phones each pair is one line, label
  left and value right, like stacked table cards; text values over 28
  characters, or items with `stacked`, put the value under the label.
- **Table rows** are a fixed `--ui-table-row-height` (48px) whether or not
  they hold a badge.
- **IDs and times:** show long IDs with `CopyId` (8 characters; commits 9)
  and times with `RelativeTime`, which keeps the exact time in the tooltip
  and a `<time dateTime>`. Use `CopyField` for values people paste whole,
  like setup links.
- **Lists:** use `List` for collections and `density="compact"` for feeds.
  `meta` holds facts (`app · commit · time`), not sentences. Separators are
  added automatically and dropped on phones. In feeds, badge only states that
  need attention (in progress, failed, needs attention), never success.
- **Activity titles are events** phrased by outcome, from `activityTitle`
  in `utils/presentation.ts`: "Deployed", "App created", "Settings saved";
  "Deploying" while running; the noun ("Deployment") next to a Failed badge.
- **Empty states** say what will appear and offer the next action. Show only one
  create action on a page.
- **Loading:** skeletons that mirror the loaded layout, never "Loading…"
  text, so nothing jumps. Wrap them in `PageSkeleton` (it announces the
  label) and compose `PageHeaderSkeleton` (with `meta` and `actions` like the
  real header) and one `SectionSkeleton` per section (`variant` table, list
  or body, with the real row count and density). See `pages/Dashboard.tsx`.
- **Load errors:** keep the page's shape. Render the normal `Page` and
  `PageHeader`, then `QueryError` (portal) for the failed query, or
  `LoadError` (shared) with your own message. It says what failed and what to
  do ("Couldn't load your apps. Try again in a minute.") and offers Retry. It
  is announced but not focused, so no focus ring appears on load. Section
  queries that fail render the same component inside the section.
- **Feedback:** confirmations that need no action are toasts or
  `InlineStatus`. Anything that needs action is an `Alert`.
- **Phones:** controls are 44px tall automatically. Don't add fixed widths;
  test at 390px.
- **Accessibility:** every icon-only button needs `label`. Never use color
  alone; badges and alerts always carry text. Keep the visible focus ring.
- **Security:** no `style` attributes, injected stylesheets or HTML injection
  (`npm run check:source` enforces this). Never put secrets in URLs, storage,
  query keys, toasts or `CopyField`. Secret inputs stay write-only.

### Badge rule

Badges are for states that need attention. The expected state is quiet.

- **Expected states** (succeeded, healthy, ready, active, live) never get a
  badge. In tables and details they render as `StatusText` (a dot and muted
  text). In feeds they are visually hidden but still read by screen readers
  (`<Status quiet="hidden">`).
- **Badges** mark everything else: in progress (queued, building,
  deploying, creating), problems (failed, unhealthy, needs attention, not
  created) and neutral exceptions (stopped, disabled, rolled back, unknown).
  The text always names the state; color is never the only signal.
- **One place per view.** A detail page shows an object's state once: next
  to the title in `PageHeader` `meta`, not again in its details or a
  sidebar.
- Use the portal `Status` component for every state so labels, tones and this
  rule stay consistent. Add new states to `components/Status.tsx`; don't map
  them in pages.

### Status vocabulary

One word per state, everywhere. Pages pass a state key to the portal
`Status` component; they never write their own labels.

| Thing            | States (label)                                                                                                                                                                   |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App              | creating (Creating), rejected (Not created), not_deployed (Not deployed), stopped (Stopped), healthy (Healthy), unhealthy (Unhealthy), unknown (Unknown)                         |
| Deployment       | queued (Queued), building (Building), deploying (Deploying), live (Live), succeeded (Succeeded), failed (Failed), recovery_required (Needs attention), rolled_back (Rolled back) |
| Activity         | prepared (Queued), accepted (In progress), succeeded (Succeeded), failed (Failed), blocked (Needs attention), unknown (Unknown)                                                  |
| Account          | active (Active), disabled (Disabled)                                                                                                                                             |
| Storage resource | ready (Ready), creating (Creating), failed (Failed)                                                                                                                              |

- **An app shows one state**, from `appState` (staff/admin records) or
  `ownerAppState` (owner records) in `utils/presentation.ts`, in this order:
  lifecycle (Creating, Not created), then first deploy (Not deployed), then
  runtime (Stopped, Healthy, Unhealthy, Unknown when stale). App lists,
  app headers and details all use it. "Ready" is a lifecycle detail and is
  never shown for an app.

## Copy rules

- Sentence case everywhere: titles, buttons, labels, tabs.
- Short, plain labels. Buttons are verbs that say what happens: "Create app",
  "Save settings", "Deploy", "Delete variable".
- No subtitle or caption under every heading, stat or label. Add a hint only
  where people would otherwise make a mistake, and always with `Hint` or a
  `Field` hint.
- No internal jargon in UI copy: intent, broker, controller, fingerprint,
  operation, maintenance plan, provider, binder, revision (say "settings"),
  allocation, scheduler, Commons, and so on.
- Errors say what happened and what to do: "Username or password is
  incorrect." or "This page expired. Reload it and try again."
- Use numerals and relative times ("2 minutes ago"); full dates on hover.
- Never hardcode a deployment, school, class or domain name. The brand is
  `platformName` and the class sign-in method is `providerLabel`; both come
  from the server.

### Glossary

| Use                                          | Not                                              |
| -------------------------------------------- | ------------------------------------------------ |
| app                                          | application, project, workspace                  |
| deploy (verb), deployment (noun)             | release, rollout, operation                      |
| settings                                     | configuration, revision                          |
| environment variables                        | env, secrets                                     |
| databases: PostgreSQL, MongoDB; S3 storage   | storage resources, buckets, bindings (in titles) |
| owner, staff, admin                          | student (for roles), instructor, operator        |
| sign in, sign out                            | log in, login, log out                           |
| local account; `providerLabel` for the other | Commons account, portal account                  |
| activity                                     | operations, intents, audit (except "Audit log")  |

## Migration

Pages that are not rebuilt yet keep their old class names, styled by
`owner-portal/src/styles/legacy.css` in the lower `legacy` layer. That file
maps the old color variables to the new tokens, so old pages already follow the
theme.

Legacy element rules (headings, labels, inputs…) are fenced off from
anything inside `Page`, `AuthLayout` or `Dialog`, and base rules (body type,
links, focus) come only from `ui.css`. So a rebuilt page must render its
content inside `Page` (or `AuthLayout` for signed-out flows) to be measured
against the system values exactly.

When you rebuild a page:

1. Rewrite it with design-system components. Keep every API call, query key,
   mutation, CSRF/step-up flow, confirmation and write-only behavior.
2. Keep accessible names that tests depend on, or update the tests. Never
   weaken a security assertion.
3. Don't edit `legacy.css`, `frontend/shared` or the shell. If something is
   missing, compose it from existing components or use a page CSS file (below)
   and note the gap for a later design-system change.
4. Page-only layout CSS goes in a file next to your pages (for example
   `src/pages/app/app-pages.css`), imported by your page module, using tokens
   and unlayered `.your-prefix-*` class names.

`legacy.css` is deleted in a final cleanup once no page renders legacy class
names.

## Phase 2 tracks

The remaining pages split into three tracks with disjoint files. No track edits
`frontend/shared`, `App.tsx`, `shell/`, `styles/`, `api.ts`, `authOptions.ts`,
`components/{Status,Operation,Feedback,ThemeButton,Mark,BoundaryText}.tsx`,
`utils/presentation.ts` or `hooks/useSession.ts`.

| Track                 | Pages and routes                                                                                                                            | Files it owns                                                                                                                                                                                                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A. App workspace      | New app `/apps/new`; app overview `/apps/:id`; settings `/apps/:id/configuration`; deploy `/apps/:id/deploy`; history and deployment detail | `pages/{NewApp,Overview,Configuration,Deploy,History,Deployment}.tsx`, `components/{AppFrame,DeploymentRow,EnvironmentSection,StorageSection}.tsx`, `hooks/useIntentPolling.ts`, `App.test.tsx`, `components/resources.test.tsx`, `e2e/owner-flow.spec.ts`, `e2e/resources.spec.ts` |
| B. Staff              | `/staff/owners`, `/staff/owners/:id`, `/staff/apps`, `/staff/apps/:id` and its deployments, `/staff/operations`                             | `pages/Staff.tsx` (split into `pages/staff/*` if useful), `staffApi.ts`, `Staff.test.tsx`                                                                                                                                                                                           |
| C. Admin and accounts | `/admin/apps`, `/admin/apps/:id`; `/admin/accounts`; `/admin/audit`; account setup `/setup` and `/activate`                                 | `pages/{AdminApps,Accounts,Enrollment}.tsx`, `adminApi.ts`, `adminAppsApi.ts`, `AdminApps.test.tsx`, `e2e/staff-flow.spec.ts`                                                                                                                                                       |

Notes for tracks:

- Track C reuses `EnvironmentSection` and `StorageSection` from track A on
  admin app pages. Track A keeps their props compatible; track C only passes
  props.
- Track C: `Enrollment` should read `/auth/options` through `authOptionsQuery`
  (`src/authOptions.ts`) so the shell shows the platform name on `/setup`. Also
  remove the hardcoded "Commons" from the admin adoption confirmation.
- Track B: staff tabs (Owners, All apps, Activity) are already in the shell;
  remove the in-page "Staff view · read only" eyebrow.
- Each track runs the smoke on its own port: `OWNER_PORTAL_SMOKE_PORT=96x0 npm
--prefix frontend/owner-portal run smoke`.
- Each track adds before/after screenshots at 1440×900 and 390×844 in light
  and dark.
