import { defineConfig, type UserConfig } from "vite";

export const flowsightViteConfig = {
  root: "ui",
  build: {
    assetsDir: "assets",
    emptyOutDir: true,
    modulePreload: { polyfill: false },
    outDir: "../flowsight/static",
    sourcemap: false,
  },
} satisfies UserConfig;

export default defineConfig(flowsightViteConfig);
