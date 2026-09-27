import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: "./setup-tests.ts",
      coverage: {
        provider: "v8",
        reporter: ["text", "json", "html", "cobertura"],
        include: ["src/**/*.{ts,tsx}"],
        exclude: [
          "**/dist/**",
          "**/node_modules/**",
          "**/*.config.*",
          "**/setup-tests.ts",
          "**/routeTree.gen.ts", // Auto-generated file cannot be tested
          "**/_demo/**", // Demo files will be deleted when starting a new project
          "**/main.tsx", // Entry point - primarily tested via integration tests
          "**/__root.tsx", // Route configuration - structural code with implicit branches from framework
        ],
        // thresholds: {
        //   lines: 80,
        //   functions: 80,
        //   branches: 80,
        //   statements: 80,
        // },
      },
    },
  }),
);
