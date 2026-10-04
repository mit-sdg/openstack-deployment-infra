import { readFileSync } from 'node:fs';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { themePlugin } from '../scripts/theme-plugin';

// Minification drops bundled packages' licence headers, so every build
// carries the third-party notices (fonts, QR encoder) as a static file.
function thirdPartyNotices() {
  return {
    name: 'third-party-notices',
    apply: 'build' as const,
    generateBundle(this: { emitFile: (file: object) => string }) {
      this.emitFile({
        type: 'asset',
        fileName: 'third-party-notices.txt',
        source: readFileSync(new URL('../THIRD_PARTY_NOTICES.md', import.meta.url), 'utf8'),
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), themePlugin(), thirdPartyNotices()],
  resolve: { dedupe: ['react', 'react-dom'] },
  build: { manifest: true, sourcemap: false },
  server: {
    proxy: Object.fromEntries(
      ['/api', '/auth', '/__test__'].map((path) => [
        path,
        {
          target: process.env.OWNER_PORTAL_API_TARGET ?? 'http://127.0.0.1:9443',
          changeOrigin: false,
        },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
