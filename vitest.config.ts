import { defineConfig } from "vitest/config";
import path from "node:path";

/**
 * Vitest config — TS unit/integration tests live alongside the Python pytest
 * suite under `tests/`. We narrow `include` to `*.test.ts` so vitest doesn't
 * try to run the Python test files (which all start with `test_*.py`).
 */
export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
});
