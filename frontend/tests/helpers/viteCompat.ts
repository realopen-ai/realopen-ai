/**
 * Vite-compatibility module hooks for the frontend test suite.
 *
 * WHY this exists: the test runner is plain `node --test` (no bundler), but
 * most src modules use Vite-only features that Node cannot handle natively:
 *
 *   1. `@/...` import specifiers — vite.config.ts maps the alias `@` to
 *      `<frontend>/src`. Node resolves bare specifiers against node_modules
 *      and fails with ERR_MODULE_NOT_FOUND. These hooks mirror the alias by
 *      rewriting `@/x` to the matching file under src/ (exact, `x.ts`, or
 *      `x/index.ts` — the same resolution order Vite's resolver uses).
 *
 *   2. `src/i18n/index.ts` imports `./locales/en.json` etc. Vite's json
 *      plugin serves the parsed object as the default export with no import
 *      attributes; Node's native JSON modules require `with { type: "json" }`
 *      on every import statement, which the src files don't carry. The load
 *      hook serves the REAL file contents as a tiny ES module with the
 *      identical default export.
 *
 *   3. `src/lib/debug.ts` reads `import.meta.env.VITE_DEBUG`. Vite injects
 *      `import.meta.env` at build time; under Node it is undefined, so the
 *      module throws `TypeError` the moment it is evaluated (which poisons
 *      every module that value-imports `@/lib/debug` — all API clients and
 *      several stores). The load hook prepends a single `import.meta.env
 *      ??= {}` line to the real source — i.e. it provides the same env
 *      contract Vite does — so the module's own code runs unchanged with an
 *      empty env (VITE_DEBUG unset → debug logging disabled, the production
 *      default).
 *
 * Everything else passes through the default hooks untouched, so TypeScript
 * stripping (`--experimental-strip-types`) keeps working.
 *
 * Usage — import this module BEFORE dynamically importing any src module
 * whose graph contains `@/` specifiers. It MUST be a dynamic import because
 * static imports of a module are resolved before any module body runs (the
 * hooks would not be registered yet):
 *
 *     import "./helpers/viteCompat.ts";
 *     const { useToolsStore } = await import("../src/store/toolsStore.ts");
 */
import { existsSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const srcRoot = fileURLToPath(new URL("../../src/", import.meta.url));

/** Resolve an `@/...` specifier the way vite.config.ts's alias does. */
function aliasTarget(specifier: string): string | null {
  const base = join(srcRoot, specifier.slice(2));
  if (existsSync(`${base}.ts`)) return `${base}.ts`;
  if (existsSync(join(base, "index.ts"))) return join(base, "index.ts");
  if (existsSync(base)) return base;
  return null;
}

function sourceText(
  source: string | ArrayBufferView | ArrayBuffer | null | undefined,
): string {
  if (typeof source === "string") return source;
  if (source == null) return "";
  return Buffer.from(
    source as Exclude<typeof source, string | null | undefined>,
  ).toString("utf8");
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      const target = aliasTarget(specifier);
      if (target) {
        return nextResolve(pathToFileURL(target).href, context);
      }
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    // Vite's json plugin behavior (see header comment, item 2).
    if (url.endsWith(".json")) {
      const data = JSON.parse(readFileSync(fileURLToPath(url), "utf8"));
      return {
        format: "module",
        shortCircuit: true,
        source: `export default ${JSON.stringify(data)}`,
      };
    }
    // Provide Vite's import.meta.env contract for the debug helpers (item 3).
    if (url.endsWith("/src/lib/debug.ts")) {
      const next = nextLoad(url, context);
      return {
        format: next.format,
        shortCircuit: true,
        source: `import.meta.env ??= {};\n${sourceText(next.source)}`,
      };
    }
    return nextLoad(url, context);
  },
});
