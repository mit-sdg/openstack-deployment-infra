import ts from "typescript";
import { readdirSync, readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const apps = ["shared", "owner-portal", "operator-dashboard"];
const failures = [];
function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(directory, entry.name);
    return entry.isDirectory()
      ? walk(full)
      : /\.[cm]?[jt]sx?$/.test(full)
        ? [full]
        : [];
  });
}
function check(file, app, options) {
  const source = ts.createSourceFile(
    file,
    readFileSync(file, "utf8"),
    ts.ScriptTarget.Latest,
    true,
  );
  const fail = (reason) =>
    failures.push(`${path.relative(root, file)}: ${reason}`);
  const isTest =
    /\.test\.[jt]sx?$/.test(file) || file.endsWith("test-setup.ts");
  function visit(node) {
    if (ts.isIdentifier(node) && node.text === "dangerouslySetInnerHTML")
      fail("HTML injection");
    if (ts.isJsxAttribute(node) && node.name.getText(source) === "style")
      fail("inline style");
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
      node.tagName.getText(source) === "style"
    )
      fail("injected stylesheet");
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      const name = node.expression.getText(source).split(".").at(-1);
      if (["eval", "Function"].includes(name)) fail("dynamic code execution");
      if (
        name === "createElement" &&
        node.arguments?.[0] &&
        ts.isStringLiteral(node.arguments[0]) &&
        node.arguments[0].text === "style"
      )
        fail("injected stylesheet");
      if (
        app === "shared" &&
        !isTest &&
        [
          "fetch",
          "XMLHttpRequest",
          "WebSocket",
          "EventSource",
          "sendBeacon",
        ].includes(name)
      )
        fail("network code in shared UI");
    }
    let specifier;
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
      node.moduleSpecifier &&
      ts.isStringLiteral(node.moduleSpecifier)
    )
      specifier = node.moduleSpecifier.text;
    if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword &&
      ts.isStringLiteral(node.arguments[0])
    )
      specifier = node.arguments[0].text;
    if (specifier) {
      const resolved = ts.resolveModuleName(
        specifier,
        file,
        options,
        ts.sys,
      ).resolvedModule;
      const target = resolved
        ? realpathSync(resolved.resolvedFileName)
        : path.resolve(path.dirname(file), specifier);
      for (const other of apps) {
        if (
          other !== app &&
          other !== "shared" &&
          target.startsWith(path.join(root, other) + path.sep)
        )
          fail(`imports ${other}`);
        if (
          app === "shared" &&
          other !== app &&
          target.startsWith(path.join(root, other) + path.sep)
        )
          fail(`shared imports ${other}`);
      }
      if (
        app === "shared" &&
        !isTest &&
        !specifier.startsWith(".") &&
        specifier !== "react" &&
        specifier !== "react/jsx-runtime"
      )
        fail(`non-presentation dependency ${specifier}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}
for (const app of apps) {
  const configPath = ts.findConfigFile(path.join(root, app), ts.sys.fileExists);
  if (!configPath) continue;
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  const parsed = ts.parseJsonConfigFileContent(
    config.config,
    ts.sys,
    path.dirname(configPath),
  );
  for (const file of walk(path.join(root, app, "src")))
    check(file, app, parsed.options);
}
if (failures.length) throw new Error(failures.join("\n"));
console.log("First-party CSP and app import boundaries passed");
