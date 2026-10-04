/**
 * Tests for src/lib/debug.ts — the Vite-gated debug-logging helpers.
 *
 * WHY source-text assertions + an import-rejection probe instead of direct
 * calls: the module reads `import.meta.env.VITE_DEBUG` at evaluation time.
 * `import.meta.env` is an object Vite INJECTS at build time; under plain Node
 * it is undefined, so the module throws TypeError the moment it is imported
 * (pinned by the first test). That also makes it impossible to exercise the
 * runtime branches here — so the behavioral contract is pinned against the
 * source: every log call must be a no-op unless VITE_DEBUG is exactly "true"
 * (case-insensitive), and every line must carry the "[RealOpen-AI]" tag.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/lib/debug.ts", import.meta.url),
  "utf8",
);

test("debug helpers require Vite's import.meta.env and fail fast outside a bundler", async () => {
  // Pins the environment contract: no silent no-op fallback — an import
  // under plain Node (e.g. server-side rendering or tests) surfaces the
  // missing build-time injection immediately.
  await assert.rejects(
    () => import("../src/lib/debug.ts"),
    (error: unknown) =>
      error instanceof TypeError &&
      /Cannot read properties of undefined \(reading 'VITE_DEBUG'\)/.test(
        error.message,
      ),
  );
});

test("debug mode activates only for the exact string 'true', case-insensitive", () => {
  assert.match(
    source,
    /import\.meta\.env\.VITE_DEBUG\?\.toString\(\)\.toLowerCase\(\) === "true"/,
  );
});

test("every log helper is a no-op unless debug mode is active", () => {
  // Zero runtime overhead when off is the module's stated contract.
  assert.match(
    source,
    /export function dbg\(\.\.\.args: unknown\[\]\): void \{\s*if \(_isDebug\) \{\s*console\.log\(TAG, \.\.\.args\);\s*\}\s*\}/,
  );
  assert.match(
    source,
    /export function dbgWarn\(\.\.\.args: unknown\[\]\): void \{\s*(?:\{\s*)?if \(_isDebug\) \{\s*console\.warn\(TAG, \.\.\.args\);\s*\}/,
  );
  assert.match(
    source,
    /export function dbgError\(\.\.\.args: unknown\[\]\): void \{\s*if \(_isDebug\) \{\s*console\.error\(TAG, \.\.\.args\);\s*\}\s*\}/,
  );
});

test("scoped loggers share the global tag and stay gated", () => {
  assert.match(source, /const TAG = "\[RealOpen-AI\]";/);
  assert.match(source, /const prefix = `\$\{TAG\}\[\$\{scope\}\]`;/);
  // The returned closure must also check the flag on every call, not bake
  // it in at creation time.
  assert.match(
    source,
    /return \(\.\.\.args: unknown\[\]\) => \{\s*if \(_isDebug\) \{\s*console\.log\(prefix, \.\.\.args\);/,
  );
});

test("exports the documented surface used across src/api and src/store", () => {
  for (const name of [
    "isDebug",
    "dbg",
    "dbgWarn",
    "dbgError",
    "createDebugLogger",
  ]) {
    assert.match(source, new RegExp(`export function ${name}\\(`));
  }
});
