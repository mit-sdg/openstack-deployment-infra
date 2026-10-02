import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
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
