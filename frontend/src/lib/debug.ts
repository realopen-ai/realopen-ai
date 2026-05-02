/**
 * Centralised debug-logging utilities for the RealOpen-AI frontend.
 *
 * When the environment variable ``VITE_DEBUG`` is set to ``true``,
 * every call to ``dbg()`` prints to the console.  When it is false /
 * missing the calls are no-ops so there is zero runtime overhead.
 *
 * Usage in any module::
 *
 *     import { dbg, isDebug, createDebugLogger } from "@/lib/debug";
 *
 *     const log = createDebugLogger("stream");
 *     log("SSE chunk received", data);
 */

const _isDebug: boolean =
  import.meta.env.VITE_DEBUG?.toString().toLowerCase() === "true";

/** Whether debug mode is active. */
export function isDebug(): boolean {
  return _isDebug;
}

/** Tag used as the prefix for every debug log line. */
const TAG = "[RealOpen-AI]";

/**
 * Log a debug message when VITE_DEBUG=true.
 */
export function dbg(...args: unknown[]): void {
  if (_isDebug) {
    console.log(TAG, ...args);
  }
}

/**
 * Log a warning when VITE_DEBUG=true.
 */
export function dbgWarn(...args: unknown[]): void {
  {
    if (_isDebug) {
      console.warn(TAG, ...args);
    }
  }
}

/**
 * Log an error when VITE_DEBUG=true.
 */
export function dbgError(...args: unknown[]): void {
  if (_isDebug) {
    console.error(TAG, ...args);
  }
}

/**
 * Create a scoped debug logger that prepends a module name.
 */
export function createDebugLogger(scope: string) {
  const prefix = `${TAG}[${scope}]`;
  return (...args: unknown[]) => {
    if (_isDebug) {
      console.log(prefix, ...args);
    }
  };
}
