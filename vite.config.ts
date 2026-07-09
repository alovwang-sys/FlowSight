import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  root: 'ui',
  base: './',
  plugins: [react()],
  server: {
    host: '127.0.0.1',
  },
  build: {
    outDir: '../flowsight/static',
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
  },
});
