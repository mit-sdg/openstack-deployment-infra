import { readFileSync } from "node:fs";
import type { Plugin } from "vite";

// Keep production and the portal's optional Vite preview on the same external
// bootstrap. Storage keys belong to each app's HTML data attribute.
export function themePlugin(): Plugin {
  const source = () =>
    readFileSync(new URL("../shared/src/theme.js", import.meta.url));
  return {
    name: "shared-theme",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "theme.js", source: source() });
    },
    configureServer(server) {
      server.middlewares.use("/theme.js", (_request, response) => {
        response.setHeader("Content-Type", "text/javascript; charset=utf-8");
        response.end(source());
      });
    },
  };
}
