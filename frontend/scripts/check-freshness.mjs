import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repository = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const directory = "openstack_platform/dashboard/static";
execFileSync("git", ["diff", "--exit-code", "--", directory], {
  cwd: repository,
  stdio: "inherit",
});
const changes = execFileSync(
  "git",
  ["status", "--porcelain", "--untracked-files=all", "--", directory],
  { cwd: repository, encoding: "utf8" },
);
if (changes)
  throw new Error(`Dashboard build differs from committed output:\n${changes}`);
console.log(
  "Committed dashboard output is fresh, including the full file inventory",
);
