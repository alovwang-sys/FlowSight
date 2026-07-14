import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { build, type InlineConfig } from "vite";

import { App } from "./App";
import { flowsightViteConfig } from "../vite.config";

function outputSnapshot(root: string): Record<string, string> {
  const result: Record<string, string> = {};

  function visit(directory: string) {
    for (const name of readdirSync(directory)) {
      const path = join(directory, name);
      const metadata = statSync(path);
      if (metadata.isDirectory()) {
        visit(path);
      } else {
        expect(metadata.isFile()).toBe(true);
        result[relative(root, path)] = createHash("sha256")
          .update(readFileSync(path))
          .digest("hex");
      }
    }
  }

  visit(root);
  return result;
}

describe("FlowSight Phase 0 shell", () => {
  it("renders the deterministic empty state without requesting runtime data", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const markup = renderToStaticMarkup(<App />);

    expect(markup).toContain("Your FlowSight shell is ready.");
    expect(markup).toContain("No runtime data in this Phase 0 shell");
    expect(markup).toContain("Read-only UI bundle");
    expect(fetchMock).not.toHaveBeenCalled();

    vi.unstubAllGlobals();
  });

  it(
    "cleans stale output and rebuilds the same regular wheel payload",
    async () => {
      const temporaryRoot = mkdtempSync(join(tmpdir(), "flowsight-ui-build-"));
      const outputDirectory = join(temporaryRoot, "static");
      const staleAsset = join(outputDirectory, "assets", "stale.js");
      mkdirSync(join(outputDirectory, "assets"), { recursive: true });
      writeFileSync(staleAsset, "stale output must be removed\n");
      const buildOptions = {
        ...flowsightViteConfig,
        root: resolve(process.cwd(), "ui"),
        configFile: false,
        logLevel: "silent" as const,
        build: {
          ...flowsightViteConfig.build,
          outDir: outputDirectory,
        },
      } satisfies InlineConfig;

      try {
        expect(flowsightViteConfig.root).toBe("ui");
        expect(flowsightViteConfig.build?.outDir).toBe("../flowsight/static");
        expect(flowsightViteConfig.build?.emptyOutDir).toBe(true);
        expect(flowsightViteConfig.build?.assetsDir).toBe("assets");
        expect(flowsightViteConfig.build?.modulePreload).toEqual({ polyfill: false });
        await build(buildOptions);
        expect(statSync(outputDirectory).isDirectory()).toBe(true);
        expect(() => statSync(staleAsset)).toThrow();
        const firstBuild = outputSnapshot(outputDirectory);
        expect(Object.keys(firstBuild)).toContain("index.html");
        expect(Object.keys(firstBuild).some((name) => name.startsWith("assets/"))).toBe(
          true,
        );
        for (const name of Object.keys(firstBuild).filter((path) => path.endsWith(".js"))) {
          const javascript = readFileSync(join(outputDirectory, name), "utf8");
          expect(javascript).not.toMatch(/\bfetch\s*\(/);
          expect(javascript).not.toContain("XMLHttpRequest");
          expect(javascript).not.toContain("WebSocket");
          expect(javascript).not.toContain("EventSource");
          expect(javascript).not.toContain("/api/v1");
          expect(javascript).not.toContain("/internal/v1");
        }

        await build(buildOptions);
        expect(outputSnapshot(outputDirectory)).toEqual(firstBuild);
      } finally {
        rmSync(temporaryRoot, { force: true, recursive: true });
      }
    },
    15_000,
  );
});
