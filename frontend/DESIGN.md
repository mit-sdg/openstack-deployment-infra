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
duration in a page.

| Group    | Tokens                                                                                                             |
| -------- | ------------------------------------------------------------------------------------------------------------------ |
| Surfaces | `--ui-bg` (page), `--ui-surface` (cards), `--ui-surface-subtle`, `--ui-surface-hover`, `--ui-surface-active`       |
| Text     | `--ui-text`, `--ui-text-muted` (secondary), `--ui-text-subtle` (labels, hints). All pass WCAG AA on every surface. |
| Lines    | `--ui-border`, `--ui-border-strong` (inputs, secondary buttons)                                                    |
| Accent   | `--ui-accent-solid` (primary buttons), `--ui-accent-text` (links), `--ui-accent-subtle`, `--ui-focus`              |
| Status   | `--ui-{success,warning,danger,info}-text`, `-subtle` and the base color for dots and icons                         |
| Type     | System font stack (nothing is downloaded). Sizes `--ui-text-xs` 12 to `--ui-text-2xl` 24. Weights 400, 500, 600.   |
| Spacing  | 4px grid: `--ui-space-1` (4px) to `--ui-space-16` (64px)                                                           |
| Shape    | `--ui-radius-sm` 6, `-md` 8 (controls), `-lg` 12 (cards, dialogs); `--ui-shadow-xs` to `-lg`                       |
| Motion   | `--ui-duration-fast` 120ms, `--ui-duration` 200ms, `--ui-ease`. Reduced motion turns animation off.                |
| Layout   | `--ui-control-height` 36px (44px on phones and touch screens), `--ui-page-width` 1200px, `--ui-narrow-width` 680px |

Colors use `light-dark()`. The theme follows the OS unless the theme toggle has
set `data-theme` on `<html>`; `theme.js` restores that choice before first paint.

Styles live in cascade layers: `ui-base` (resets), `legacy` (pre-redesign page
CSS, see [Migration](#migration)) and `ui` (components). Components always win
over legacy rules.

## Components

Import everything from `@openstack-platform/ui`. Components take children and
plain props; routing stays in the app, so links are passed in as `<Link>`
elements and styled with the exported class helpers.

| Need                      | Use                                                                                                                                |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Page frame                | `AppShell`, `Brand`, `AccountMenu`, `navLinkClass`, `menuItemClass` (shell only)                                                   |
| Page content              | `Page` (`width="narrow"` for single forms), `PageHeader`, `backLinkClass`                                                          |
| Signed-out flows          | `AuthLayout` (sign in, account setup)                                                                                              |
| Grouping                  | `Section` (titled card; `flush` for tables/lists; `footer` for form actions), `Card`                                               |
| Layout                    | `Stack`, `Cluster`, `Grid` (`columns={2 \| 3}`, one column on phones)                                                              |
| Actions                   | `Button` (`primary`, `secondary`, `ghost`, `danger`; `size="sm"`; `loading`), `IconButton`, `buttonClass()` for links              |
| Forms                     | `Field` + `Input` / `Textarea` / `Select` / `PasswordInput`; `Checkbox`, `Radio`, `Switch`, `Fieldset` (`variant="cards"`), `Hint` |
| Choice in place           | `SegmentedControl` (two to four options, not navigation)                                                                           |
| Navigation between routes | `TabNav` + `tabClass()`                                                                                                            |
| Status                    | `Badge` with a tone. In the portal use `components/Status.tsx`, which maps states.                                                 |
| Collections               | `DataTable` (stacks on phones), `List` + `ListItem` (feeds), `KeyValueList`                                                        |
| Text values               | `CodeBlock` (`variant="log"` for build output), `CopyField`, `BoundaryText` for URLs                                               |
| Feedback                  | `Alert`, `ErrorAlert`, `InlineStatus`, `useToast()` (inside `ToastProvider`), `EmptyState`                                         |
| Loading                   | `PageSkeleton` (whole page), `LoadingRows` (inside a section), `Skeleton`, `Spinner`                                               |
| Overlays                  | `Dialog` (centered; a bottom sheet on phones)                                                                                      |
| Utilities (class names)   | `ui-link`, `ui-mono`, `ui-text-muted`, `ui-text-subtle`, `ui-text-danger`, `ui-text-sm`, `ui-truncate`, `ui-break`, `ui-sr-only`   |

The portal adds a few shared pieces in `owner-portal/src/components`:
`Status` (state to badge), `Operation` + `OperationList` (activity rows),
`Feedback` (re-exports), `ThemeButton`, `Mark`.

### Usage rules

- **One page, one `PageHeader`.** Title and actions only. Put status next to the
  title with `meta`. A back link goes in `back` as
  `<Link className={backLinkClass}>`. No eyebrow labels and no subtitle.
- **One primary button per view.** Other actions are `secondary` or `ghost`.
  Destructive actions use `danger` and a confirming `Dialog` that names the
  thing being removed.
- **Forms:** wrap every control in `Field`. Set `id` on the `Field`, not the
  control. Put form actions in the `Section` `footer`, primary last. Use
  `loading` on the submit button while the request runs. Show field problems
  with `error`, and request failures with `ErrorAlert` above the actions.
- **Tables:** use `DataTable` for records people compare across rows. Give each
  column a plain `header` and choose its phone layout (`title`, `trailing`,
  `field`, `hidden`). Put tables in `<Section flush>`.
- **Lists:** use `List` for feeds. `meta` holds facts (`app · commit · time`),
  not sentences. Separators are added automatically.
- **Empty states** say what will appear and offer the next action. Show only one
  create action on a page.
- **Loading:** skeletons, not "Loading…" text. Keep layout stable while
  data loads.
- **Feedback:** confirmations that need no action are toasts or
  `InlineStatus`. Anything that needs action is an `Alert`.
- **Phones:** controls are 44px tall automatically. Don't add fixed widths;
  test at 390px.
- **Accessibility:** every icon-only button needs `label`. Never use color
  alone; badges and alerts always carry text. Keep the visible focus ring.
- **Security:** no `style` attributes, injected stylesheets or HTML injection
  (`npm run check:source` enforces this). Never put secrets in URLs, storage,
  query keys, toasts or `CopyField`. Secret inputs stay write-only.

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
theme. When you rebuild a page:

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
