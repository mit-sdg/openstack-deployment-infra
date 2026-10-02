// Read-only renderer for the operator dashboard.
//
// The page never changes platform state. It polls the server's cached
// snapshot, renders it with DOM APIs (never HTML strings, so controller text
// cannot become markup), and can only ask the server to refresh early.

const SNAPSHOT_URL = "/api/snapshot";
const REFRESH_URL = "/api/refresh";
const POLL_MS = 10_000;
const BUSY_POLL_MS = 2_000;
const HIDDEN_POLL_MS = 60_000;
const THEME_KEY = "platform-dashboard-theme";
const HISTORY_SLOTS = 24;
const OPERATION_PREVIEW = 8;
const TONES = ["critical", "warning", "info", "neutral", "good"];
const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  snapshot: null,
  etag: null,
  connected: true,
  lastContact: 0,
  filter: "all",
  query: "",
  sort: "status",
  openApp: null,
  showAllOperations: false,
  expectRefreshUntil: 0,
  refreshBaseline: null,
  timer: null,
  contentKey: null,
  inFlight: false,
  pollAgain: false,
  headline: null,
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- DOM helpers

function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") element.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2), value);
    } else element.setAttribute(key, value === true ? "" : String(value));
  }
  return append(element, children);
}

function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#${name}`);
  svg.append(use);
  return svg;
}

function toneOf(value) {
  return TONES.includes(value) ? value : "neutral";
}

function toneIcon(tone) {
  const safe = toneOf(tone);
  const svg = icon(`s-${safe}`, "sicon");
  svg.setAttribute("data-tone", safe);
  return svg;
}

function pill(status) {
  const tone = toneOf(status?.tone);
  return h(
    "span",
    { class: "status", "data-tone": tone },
    toneIcon(tone),
    h("span", {}, status?.label ?? "Unknown"),
  );
}

function dot(tone) {
  return h("span", { class: "dot", "data-tone": toneOf(tone), "aria-hidden": "true" });
}

function replace(element, ...children) {
  element.replaceChildren();
  return append(element, children);
}

// ------------------------------------------------------------------ Formatting

const absoluteFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});
const clockFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });
const numberFormat = new Intl.NumberFormat();

function parseTime(value) {
  if (typeof value !== "string") return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function relative(value, now = Date.now()) {
  const time = parseTime(value);
  if (time === null) return "never";
  const seconds = Math.max(0, Math.round((now - time) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function absolute(value) {
  const time = parseTime(value);
  return time === null ? "Unknown time" : absoluteFormat.format(time);
}

function timeElement(value, prefix = "") {
  const time = parseTime(value);
  if (time === null) return h("span", { class: "muted" }, "—");
  return h(
    "time",
    { datetime: value, title: absoluteFormat.format(time), "data-relative": value, "data-prefix": prefix },
    prefix + relative(value),
  );
}

function plural(count, singular, pluralWord = `${singular}s`) {
  return `${numberFormat.format(count)} ${count === 1 ? singular : pluralWord}`;
}

function trimNumber(value) {
  return Number.isInteger(value) ? String(value) : value.toFixed(1).replace(/\.0$/, "");
}

function formatCpu(mhz) {
  if (typeof mhz !== "number") return null;
  return mhz >= 1000 ? `${trimNumber(mhz / 1000)} GHz` : `${mhz} MHz`;
}

function formatMemory(mib) {
  if (typeof mib !== "number") return null;
  return mib >= 1024 ? `${trimNumber(mib / 1024)} GiB` : `${mib} MiB`;
}

function formatBytes(bytes) {
  if (typeof bytes !== "number") return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${trimNumber(Math.round(value * 10) / 10)} ${units[unit]}`;
}

function sentence(text) {
  // Capitalize prose from the server, but never URLs, paths, or identifiers.
  if (typeof text !== "string" || !/^[a-z]+(?:\s|$)/.test(text)) return text;
  return text[0].toUpperCase() + text.slice(1);
}

function shortId(value, length = 8) {
  return typeof value === "string" ? value.slice(0, length) : "—";
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function safeHref(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

// Offer line breaks after dots and slashes so long URLs wrap at boundaries.
function breakable(text) {
  const parts = String(text).split(/(?<=[./])/);
  return parts.flatMap((part, index) =>
    index < parts.length - 1 ? [part, document.createElement("wbr")] : [part],
  );
}

function externalLink(url, label, className = "") {
  const href = safeHref(url);
  if (href === null) return h("span", {}, label);
  const parts = breakable(label);
  const last = parts.length && typeof parts.at(-1) === "string" ? parts.pop() : "";
  // Keep the icon with a short final word instead of letting it wrap alone.
  const tail =
    last.length <= 24
      ? h("span", { class: "ext-link__tail" }, last, icon("i-external"))
      : [last, icon("i-external")];
  return h(
    "a",
    {
      class: `ext-link ${className}`.trim(),
      href,
      target: "_blank",
      rel: "noopener noreferrer",
      "data-focus-key": `link:${href}`,
    },
    h("span", {}, parts),
    tail,
  );
}

// --------------------------------------------------------------------- Toast

let toastTimer = null;

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    element.hidden = true;
  }, 2600);
}

function copyButton(value, label) {
  const button = h(
    "button",
    {
      class: "copy",
      type: "button",
      title: `Copy ${label}`,
      "aria-label": `Copy ${label}`,
      "data-focus-key": `copy:${label}`,
    },
    icon("i-copy"),
  );
  button.addEventListener("click", async (event) => {
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
      button.dataset.copied = "true";
      button.replaceChildren(icon("i-check"));
      toast(`Copied ${label}`);
      setTimeout(() => {
        button.dataset.copied = "false";
        button.replaceChildren(icon("i-copy"));
      }, 1600);
    } catch {
      toast("Copying is unavailable in this browser");
    }
  });
  return button;
}

// --------------------------------------------------------------------- Header

function renderHeader(snapshot) {
  const platform = snapshot.platform ?? {};
  $("platform-name").textContent = platform.name || "Platform status";
  const meta = [platform.domain, platform.region].filter(Boolean).join(" · ");
  $("platform-meta").textContent = meta || "Operator dashboard";
  const headline = snapshot.summary?.headline;
  document.title = headline ? `${headline} · ${platform.name ?? "Platform"}` : "Platform status";
}

function renderFreshness() {
  const snapshot = state.snapshot;
  const element = $("freshness");
  const button = $("refresh-button");
  const refreshing =
    Boolean(snapshot?.refresh?.inProgress) ||
    snapshot?.state === "pending" ||
    Date.now() < state.expectRefreshUntil;
  button.setAttribute("aria-busy", refreshing ? "true" : "false");
  button.title = refreshing ? "Refreshing…" : "Refresh now";
  if (!snapshot || snapshot.state !== "ready") {
    element.textContent = refreshing ? "Collecting…" : "";
    return;
  }
  element.textContent = refreshing ? "Refreshing…" : `Updated ${relative(snapshot.generatedAt)}`;
  element.title = `Snapshot generated ${absolute(snapshot.generatedAt)}`;
}

function renderConnection() {
  const banner = $("connection-banner");
  const page = $("main");
  if (state.connected) {
    banner.hidden = true;
    page.dataset.disconnected = "false";
    return;
  }
  page.dataset.disconnected = state.snapshot ? "true" : "false";
  const since = state.snapshot?.generatedAt;
  replace(
    banner,
    toneIcon("warning"),
    h(
      "span",
      {},
      since
        ? `Lost contact with the dashboard service. Showing the snapshot from ${clockFormat.format(parseTime(since))}. Retrying…`
        : "Cannot reach the dashboard service. Retrying…",
    ),
  );
  banner.hidden = false;
}

// ------------------------------------------------------------------- Overview

function stat(iconName, label, value, of, sub, subTone = null) {
  return h(
    "div",
    { class: "stat" },
    h("dt", {}, icon(iconName), label),
    h(
      "dd",
      {},
      h("span", { class: "stat__value" }, value, of ? h("span", { class: "stat__of" }, of) : null),
      h("span", { class: "stat__sub" }, subTone ? toneIcon(subTone) : null, h("span", {}, sub)),
    ),
  );
}

function renderOverview(snapshot) {
  const overview = $("overview");
  const iconSlot = $("overview-icon");
  if (snapshot.state !== "ready") {
    overview.dataset.tone = "pending";
    replace(iconSlot, h("span", { class: "spinner" }));
    $("headline").textContent = "Collecting platform status…";
    $("overview-detail").textContent =
      "The first refresh waits for the admin host, OpenStack, and public routes to answer.";
    replace($("overview-checked"));
    return;
  }
  const summary = snapshot.summary;
  overview.dataset.tone = toneOf(summary.tone);
  replace(iconSlot, toneIcon(summary.tone));
  if (state.headline !== summary.headline) {
    $("headline").textContent = summary.headline;
    state.headline = summary.headline;
  }
  $("overview-detail").textContent = summary.detail;
  const interval = snapshot.refresh?.intervalSeconds;
  const generated = parseTime(snapshot.generatedAt);
  replace(
    $("overview-checked"),
    h("strong", {}, generated === null ? "Not checked yet" : `Checked ${clockFormat.format(generated)}`),
    interval ? `Refreshes every ${interval} s` : null,
  );

  const counts = summary.counts;
  const roles = counts.roles;
  const apps = counts.applications;
  const operations = counts.operations;
  const backup = counts.backup;
  const expected = apps.total - apps.stopped;

  const roleModels = snapshot.roles ?? [];
  const failingRoles = roleModels.filter((item) => item.status.tone === "critical").length;
  const degradedRoles = roleModels.filter((item) => item.status.tone === "warning").length;
  const unverifiedRoles = roleModels.filter((item) => item.status.key === "unverified").length;
  const roleParts = [
    failingRoles ? `${failingRoles} failing` : null,
    degradedRoles ? `${degradedRoles} degraded` : null,
    unverifiedRoles ? `${unverifiedRoles} unverified` : null,
  ].filter(Boolean);
  const roleSub = roleParts.length ? roleParts.join(" · ") : "healthy";
  let roleTone = null;
  if (failingRoles) roleTone = "critical";
  else if (degradedRoles) roleTone = "warning";
  else if (unverifiedRoles) roleTone = "neutral";
  let appSub = "serving";
  let appTone = null;
  if (apps.total === 0) appSub = "none declared yet";
  else if (expected === 0) appSub = "all stopped";
  else if (apps.attention) {
    appSub = `${apps.attention} need attention`;
    appTone = snapshot.applications.some((item) => item.status.tone === "critical")
      ? "critical"
      : "warning";
  } else if (apps.changing) {
    appSub = `serving · ${apps.changing} changing`;
    appTone = "info";
  } else if (apps.stopped) appSub = `serving · ${apps.stopped} stopped`;

  let operationSub = "none running";
  let operationTone = null;
  if (operations.recovery) {
    operationSub = `${operations.recovery} need recovery`;
    operationTone = "warning";
  } else if (operations.running) {
    operationSub = "running now";
    operationTone = "info";
  }

  let backupValue = "—";
  let backupAgo = null;
  let backupSub = "no health evidence";
  let backupTone = null;
  if (typeof backup.ageHours === "number") {
    const age = backup.ageHours;
    backupValue = `${age < 10 ? age.toFixed(1) : Math.round(age)} h`;
    backupAgo = " ago";
    backupSub = backup.offsite === "passed" ? "off-site copy verified" : "encrypted set";
  }
  if (backup.state === "failed") {
    backupSub = "backup check failed";
    backupTone = "warning";
  } else if (backup.state === "not-run") {
    backupSub = "not checked this run";
    backupTone = "neutral";
  } else if (backup.offsite === "failed") {
    backupSub = "off-site check failed";
    backupTone = "warning";
  }

  replace(
    $("stats"),
    stat(
      "i-roles",
      "Infrastructure roles",
      String(roles.healthy),
      `/${roles.total}`,
      roleSub,
      roleTone,
    ),
    stat(
      "i-apps",
      "Applications",
      String(apps.serving),
      expected > 0 ? `/${expected}` : null,
      appSub,
      appTone,
    ),
    stat("i-activity", "Operations", String(operations.running), null, operationSub, operationTone),
    stat("i-backup", "Newest backup", backupValue, backupAgo, backupSub, backupTone),
  );
}

// --------------------------------------------------------------------- Issues

function highlightRole(role) {
  const card = $(`role-${role}`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.dataset.highlight = "true";
  setTimeout(() => {
    card.dataset.highlight = "false";
  }, 2200);
}

function showAttention() {
  state.filter = "attention";
  renderApplications(state.snapshot);
  $("applications-section").scrollIntoView({ behavior: "smooth", block: "start" });
}

// One cause (for example, a failed wildcard route) can fail every application
// identically; collapse three or more identical rows into one.
function groupIssues(issues) {
  const key = (issue) => `${issue.tone}|${issue.summary}|${issue.detail}`;
  const groups = new Map();
  for (const issue of issues) {
    if (issue.scope !== "application") continue;
    groups.set(key(issue), [...(groups.get(key(issue)) ?? []), issue]);
  }
  const emitted = new Set();
  const rows = [];
  for (const issue of issues) {
    const group = issue.scope === "application" ? groups.get(key(issue)) : null;
    if (!group || group.length < 3) {
      rows.push(issue);
      continue;
    }
    if (emitted.has(key(issue))) continue;
    emitted.add(key(issue));
    const names = group.slice(0, 3).map((item) => item.subject);
    const more = group.length > 3 ? ` and ${group.length - 3} more` : "";
    rows.push({
      ...issue,
      scope: "applications",
      subject: plural(group.length, "application"),
      detail: `${sentence(issue.detail)} · ${names.join(", ")}${more}`,
    });
  }
  return rows;
}

function renderIssues(snapshot) {
  const section = $("issues-section");
  const issues = snapshot.issues ?? [];
  if (snapshot.state !== "ready" || issues.length === 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  $("issues-count").textContent = String(issues.length);
  replace(
    $("issue-list"),
    groupIssues(issues).map((issue) => {
      let action = null;
      if (issue.scope === "application") action = () => openDrawer(issue.target);
      else if (issue.scope === "applications") action = showAttention;
      else if (issue.scope === "role") action = () => highlightRole(issue.target);
      return h(
        "li",
        { class: "issue" },
        h(
          "button",
          {
            class: "issue__button",
            type: "button",
            disabled: action === null,
            onclick: action,
            "data-focus-key": `issue:${issue.scope}:${issue.target}:${issue.summary}`,
          },
          toneIcon(issue.tone),
          h(
            "span",
            {},
            h("span", { class: "issue__title" }, issue.subject),
            h("span", { class: "issue__subject" }, ` — ${issue.summary}`),
            // The leading space separates words in the accessible name; layout collapses it.
            issue.detail ? h("span", { class: "issue__detail" }, " ", sentence(issue.detail)) : null,
          ),
          action ? h("span", { class: "issue__go" }, icon("i-chevron")) : h("span"),
        ),
      );
    }),
  );
}

// ---------------------------------------------------------------------- Roles

const ROLE_ICONS = {
  admin: "i-admin",
  ingress: "i-ingress",
  storage: "i-storage",
  worker: "i-worker",
  builder: "i-builder",
};

function roleCard(role) {
  const signals = h("dl", { class: "signals" });
  for (const signal of role.signals ?? []) {
    const tone = toneOf(signal.tone);
    const row = h(
      "div",
      { class: "signal", title: sentence(signal.detail) || null },
      h("dt", {}, signal.label),
      h("dd", {}, dot(tone), h("span", { class: "signal__value" }, signal.value)),
    );
    if (signal.detail && (tone === "critical" || tone === "warning")) {
      row.append(h("span", { class: "signal__detail" }, sentence(signal.detail)));
    }
    signals.append(row);
  }
  for (const fact of role.facts ?? []) {
    signals.append(
      h(
        "div",
        { class: "signal" },
        h("dt", {}, fact.label),
        h("dd", {}, h("span", { class: "signal__value signal__value--fact" }, fact.value)),
      ),
    );
  }
  const image = role.image;
  const foot = h("div", { class: "role-card__foot" });
  if (image && (image.name || image.commit)) {
    foot.append(
      h("span", { class: "role-card__image mono", title: image.name || "" }, image.name || "Image"),
    );
    if (image.commit) {
      foot.append(
        h("span", { class: "chip", title: image.commit }, icon("i-commit"), h("span", { class: "mono" }, image.commit.slice(0, 7))),
      );
    }
    if (image.selectedAt) foot.append(timeElement(image.selectedAt, "Selected "));
  } else {
    foot.append(h("span", {}, "No image selection recorded"));
  }
  return h(
    "article",
    {
      class: "role-card card",
      id: `role-${role.role}`,
      "data-role": role.role,
      "data-lifetime": role.lifetime,
      "aria-labelledby": `role-${role.role}-name`,
    },
    h(
      "div",
      { class: "role-card__head" },
      h("span", { class: "role-card__icon", "aria-hidden": "true" }, icon(ROLE_ICONS[role.role] ?? "i-roles")),
      h(
        "div",
        { class: "role-card__title" },
        h("h3", { class: "role-card__name", id: `role-${role.role}-name` }, role.name),
        h("div", { class: "role-card__lifetime" }, role.lifetimeLabel),
      ),
      pill(role.status),
    ),
    h("p", { class: "role-card__summary" }, role.summary),
    signals,
    foot,
  );
}

function renderRoles(snapshot) {
  if (snapshot.state !== "ready") return;
  replace($("roles"), (snapshot.roles ?? []).map(roleCard));
  const healthy = snapshot.summary?.counts?.roles;
  $("roles-note").textContent = healthy
    ? `${healthy.healthy} of ${healthy.total} healthy · persistent hosts, replaceable workers, single-use builders`
    : "";
}

// A failed controller read keeps the last accepted records visible; say so.
function staleNotice(snapshot) {
  const controller = snapshot.sources?.find((source) => source.key === "controller");
  if (!controller || controller.ok) return null;
  const since = controller.observedAt
    ? ["Showing controller records from ", timeElement(controller.observedAt), ". "]
    : [];
  return h(
    "div",
    { class: "stale-note", role: "note" },
    toneIcon("warning"),
    h("span", {}, since, sentence(controller.error) ?? "The latest controller read failed.", "."),
  );
}

// --------------------------------------------------------------- Applications

const FILTERS = [
  { key: "all", label: "All", test: (app) => !app.deletedAt },
  {
    key: "attention",
    label: "Needs attention",
    test: (app) => !app.deletedAt && ["critical", "warning"].includes(app.status.tone),
  },
  { key: "serving", label: "Serving", test: (app) => app.status.key === "serving" },
  { key: "changing", label: "Changing", test: (app) => app.status.tone === "info", optional: true },
  { key: "stopped", label: "Stopped", test: (app) => app.status.key === "stopped" },
  { key: "deleted", label: "Deleted", test: (app) => Boolean(app.deletedAt), optional: true },
];

function setupFilters() {
  const container = $("app-filters");
  for (const filter of FILTERS) {
    const button = h(
      "button",
      {
        type: "button",
        "data-filter": filter.key,
        "aria-pressed": filter.key === state.filter ? "true" : "false",
        hidden: Boolean(filter.optional),
      },
      h("span", {}, filter.label),
      h("span", { class: "segmented__count" }),
    );
    button.addEventListener("click", () => {
      state.filter = filter.key;
      renderApplications(state.snapshot);
    });
    container.append(button);
  }
  $("app-search").addEventListener("input", (event) => {
    state.query = event.target.value.trim().toLowerCase();
    renderApplications(state.snapshot);
  });
  $("app-sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    renderApplications(state.snapshot);
  });
}

// Problems first, then work in progress, then healthy and intentionally idle.
const STATUS_RANK = {
  failing: 0,
  "needs-recovery": 1,
  unverified: 2,
  "not-deployed": 3,
  changing: 4,
  unknown: 5,
  serving: 6,
  stopped: 7,
  deleted: 8,
};

function matchesQuery(app, query) {
  if (!query) return true;
  const haystack = [
    app.slug,
    app.url,
    app.id,
    app.deployment?.id,
    app.deployment?.commit,
    app.deployment?.ref,
    app.status?.label,
  ];
  return haystack.some((value) => typeof value === "string" && value.toLowerCase().includes(query));
}

function compareApps(left, right) {
  if (state.sort === "name") return left.slug.localeCompare(right.slug);
  if (state.sort === "deployed") {
    const a = parseTime(left.deployment?.acceptedAt) ?? -Infinity;
    const b = parseTime(right.deployment?.acceptedAt) ?? -Infinity;
    return b - a || left.slug.localeCompare(right.slug);
  }
  if (state.sort === "latency") {
    const a = left.route?.latencyMs ?? -1;
    const b = right.route?.latencyMs ?? -1;
    return b - a || left.slug.localeCompare(right.slug);
  }
  const rank = (app) => STATUS_RANK[app.status.key] ?? STATUS_RANK.unknown;
  return rank(left) - rank(right) || left.slug.localeCompare(right.slug);
}

function historyBars(checks, label) {
  const recent = (checks ?? []).slice(-HISTORY_SLOTS);
  const passed = recent.filter((check) => check.tone === "good").length;
  const failed = recent.filter((check) => check.tone === "critical").length;
  const container = h("span", {
    class: "bars",
    role: "img",
    "aria-label": recent.length
      ? `${label}: last ${plural(recent.length, "check")}, ${passed} passed, ${failed} failed`
      : `${label}: no checks recorded yet`,
  });
  for (let index = recent.length; index < HISTORY_SLOTS; index += 1) {
    container.append(h("span", { class: "bar" }));
  }
  for (const check of recent) {
    const word = { good: "Passed", warning: "Unverified", critical: "Failed" }[check.tone] ?? "Unknown";
    container.append(
      h("span", { class: "bar", "data-tone": toneOf(check.tone), title: `${word} · ${absolute(check.at)}` }),
    );
  }
  return container;
}

function routeText(route) {
  if (!route || route.outcome === "not-checked") return null;
  const latency = typeof route.latencyMs === "number" ? ` · ${numberFormat.format(route.latencyMs)} ms` : "";
  return `${route.summary}${latency}`;
}

function healthCell(app) {
  const text = routeText(app.route);
  if (text === null) {
    return h(
      "div",
      { title: app.route?.detail || null },
      h("div", { class: "cell-line muted" }, h("span", {}, "Not checked")),
    );
  }
  const latency = app.route.latencyMs;
  return h(
    "div",
    { title: app.route.detail || null },
    h(
      "div",
      { class: "cell-line" },
      h("span", { class: "health-value" }, app.route.summary),
      typeof latency === "number"
        ? h("span", { class: "health-latency" }, `${numberFormat.format(latency)} ms`)
        : null,
    ),
    historyBars(app.checks, "Health checks"),
  );
}

function deployCell(app) {
  const deployment = app.deployment;
  if (!deployment) {
    const latest = app.attempts?.[0];
    return h(
      "div",
      {},
      h("div", { class: "cell-line muted" }, h("span", {}, latest ? latest.status.label : "No deployment")),
      latest ? h("div", { class: "cell-sub" }, timeElement(latest.requestedAt, "Requested ")) : null,
    );
  }
  return h(
    "div",
    {},
    h(
      "div",
      { class: "cell-line" },
      h("span", { class: "mono", title: deployment.commit || "" }, deployment.commit ? deployment.commit.slice(0, 7) : "—"),
      deployment.ref ? h("span", { class: "ref", title: deployment.ref }, icon("i-branch"), h("span", {}, deployment.ref)) : null,
    ),
    h("div", { class: "cell-sub" }, timeElement(deployment.acceptedAt, "Accepted ")),
  );
}

function resourcesCell(app) {
  const size = [formatCpu(app.sizing?.cpuMHz), formatMemory(app.sizing?.memoryMiB)].filter(Boolean).join(" · ");
  const runtime = app.deployment?.runtime;
  return h(
    "div",
    {},
    h(
      "div",
      { class: "cell-line" },
      runtime ? h("span", { class: "chip" }, runtime) : null,
      h("span", { class: size ? "" : "muted" }, size || "Default size"),
    ),
    app.storage?.length
      ? h(
          "div",
          { class: "storage-tags" },
          app.storage.map((resource) =>
            h(
              "span",
              { class: "chip", title: `${resource.typeLabel} · ${resource.label || resource.name} · ${resource.status.label}` },
              resource.typeLabel,
            ),
          ),
        )
      : null,
  );
}

function appRow(app) {
  const open = () => openDrawer(app.id);
  const host = app.url
    ? externalLink(app.url, hostOf(app.url), "app-host")
    : h("span", { class: "app-host" }, h("span", {}, "No public URL"));
  host.addEventListener("click", (event) => event.stopPropagation());
  return h(
    "tr",
    { role: "row", onclick: open },
    h(
      "td",
      { class: "cell-app", role: "cell" },
      h(
        "button",
        {
          class: "app-name",
          type: "button",
          "aria-label": `${app.slug}, ${app.status.label}. Open details`,
          "data-focus-key": `app:${app.id}`,
          onclick: (event) => {
            event.stopPropagation();
            open();
          },
        },
        app.slug,
      ),
      host,
    ),
    h("td", { class: "cell-status", role: "cell" }, pill(app.status)),
    h("td", { class: "cell-health", role: "cell" }, healthCell(app)),
    h("td", { class: "cell-deploy", role: "cell" }, deployCell(app)),
    h("td", { class: "cell-resources", role: "cell" }, resourcesCell(app)),
    h("td", { class: "cell-go go", role: "cell", "aria-hidden": "true" }, icon("i-chevron")),
  );
}

function renderApplications(snapshot) {
  if (!snapshot || snapshot.state !== "ready") return;
  const apps = snapshot.applications ?? [];
  const visible = apps.filter((app) => !app.deletedAt);
  $("applications-count").textContent = String(visible.length);

  for (const button of $("app-filters").querySelectorAll("button")) {
    const filter = FILTERS.find((item) => item.key === button.dataset.filter);
    const count = apps.filter(filter.test).length;
    button.querySelector(".segmented__count").textContent = String(count);
    button.hidden = Boolean(filter.optional) && count === 0 && state.filter !== filter.key;
    button.setAttribute("aria-pressed", filter.key === state.filter ? "true" : "false");
  }

  const body = $("apps-body");
  const notice = staleNotice(snapshot);
  if (apps.length === 0) {
    replace(
      body,
      notice,
      h(
        "div",
        { class: "empty" },
        icon("i-inbox"),
        h("div", { class: "empty__title" }, "No applications yet"),
        h("div", {}, "Applications appear here after they are declared through the controller."),
      ),
    );
    return;
  }
  const filter = FILTERS.find((item) => item.key === state.filter) ?? FILTERS[0];
  const rows = apps.filter((app) => filter.test(app) && matchesQuery(app, state.query)).sort(compareApps);
  if (rows.length === 0) {
    replace(
      body,
      notice,
      h(
        "div",
        { class: "empty" },
        icon("i-search"),
        h("div", { class: "empty__title" }, "No applications match"),
        h("div", {}, "Try another search or filter."),
        h(
          "button",
          {
            class: "empty__action",
            type: "button",
            "data-focus-key": "filters:clear",
            onclick: () => {
              state.filter = "all";
              state.query = "";
              $("app-search").value = "";
              renderApplications(state.snapshot);
              $("app-search").focus();
            },
          },
          "Clear filters",
        ),
      ),
    );
    return;
  }
  replace(
    body,
    notice,
    h(
      "table",
      { class: "app-table", role: "table" },
      h("caption", { class: "visually-hidden" }, "Applications and their latest public health checks"),
      h(
        "thead",
        { role: "rowgroup" },
        h(
          "tr",
          { role: "row" },
          h("th", { class: "col-app", scope: "col", role: "columnheader" }, "Application"),
          h("th", { class: "col-status", scope: "col", role: "columnheader" }, "Status"),
          h("th", { class: "col-health", scope: "col", role: "columnheader" }, "Health check"),
          h("th", { class: "col-deploy", scope: "col", role: "columnheader" }, "Deployment"),
          h("th", { class: "col-resources", scope: "col", role: "columnheader" }, "Resources"),
          h("th", { class: "col-go", scope: "col", role: "columnheader" }, h("span", { class: "visually-hidden" }, "Details")),
        ),
      ),
      h("tbody", { role: "rowgroup" }, rows.map(appRow)),
    ),
  );
}

// --------------------------------------------------------------------- Drawer

function kv(rows) {
  const list = h("dl", { class: "kv" });
  for (const [label, ...value] of rows) {
    if (value.length === 1 && (value[0] === null || value[0] === undefined)) continue;
    list.append(h("dt", {}, label), h("dd", {}, value));
  }
  return list;
}

function section(title, ...children) {
  return h("section", { class: "dsec" }, h("h3", { class: "dsec__title" }, title), children);
}

function lead(tone, title, detail) {
  return h(
    "div",
    { class: "dsec__lead" },
    toneIcon(tone),
    h("div", {}, h("strong", {}, title), detail ? h("span", {}, detail) : null),
  );
}

function idValue(value, label) {
  if (!value) return h("span", { class: "muted" }, "—");
  return [h("span", { class: "mono", title: value }, value), copyButton(value, label)];
}

function attemptItem(attempt) {
  return h(
    "li",
    { class: "attempt" },
    toneIcon(attempt.status.tone),
    h(
      "div",
      { class: "attempt__main" },
      h(
        "div",
        { class: "cell-line" },
        h("strong", {}, attempt.status.label),
        attempt.commit ? h("span", { class: "mono muted" }, attempt.commit.slice(0, 7)) : null,
        attempt.ref ? h("span", { class: "ref" }, icon("i-branch"), h("span", {}, attempt.ref)) : null,
      ),
      attempt.error ? h("div", { class: "attempt__error" }, attempt.error) : null,
    ),
    h("span", { class: "op__time" }, timeElement(attempt.requestedAt)),
  );
}

const QUOTA_LABELS = {
  postgresConnections: ["Connections", (value) => numberFormat.format(value)],
  measuredTargetBytes: ["Size target", formatBytes],
  s3Bytes: ["Bytes", formatBytes],
  s3Objects: ["Objects", (value) => numberFormat.format(value)],
};

function resourceCard(resource) {
  const quotas = Object.entries(resource.quotas ?? {})
    .filter(([key]) => key in QUOTA_LABELS)
    .map(([key, value]) => `${QUOTA_LABELS[key][0]} ${QUOTA_LABELS[key][1](value)}`);
  return h(
    "div",
    { class: "resource" },
    h(
      "div",
      { class: "resource__head" },
      h("span", { class: "chip" }, resource.typeLabel),
      h("span", { class: "resource__name" }, resource.label || resource.name),
      pill(resource.status),
    ),
    h(
      "div",
      { class: "resource__meta" },
      [quotas.join(" · ") || "No quota recorded", " · "],
      resource.lastVerifiedAt ? timeElement(resource.lastVerifiedAt, "Verified ") : "Never verified",
    ),
  );
}

function drawerContent(app) {
  const route = app.route;
  const deployment = app.deployment;
  const operation = app.operation;
  const parts = [];

  const leadDetail = operation && ["changing", "needs-recovery"].includes(app.status.key)
    ? `${operation.label} · ${operation.status.label}${operation.phase ? ` · ${operation.phase}` : ""}`
    : (app.recoveryNote ?? route?.detail);
  parts.push(
    h("section", { class: "dsec" }, lead(app.status.tone, app.status.label, sentence(leadDetail))),
  );

  const routeRows = [];
  if (route && route.outcome !== "not-checked") {
    routeRows.push(["Endpoint", externalLink(route.url, route.url.replace(/^https:\/\//, ""))]);
    routeRows.push(["Response", h("span", { class: "health-value" }, routeText(route))]);
    routeRows.push(["Checked", timeElement(route.checkedAt)]);
    routeRows.push(["Recent checks", historyBars(app.checks, "Health checks")]);
  } else {
    routeRows.push(["Result", route?.detail ?? "Not checked"]);
  }
  parts.push(section("Health check", kv(routeRows)));

  if (operation) {
    parts.push(
      section(
        "Operation in progress",
        kv([
          ["Kind", operation.label],
          ["Status", pill(operation.status)],
          ["Phase", operation.phase ?? "—"],
          ["Started", timeElement(operation.startedAt)],
          ["Updated", timeElement(operation.updatedAt)],
          ["Deadline", operation.deadlineAt ? timeElement(operation.deadlineAt) : null],
          ["Operation", idValue(operation.id, "operation ID")],
          ["Error", operation.error ?? null],
        ]),
      ),
    );
  }

  if (deployment) {
    const commit = deployment.commit
      ? [
          deployment.commitUrl
            ? externalLink(deployment.commitUrl, deployment.commit.slice(0, 12), "mono")
            : h("span", { class: "mono" }, deployment.commit.slice(0, 12)),
          copyButton(deployment.commit, "commit"),
        ]
      : h("span", { class: "muted" }, "—");
    parts.push(
      section(
        "Accepted deployment",
        kv([
          ["Commit", commit],
          ["Branch", deployment.ref ?? null],
          ["Repository", deployment.repository ? externalLink(deployment.repository, deployment.repository.replace(/^https:\/\/github\.com\//, "")) : null],
          ["Runtime", [deployment.runtime ?? "Unknown", deployment.port ? ` · port ${deployment.port}` : ""].join("")],
          ["Health path", deployment.healthPath ? h("span", { class: "mono" }, deployment.healthPath) : null],
          ["Accepted", timeElement(deployment.acceptedAt)],
          ["Last healthy", deployment.lastHealthyAt ? timeElement(deployment.lastHealthyAt) : null],
          [
            "Configuration",
            typeof deployment.configurationRevision === "number"
              ? `Revision ${deployment.configurationRevision}`
              : null,
          ],
          ["Image", deployment.imageDigest ? [h("span", { class: "mono", title: deployment.imageDigest }, `…${deployment.imageDigest.slice(-19)}`), copyButton(deployment.imageDigest, "image digest")] : null],
          ["Deployment", idValue(deployment.id, "deployment ID")],
        ]),
      ),
    );
  }

  if (app.attempts?.length) {
    parts.push(section("Recent deployments", h("ul", { class: "attempts" }, app.attempts.map(attemptItem))));
  }

  parts.push(
    section(
      "Sizing",
      kv([
        ["Worker flavor", app.sizing?.flavor ? h("span", { class: "mono" }, app.sizing.flavor) : "Platform default"],
        ["CPU", formatCpu(app.sizing?.cpuMHz) ?? "Platform default"],
        ["Memory", formatMemory(app.sizing?.memoryMiB) ?? "Platform default"],
      ]),
    ),
  );

  parts.push(
    section(
      "Managed storage",
      app.storage?.length
        ? app.storage.map(resourceCard)
        : h("p", { class: "muted" }, "No managed PostgreSQL, MongoDB, or S3 resources."),
    ),
  );

  parts.push(
    section(
      "Identifiers",
      kv([
        ["Application", idValue(app.id, "application ID")],
        ["Created", timeElement(app.createdAt)],
        ["Updated", timeElement(app.updatedAt)],
        ["Deleted", app.deletedAt ? timeElement(app.deletedAt) : null],
      ]),
    ),
  );
  return parts;
}

function openDrawer(id) {
  state.openApp = id;
  const dialog = $("app-drawer");
  if (!renderDrawer()) return;
  if (!dialog.open) {
    dialog.showModal();
    $("drawer-body").scrollTop = 0;
    $("drawer-close").focus();
  }
}

function renderDrawer() {
  const dialog = $("app-drawer");
  if (state.openApp === null) return false;
  const app = state.snapshot?.applications?.find((item) => item.id === state.openApp);
  if (!app) {
    if (dialog.open) dialog.close();
    state.openApp = null;
    return false;
  }
  $("drawer-title").textContent = app.slug;
  replace(
    $("drawer-subtitle"),
    pill(app.status),
    app.url ? externalLink(app.url, hostOf(app.url)) : h("span", { class: "muted" }, "No public URL"),
  );
  const body = $("drawer-body");
  const scroll = body.scrollTop;
  replace(body, drawerContent(app));
  body.scrollTop = scroll;
  return true;
}

function setupDrawer() {
  const dialog = $("app-drawer");
  $("drawer-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    state.openApp = null;
  });
}

// ---------------------------------------------------------- Operations, checks

function operationItem(operation) {
  const subject = operation.applicationId
    ? h(
        "button",
        {
          class: "op__subject--link",
          type: "button",
          "data-focus-key": `op:${operation.id}`,
          onclick: () => openDrawer(operation.applicationId),
        },
        operation.subject,
      )
    : h("span", { class: "op__subject" }, operation.subject);
  const meta = [operation.status.label, operation.phase].filter(Boolean).join(" · ");
  const showError = operation.error && operation.status.key !== "succeeded";
  return h(
    "li",
    { class: "op" },
    toneIcon(operation.status.tone),
    h(
      "div",
      {},
      h("div", {}, h("span", { class: "op__title" }, operation.label), h("span", { class: "op__subject" }, " · "), subject),
      h("div", { class: "op__meta" }, meta, operation.startedAt ? [" · ", timeElement(operation.startedAt, "started ")] : null),
      showError ? h("div", { class: "op__error" }, operation.error) : null,
    ),
    h("span", { class: "op__time" }, timeElement(operation.updatedAt)),
  );
}

function renderOperations(snapshot) {
  if (snapshot.state !== "ready") return;
  const operations = snapshot.operations ?? [];
  const container = $("operations");
  const running = operations.filter((item) => item.status.key === "running").length;
  const recovery = operations.filter((item) => item.status.key === "recovery_required").length;
  const notes = [];
  if (running) notes.push(`${running} running`);
  if (recovery) notes.push(`${recovery} need recovery`);
  if (!notes.length) notes.push("Most recent first");
  if (snapshot.operationsTruncated) notes.push("older operations not loaded");
  $("operations-note").textContent = notes.join(" · ");
  const notice = staleNotice(snapshot);
  if (operations.length === 0) {
    replace(
      container,
      notice,
      h(
        "div",
        { class: "empty empty--compact" },
        icon("i-activity"),
        h("div", { class: "empty__title" }, "No operations recorded"),
        h("div", {}, "Deployments, storage changes, and host operations appear here."),
      ),
    );
    return;
  }
  const shown = state.showAllOperations ? operations : operations.slice(0, OPERATION_PREVIEW);
  const list = h("ul", { class: "op-list" }, shown.map(operationItem));
  const more =
    operations.length > OPERATION_PREVIEW
      ? h(
          "button",
          {
            class: "list-more",
            type: "button",
            "data-focus-key": "operations:more",
            onclick: () => {
              state.showAllOperations = !state.showAllOperations;
              renderOperations(state.snapshot);
            },
          },
          state.showAllOperations ? "Show fewer" : `Show all ${operations.length}`,
        )
      : null;
  replace(container, notice, list, more);
}

const CHECK_STATES = { passed: "Passed", failed: "Failed", "not-run": "Skipped", unavailable: "Unavailable" };

function renderChecks(snapshot) {
  if (snapshot.state !== "ready") return;
  const checks = snapshot.checks ?? { items: [] };
  const note = $("checks-note");
  if (checks.available && checks.checkedAt) {
    replace(
      note,
      "Health timer ",
      timeElement(checks.checkedAt),
      checks.stale ? h("span", { class: "stale-chip" }, "Stale") : null,
    );
  } else {
    replace(note, "No snapshot yet");
  }
  if (!checks.available) {
    const reason = snapshot.sources?.find((source) => source.key === "health")?.error;
    replace(
      $("checks"),
      h(
        "li",
        { class: "empty empty--compact" },
        icon("i-shield"),
        h("div", { class: "empty__title" }, "No platform-health snapshot"),
        h("div", {}, sentence(reason) || "The admin health timer has not reported yet."),
      ),
    );
    return;
  }
  replace(
    $("checks"),
    checks.items.map((check) =>
      h(
        "li",
        { class: "check" },
        toneIcon(check.tone),
        h(
          "div",
          {},
          h("div", { class: "check__title" }, check.label),
          h("div", { class: "check__detail" }, sentence(check.detail)),
        ),
        h("span", { class: "check__state" }, CHECK_STATES[check.state] ?? check.state),
      ),
    ),
  );
}

// --------------------------------------------------------------------- Footer

function renderFooter(snapshot) {
  const footer = $("footer");
  const parts = [h("span", {}, "Read-only view. Nothing on this page can change the platform.")];
  if (snapshot.state === "ready") {
    parts.push(
      h(
        "ul",
        { class: "sources", "aria-label": "Data sources" },
        (snapshot.sources ?? []).map((source) =>
          h(
            "li",
            {
              class: "source",
              title: sentence(source.error) || (source.observedAt ? absolute(source.observedAt) : ""),
            },
            dot(source.ok ? "good" : "warning"),
            h("span", {}, source.label),
            source.observedAt ? timeElement(source.observedAt) : h("span", {}, "never"),
          ),
        ),
      ),
    );
  }
  const release = snapshot.platform?.release;
  if (release) {
    parts.push(h("span", { class: "footer__release", title: release }, "Release ", h("span", { class: "mono" }, release.slice(0, 7))));
  }
  replace(footer, parts);
}

// ------------------------------------------------------------- Render, polling

function focusKey() {
  const active = document.activeElement;
  if (!(active instanceof Element)) return null;
  return active.closest("[data-focus-key]")?.getAttribute("data-focus-key") ?? null;
}

// Renders replace nodes; put keyboard focus back on the equivalent element.
function restoreFocus(key) {
  const active = document.activeElement;
  if (key === null || (active && active !== document.body && active.isConnected)) return;
  const dialog = $("app-drawer");
  const scope = dialog.open ? dialog : document;
  scope.querySelector(`[data-focus-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
}

function render() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  const focused = focusKey();
  renderHeader(snapshot);
  renderOverview(snapshot);
  renderIssues(snapshot);
  renderRoles(snapshot);
  renderApplications(snapshot);
  renderOperations(snapshot);
  renderChecks(snapshot);
  renderFooter(snapshot);
  renderFreshness();
  if (state.openApp !== null) renderDrawer();
  restoreFocus(focused);
}

function tickRelativeTimes() {
  const now = Date.now();
  for (const element of document.querySelectorAll("time[data-relative]")) {
    element.textContent = (element.dataset.prefix ?? "") + relative(element.dataset.relative, now);
  }
}

function nextDelay() {
  if (document.hidden) return HIDDEN_POLL_MS;
  const snapshot = state.snapshot;
  const busy =
    !snapshot ||
    snapshot.state === "pending" ||
    snapshot.refresh?.inProgress ||
    Date.now() < state.expectRefreshUntil;
  return busy || !state.connected ? BUSY_POLL_MS : POLL_MS;
}

async function poll() {
  if (state.inFlight) {
    state.pollAgain = true;
    return;
  }
  state.inFlight = true;
  clearTimeout(state.timer);
  try {
    const headers = state.etag ? { "If-None-Match": state.etag } : {};
    const response = await fetch(SNAPSHOT_URL, { headers, cache: "no-store", credentials: "same-origin" });
    if (response.status === 304) {
      state.connected = true;
    } else if (response.ok) {
      const snapshot = await response.json();
      state.etag = response.headers.get("ETag");
      state.snapshot = snapshot;
      state.connected = true;
      // The server has picked up a requested refresh once its start time moves.
      if (snapshot.refresh?.inProgress || snapshot.refresh?.startedAt !== state.refreshBaseline) {
        state.expectRefreshUntil = 0;
      }
      const contentKey = JSON.stringify({ ...snapshot, refresh: null });
      if (contentKey !== state.contentKey) {
        state.contentKey = contentKey;
        render();
      }
    } else {
      throw new Error(`snapshot request failed with HTTP ${response.status}`);
    }
    state.lastContact = Date.now();
  } catch {
    state.connected = false;
  } finally {
    state.inFlight = false;
  }
  renderConnection();
  renderFreshness();
  if (state.pollAgain) {
    state.pollAgain = false;
    poll();
    return;
  }
  state.timer = setTimeout(poll, nextDelay());
}

async function requestRefresh() {
  const button = $("refresh-button");
  if (button.getAttribute("aria-busy") === "true") {
    toast("A refresh is already running");
    return;
  }
  button.setAttribute("aria-busy", "true");
  state.refreshBaseline = state.snapshot?.refresh?.startedAt ?? null;
  state.expectRefreshUntil = Date.now() + 15_000;
  try {
    const response = await fetch(REFRESH_URL, {
      method: "POST",
      headers: { "X-Dashboard-Refresh": "1" },
      cache: "no-store",
      credentials: "same-origin",
    });
    const result = response.ok ? await response.json() : { accepted: false };
    if (!result.accepted) {
      state.expectRefreshUntil = 0;
      toast("The platform was checked moments ago");
    }
  } catch {
    state.expectRefreshUntil = 0;
    toast("Cannot reach the dashboard service");
  }
  poll();
}

// ---------------------------------------------------------------------- Theme

const THEMES = {
  system: { next: "light", icon: "i-theme-system", label: "Color theme: match system" },
  light: { next: "dark", icon: "i-theme-light", label: "Color theme: light" },
  dark: { next: "system", icon: "i-theme-dark", label: "Color theme: dark" },
};

function currentTheme() {
  const theme = document.documentElement.dataset.theme;
  return theme === "light" || theme === "dark" ? theme : "system";
}

function showTheme() {
  const theme = THEMES[currentTheme()];
  const button = $("theme-button");
  button.replaceChildren(icon(theme.icon));
  button.setAttribute("aria-label", theme.label);
  button.title = theme.label;
}

function cycleTheme() {
  const next = THEMES[currentTheme()].next;
  try {
    if (next === "system") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, next);
  } catch {
    // The choice still applies to this page view.
  }
  if (next === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = next;
  showTheme();
}

// ----------------------------------------------------------------------- Boot

function start() {
  setupFilters();
  setupDrawer();
  showTheme();
  $("refresh-button").addEventListener("click", requestRefresh);
  $("theme-button").addEventListener("click", cycleTheme);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });
  setInterval(() => {
    renderFreshness();
    tickRelativeTimes();
  }, 1000);
  poll();
}

start();
