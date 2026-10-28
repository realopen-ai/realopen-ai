import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.ts"],
    maxWorkers: 1,
    fileParallelism: false,
    restoreMocks: true,
    unstubGlobals: true,
  },
});
