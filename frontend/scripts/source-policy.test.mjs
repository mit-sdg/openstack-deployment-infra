import { test } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { inspectSource } from "./check-source.mjs";

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
