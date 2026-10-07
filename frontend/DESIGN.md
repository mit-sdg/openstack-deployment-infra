# Portal design system

Use this design system when changing portal pages. The UI uses `frontend/shared` (`@openstack-platform/ui`); this page defines its tokens, components, usage rules, status vocabulary, and copy.

Keep the UI calm and consistent: neutral grays, one indigo accent, whitespace, and shared page patterns. Use these reference pages:

- the shell, `owner-portal/src/shell/PortalShell.tsx`
- sign-in, `owner-portal/src/pages/SignIn.tsx`
- the app list, `owner-portal/src/pages/Dashboard.tsx`

## The short version

- Take every color, size, and duration from a token. If the value you need is missing, ask for a new token.
- Build pages from `@openstack-platform/ui` components. Keep routing and API calls in the app.
- One `PageHeader` per page. One primary button per view.
- Show every state through the portal's `Status` component. Badges are only for states that need attention.
- Write short, plain, sentence-case copy without internal jargon.
- No inline styles, injected stylesheets, or HTML injection. Never put secrets in URLs, storage, or toasts.

## Foundations

All values come from `shared/src/tokens.css`. Never hardcode a color, size, or duration in a page.

### Color

| Group | Tokens |
| --- | --- |
| Surfaces | `--ui-bg` (page), `--ui-surface` (cards), `--ui-surface-subtle`, `--ui-surface-hover`, `--ui-surface-active`, `--ui-surface-raised` |
| Text | `--ui-text`, `--ui-text-muted` (secondary), `--ui-text-subtle` (labels, hints). All pass WCAG AA. |
| Lines | `--ui-border`, `--ui-border-strong` (inputs, secondary buttons), `--ui-border-hover` |
| Accent | `--ui-accent-solid` (primary buttons), `--ui-accent-text` (links), `--ui-accent-subtle`, `--ui-focus` |
| Status | For each of `success`, `warning`, `danger`, and `info`: `--ui-<status>-text`, `--ui-<status>-subtle` (quiet background), and `--ui-<status>` (dots and icons). `neutral` has the same three. |

Theme-dependent colors use `light-dark()`. The theme follows the operating system unless the theme toggle has set `data-theme` on `<html>`. The shared `theme.js` restores that choice before first paint. Each app names its own storage key on the script tag (`data-theme-storage`); the portal's key is `owner-portal-theme`.

### Type

Inter (UI text) and JetBrains Mono (code) are self-hosted from `@fontsource-variable/inter` and `@fontsource-variable/jetbrains-mono`, pinned in `frontend/package-lock.json` under the SIL Open Font License 1.1. `shared/src/fonts.css` loads only the Latin variable-weight files with `font-display: swap`, plus a metric-matched local fallback so text doesn't shift when the font arrives.

| Step | Size / line height (px) | Tracking | Use |
| --- | --- | --- | --- |
| `2xl` | 24 / 32 | -0.019em | Page title (20 / 28 on phones) |
| `xl` | 20 / 28 | -0.017em | Sign-in title |
| `lg` | 16 / 24 | -0.011em | Section, dialog, and empty-state titles; the brand |
| `md` | 14 / 20 | -0.006em | Body, buttons, inputs, table cells (the base size) |
| `sm` | 13 / 20 | -0.003em | Labels, hints, meta lines, small buttons |
| `xs` | 12 / 16 | 0 | Table headers, badges, "Optional" |

Each step is three tokens: `--ui-text-<step>`, `--ui-leading-<step>`, and `--ui-tracking-<step>`.

- Weights: 400 for text, 500 for labels, buttons, links, and row titles, 600 for headings (`--ui-weight-regular`, `--ui-weight-medium`, `--ui-weight-semibold`).
- Code is JetBrains Mono at 0.93em (`--ui-mono-scale`).
- Use `ui-tabular` only on cells that hold nothing but numbers. Tabular figures also widen Inter's hyphens.

### Spacing and layout

Everything sits on a 4px grid, `--ui-space-1` (4px) to `--ui-space-16` (64px). Pages should use only these layout tokens:

| Token | Desktop | Phone (640px and narrower) | Where |
| --- | --- | --- | --- |
| `--ui-gutter` | 32px (24px from 641 to 1023px) | 16px | Left and right page edge for the header, tabs, and content |
| `--ui-content-width` | 1120px | same | Maximum content width |
| `--ui-narrow-width` | 640px | same | `Page width="narrow"` (single forms) |
| `--ui-header-height` | 56px | 56px | Top bar |
| `--ui-tabbar-height` | 44px | 44px | Section tabs and `TabNav` |
| `--ui-page-padding-top` | 32px | 24px | Header or tabs to the page header |
| `--ui-page-padding-bottom` | 64px | 48px | Below the last section |
| `--ui-page-gap` | 24px | 20px | Page header to content; section to section |
| `--ui-card-padding` | 20px | 16px | Card and section padding; first and last table cell; list rows |
| `--ui-card-gap` | 16px | 16px | Between blocks inside a section body |
| `--ui-section-header-height` | 56px | 56px | Section header (title and actions) |
| `--ui-cell-padding-y`, `--ui-cell-padding-x` | 12px, 16px | rows stack | Table cells |
| `--ui-table-row-height` | 48px | | Every table row, with or without a badge |
| `--ui-row-padding-y`, `--ui-row-padding-y-compact` | 12px, 10px | same | List rows (default and compact) |
| `--ui-field-label-gap` | 6px | 6px | Label to control; control to hint or error |
| `--ui-field-gap` | 16px | 16px | Field to field (`Stack gap={4}`, `Grid`) |
| `--ui-group-gap` | 24px | 24px | Group to group in a form (`Stack gap={6}`) |
| `--ui-control-height` | 36px | 44px | Buttons, inputs, selects, segmented control |
| `--ui-control-height-sm` | 32px | 44px | Small buttons, header navigation links |
| `--ui-control-padding-x`, `--ui-control-padding-x-sm` | 14px, 10px | same | Button sides (medium, small) |
| `--ui-input-padding-x` | 12px | 12px | Input sides |
| `--ui-badge-height` | 22px | 22px | Badges |

Touch screens of any width also get 44px controls.

Alignment rules:

- **One left edge.** The header brand, section tabs, and page content share one container, so the left edge is always `--ui-gutter`. Don't add horizontal margins to pages.
- **Card content lines up.** Inside a card, text starts at `--ui-card-padding`: section titles, the first table column, list rows, and form fields all align.
- **Fixed vertical rhythm.** Page header, `--ui-page-gap`, section, `--ui-page-gap`, section. Don't add margins between sections; put them in a `Page`.
- **Same size, same height.** A medium button, input, select, and segmented control are all `--ui-control-height` tall and line up in a row.

### Shape, focus, and states

- **Radii.** `--ui-radius-xs` 4px (16px checkboxes only), `--ui-radius-sm` 6px (segments, menu items), `--ui-radius-md` 8px (controls, alerts, code), `--ui-radius-lg` 12px (cards, dialogs, menus), `--ui-radius-full` (badges, avatars).
- **Borders.** 1px `--ui-border` on surfaces and `--ui-border-strong` on controls.
- **Focus.** One 2px `--ui-focus` outline with a 2px offset on every control (`--ui-focus-ring-width`, `--ui-focus-ring-offset`). Inside menus, tabs, and segmented controls the outline is inset.
- **Hover and pressed.** Hover uses `--ui-surface-hover` (or the solid's `-hover` token); pressed uses `--ui-surface-active` (or the solid's `-active` token). Hover styles apply only on devices that can hover (`@media (hover: hover)`), so a tap never leaves a fill behind.
- **Disabled.** 50% opacity, a not-allowed cursor, and no hover change.
- **Fixed-height controls keep their line height.** Buttons use 14/20, small buttons and segments 13/20, badges 12/16. The control height centers the text.
- **Section headers are always 56px** (`--ui-section-header-height`), with or without an action. Their padding leaves room for exactly one control.
- **Motion.** `--ui-duration-fast` (120ms) for hovers, `--ui-duration` (200ms) for overlays. Reduced motion turns animation off.
- **Depth.** `--ui-shadow-xs` (cards, buttons), `--ui-shadow-sm` (switch thumbs, the selected segment), `--ui-shadow-md` (menus and panels), `--ui-shadow-lg` (dialogs, toasts).

Styles live in two cascade layers: `ui-base` (resets) and `ui` (components and utilities). Page CSS outside any layer wins over both, so keep page CSS to page layout.

### Gallery

`owner-portal/gallery.html` shows every component in its variants and states. Only the Vite development server serves it. The production build has one entry (`index.html`), so the gallery never ships.

```bash
npm --prefix frontend/owner-portal run dev -- --host 127.0.0.1 --port 9605 --strictPort
```

Open `http://127.0.0.1:9605/gallery.html`. It needs no backend. Use the theme toggle in its header, or take screenshots with `colorScheme` set to light and dark.

## Components

Import everything from `@openstack-platform/ui`. Components take children and plain props. Routing stays in the app: pass links in as `<Link>` elements and style them with the exported class helpers.

| Need | Use |
| --- | --- |
| Page frame | `AppShell`, `Brand`, `AccountMenu`, `navLinkClass`, `menuItemClass` (shell only) |
| Page content | `Page` (`width="narrow"` for single forms), `PageHeader`, `backLinkClass` |
| Signed-out flows | `AuthLayout` (sign-in, account setup) |
| Grouping | `Section` (a titled card; `flush` for tables and lists; `footer` for form actions), `Card` |
| Layout | `Stack`, `Cluster`, `Grid` (`columns={2}` or `columns={3}`, one column on phones) |
| Actions | `Button` (`variant` `primary`, `secondary`, `ghost`, or `danger`; `size="sm"`; `loading`), `IconButton`, `buttonClass()` for links |
| Forms | `Field` with `Input`, `Textarea`, `Select`, or `PasswordInput`; `Checkbox`, `Radio`, `Switch`, `Fieldset` (`variant="cards"`), `Hint` |
| A choice in place | `SegmentedControl` (two to four options; not for navigation) |
| Navigation between routes | `TabNav` with `tabClass()` |
| Status | `Badge` (needs attention) and `StatusText` (expected state). In the portal, use `components/Status.tsx`, which applies the [badge rule](#badge-rule). |
| Collections | `DataTable` (stacks on phones), `List` with `ListItem` (feeds), `KeyValueList` |
| Text values | `CodeBlock` (`variant="log"` for build output), `CopyField`, `CopyId` (short ID with copy), `RelativeTime`, `BoundaryText` for URLs |
| Feedback | `Alert`, `ErrorAlert`, `InlineStatus`, `useToast()` (inside `ToastProvider`), `EmptyState` |
| Loading | `PageSkeleton` (whole page), `PageHeaderSkeleton`, `SectionSkeleton`, `LoadingRows` (inside a section), `Skeleton`, `Spinner` |
| Errors | `LoadError` (shared) or the portal's `QueryError` |
| Overlays | `Dialog` (centered; a bottom sheet on phones) |
| Icons and theme | `Icon`, `Mark`, `ThemeToggle` |
| Utility classes | `ui-link`, `ui-mono`, `ui-tabular`, `ui-text-muted`, `ui-text-subtle`, `ui-text-danger`, `ui-text-sm`, `ui-truncate`, `ui-break`, `ui-sr-only` |

The portal adds a few shared pieces in `owner-portal/src/components`:

- `Status` maps a state to its label and tone and applies the badge rule.
- `Operation` and `OperationList` render activity rows.
- `Feedback` re-exports the shared feedback components and adds `QueryError`.
- `ThemeButton` is `ThemeToggle` with the portal's storage key.
- `Mark` re-exports the shared mark.

The package also exports older primitives (`ThemeButton`, `StatusBadge`, `LegacyCard`, `ActivityRow`, `ShellFrame`, `Empty`, `ErrorNotice`, `Loading`) for the operator dashboard. Don't use them in new portal code.

## Usage rules

### Page structure

- **One page, one `PageHeader`.** It holds the title and actions only. Put status next to the title with `meta`. A back link goes in `back` as `<Link className={backLinkClass}>`. No eyebrow labels and no subtitle.
- **Counts and limits** ("1 of 2") go in `PageHeader` `meta` as a neutral `Badge` without a dot, never as a caption under the title.
- Keep header actions to one or two buttons so the row fits at 390px.

### Actions

- **One primary button per view.** Other actions are `secondary` or `ghost`.
- **Destructive actions** use `danger` and a confirming `Dialog` that names the thing being removed.

### Forms

- Wrap every control in `Field`. Set `id` on the `Field`, not on the control.
- Put form actions in the `Section` `footer`, with the primary action last.
- Use `loading` on the submit button while the request runs.
- Show a field's problem with its `error`. Show a request failure with `ErrorAlert` above the actions.

### Tables and details

- **Use `DataTable`** for records people compare across rows.
- For rows that open a page, pass `onRowClick` and keep a link in the first column.
- **Phone layout.** Set each column's `mobile` role. `title` and `trailing` (status) share the first line. With only one or two extra values, use `secondary` (a muted line, such as a username) and `meta` (a subtle line, such as a time) instead of labelled `field` lines. `hidden` drops a column (such as an ID) on phones.
- **Row height** is a fixed `--ui-table-row-height` (48px), whether or not the row holds a badge.
- **"View all":** a section that previews a longer list puts the link in `Section` `actions` as a small ghost button. Show it only when there is more. There is no footer variant.

  ```tsx
  <Link className={buttonClass({ variant: "ghost", size: "sm" })}>
    View all <Icon name="chevron-right" />
  </Link>
  ```

- Use `KeyValueList` for details. On phones each pair is one line, label left and value right, like stacked table cards. Text values over 28 characters, or items with `stacked`, put the value under the label.
- Show long IDs with `CopyId` (8 characters; commits use 9) and times with `RelativeTime`, which keeps the exact time in the tooltip and a `<time dateTime>`. Use `CopyField` for values people paste whole, such as setup links.

### Lists and activity

- Use `List` for collections and `density="compact"` for feeds.
- `meta` holds facts (`app · commit · time`), not sentences. Separators are added automatically and dropped on phones.
- In feeds, badge only states that need attention (in progress, failed, needs attention), never success.
- **Activity titles are events**, phrased by outcome, from `activityTitle` in `utils/presentation.ts`: "Deployed", "App created", "Settings saved"; "Deploying" while running; the noun ("Deployment") next to a Failed badge.

### Empty, loading, and error states

- **Empty states** say what will appear and offer the next action. Show only one create action on a page.
- **Loading** uses skeletons that mirror the loaded layout, never "Loading…" text, so nothing jumps. Wrap them in `PageSkeleton` (it announces the label). Inside, compose `PageHeaderSkeleton` (with `meta` and `actions` like the real header) and one `SectionSkeleton` per section (`variant` `table`, `list`, or `body`, with the real row count and density). See `pages/Dashboard.tsx`.
- **Load errors keep the page's shape.** Render the normal `Page` and `PageHeader`, then `QueryError` (portal) for the failed query, or `LoadError` (shared) with your own message. It says what failed and what to do ("Couldn't load your apps. Try again in a minute.") and offers Retry. It is announced but not focused, so no focus ring appears on load. A section whose query fails renders the same component inside the section.

### Feedback

- Confirmations that need no action are toasts or `InlineStatus`.
- Anything that needs action is an `Alert`.

### Phones and accessibility

- Controls are 44px tall on phones automatically. Don't add fixed widths. Test at 390px.
- Every icon-only button needs a `label`.
- Never use color alone. Badges and alerts always carry text.
- Keep the visible focus ring.

### Security

- No `style` attributes, injected stylesheets, or HTML injection. `npm --prefix frontend run check:source` enforces this.
- Never put secrets in URLs, browser storage, query keys, toasts, or `CopyField`.
- Secret inputs stay write-only.

## Badge rule

Badges are for states that need attention. The expected state is quiet.

- **Expected states never get a badge.** These are succeeded, healthy, ready, active, and live. In tables and details they render as `StatusText` (a dot and muted text). In feeds they are visually hidden but still read by screen readers (`<Status quiet="hidden">`).
- **Badges mark everything else:**
  - in progress: queued, building, deploying, creating
  - problems: failed, unhealthy, needs attention, not created
  - neutral exceptions: stopped, disabled, rolled back, unknown

  The text always names the state; color is never the only signal.
- **One place per view.** A detail page shows an object's state once, next to the title in `PageHeader` `meta`. Don't repeat it in the details or a sidebar.
- **Use the portal `Status` component for every state**, so labels, tones, and this rule stay consistent. Add new states to `components/Status.tsx`; don't map them in pages.

## Status vocabulary

One word per state, everywhere. Pages pass a state key to the portal `Status` component and never write their own labels.

| Thing | States, as key (label) |
| --- | --- |
| App | `creating` (Creating), `rejected` (Not created), `not_deployed` (Not deployed), `stopped` (Stopped), `healthy` (Healthy), `unhealthy` (Unhealthy), `unknown` (Unknown) |
| Deployment | `queued` (Queued), `building` (Building), `deploying` (Deploying), `running` (Running), `live` (Live), `succeeded` (Succeeded), `failed` (Failed), `recovery_required` (Needs attention), `rolled_back` (Rolled back) |
| Activity | `prepared` (Queued), `accepted` (In progress), `succeeded` (Succeeded), `failed` (Failed), `blocked` (Needs attention), `unknown` (Unknown) |
| Account | `active` (Active), `pending` (Setup pending), `disabled` (Disabled) |
| Storage resource | `ready` (Ready), `creating` (Creating), `failed` (Failed) |

**An app shows one state.** It comes from `appState` (staff and admin records) or `ownerAppState` (owner records) in `utils/presentation.ts`, chosen in this order:

1. lifecycle: Creating, Not created
2. first deploy: Not deployed
3. runtime: Stopped, Healthy, Unhealthy, or Unknown when the data is stale

App lists, app headers, and details all use it. "Ready" is a lifecycle detail and is never shown for an app.

## Copy rules

- Use sentence case everywhere: titles, buttons, labels, tabs.
- Keep labels short and plain. Buttons are verbs that say what happens: "Create app", "Save settings", "Deploy", "Delete variable".
- Don't put a subtitle or caption under every heading, stat, or label. Add a hint only where people would otherwise make a mistake, and always with `Hint` or a `Field` hint.
- No internal jargon in UI copy: intent, broker, controller, fingerprint, operation, maintenance plan, provider, binder, revision (say "settings"), allocation, scheduler, Commons, and similar words.
- Errors say what happened and what to do: "Username or password is incorrect." or "This page expired. Reload it and try again."
- Use numerals and relative times ("2 minutes ago"), with the full date on hover.
- Never hardcode a deployment, school, class, or domain name. The brand is `platformName` and the class sign-in method is `providerLabel`; both come from the server.

### Glossary

| Use | Not |
| --- | --- |
| app | application, project, workspace |
| deploy (verb), deployment (noun) | release, rollout, operation |
| settings | configuration, revision |
| environment variables | env, secrets |
| databases: PostgreSQL, MongoDB; S3 storage | storage resources, buckets, bindings (in titles) |
| owner, staff, admin | student (for roles), instructor, operator |
| sign in, sign out | log in, login, log out |
| local account; `providerLabel` for the other kind | Commons account, portal account |
| activity | operations, intents, audit (except "Audit log") |

## Adding or changing pages

1. Build pages from design system components. Keep every API call, query key, mutation, CSRF and step-up flow, confirmation, and write-only behavior.
2. Keep the accessible names that tests depend on, or update the tests. Never weaken a security assertion.
3. Render page content inside `Page` (or `AuthLayout` for signed-out flows) so it follows the system's spacing and type.
4. Put page-only layout CSS in a file next to the pages (for example `src/pages/app-pages.css`), imported by the page module, using tokens and unlayered `.prefix-*` class names. When more than one page needs a pattern, add it to `frontend/shared` and the gallery instead.
5. When a change adds a bundled dependency or font, add its licence to `frontend/THIRD_PARTY_NOTICES.md`. The portal build ships that file as `third-party-notices.txt`.

To run the portal locally and check your change, see [Development](../docs/development.md#run-the-local-owner-portal).
