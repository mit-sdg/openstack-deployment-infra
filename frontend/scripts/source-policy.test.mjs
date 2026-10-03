import { test } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { inspectSource } from "./check-source.mjs";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { execFileSync, spawnSync } from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
test("both app import directions and shared app dependencies are rejected", () => {
  for (const [app, other] of [
    ["owner-portal", "operator-dashboard"],
    ["operator-dashboard", "owner-portal"],
    ["shared", "owner-portal"],
    ["shared", "operator-dashboard"],
  ]) {
    const file = path.join(root, app, "src", "probe.tsx");
    assert.notEqual(
      inspectSource(file, app, {}, `import '../../${other}/src/App';`).length,
      0,
    );
  }
});
test("CSP source guard rejects first-party injection without scanning bundled React internals", () => {
  const file = path.join(root, "operator-dashboard/src/probe.tsx");
  for (const source of [
    "<div dangerouslySetInnerHTML={{__html: value}}/>",
    "eval(value)",
    "new Function(value)",
    "<div style={{color: value}}/>",
    "<style>{value}</style>",
    "document.createElement('style')",
  ])
    assert.notEqual(
      inspectSource(file, "operator-dashboard", {}, source).length,
      0,
    );
  assert.deepEqual(
    inspectSource(file, "operator-dashboard", {}, "<div>{value}</div>"),
    [],
  );
});
test("shared UI cannot acquire a network or router dependency", () => {
  const file = path.join(root, "shared/src/probe.tsx");
  for (const source of [
    "fetch('/api/snapshot')",
    "import {Link} from 'wouter'",
    "import {useQuery} from '@tanstack/react-query'",
  ])
    assert.notEqual(inspectSource(file, "shared", {}, source).length, 0);
});

test("freshness accepts clean output and rejects changed, untracked and staged output", () => {
  const repository = mkdtempSync(path.join(tmpdir(), "dashboard-freshness-"));
  try {
    const scripts = path.join(repository, "frontend/scripts");
    const assets = path.join(repository, "openstack_platform/dashboard/static");
    mkdirSync(scripts, { recursive: true });
    mkdirSync(assets, { recursive: true });
    copyFileSync(
      path.join(root, "scripts/check-freshness.mjs"),
      path.join(scripts, "check-freshness.mjs"),
    );
    writeFileSync(path.join(assets, "index.html"), "fixture entry");
    const git = (...args) =>
      execFileSync("git", args, { cwd: repository, stdio: "pipe" });
    git("init", "-q");
    git("add", ".");
    git(
      "-c",
      "user.name=Frontend Tests",
      "-c",
      "user.email=tests@example.invalid",
      "commit",
      "-qm",
      "fixture",
    );
    const check = () =>
      spawnSync(process.execPath, [path.join(scripts, "check-freshness.mjs")], {
        cwd: repository,
      });
    assert.equal(check().status, 0);
    writeFileSync(path.join(assets, "index.html"), "changed entry");
    assert.notEqual(check().status, 0);
    git("checkout", "--", "openstack_platform/dashboard/static/index.html");
    writeFileSync(path.join(assets, "unexpected.js"), "// untracked");
    assert.notEqual(check().status, 0);
    git("add", "openstack_platform/dashboard/static/unexpected.js");
    assert.notEqual(check().status, 0);
  } finally {
    rmSync(repository, { recursive: true, force: true });
  }
});
