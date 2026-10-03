import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { themePlugin } from "../scripts/theme-plugin";

export default defineConfig({
  plugins: [react(), themePlugin()],
  resolve: { dedupe: ["react", "react-dom"] },
  base: "/",
  build: {
    outDir: "../../openstack_platform/dashboard/static",
    emptyOutDir: true,
    manifest: false,
    sourcemap: false,
    assetsInlineLimit: 0,
    cssCodeSplit: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
