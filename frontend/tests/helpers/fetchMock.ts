/**
 * Fetch mocking for the frontend test suite (the runner has no jest/vitest,
 * so we swap globalThis.fetch like tests/sandboxLifecycle.test.ts does).
 *
 * All REST/SSE clients in src/api use RELATIVE urls ("/api/..."), which Node
 * cannot fetch at all (fetch("/api/x") throws before any network happens),
 * so an unmocked call fails loudly instead of reaching a real backend.
 *
 *   const mock = installFetch((url, init) => jsonResponse({ tools: [] }));
 *   ... call store actions ...
 *   mock.restore();
 */

export interface RecordedFetch {
  url: string;
  init?: RequestInit;
}

export interface FetchMock {
  /** Every fetch call in order (url + init), for asserting request shapes. */
  calls: RecordedFetch[];
  restore(): void;
}

/** Replace globalThis.fetch with `handler`; restores the original on restore(). */
export function installFetch(
  handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
): FetchMock {
  const original = globalThis.fetch;
  const calls: RecordedFetch[] = [];
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    calls.push({ url, init });
    return handler(url, init);
  }) as typeof fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

/** A canned JSON Response (status 200 by default). */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * A canned SSE Response: the given raw strings are streamed through a
 * ReadableStream so the clients' `res.body.getReader()` parsing loops run
 * for real. Join lines with "\n" exactly like an SSE body ("data: {...}\n\n").
 */
export function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** One SSE `data:` line helper. */
export function sseData(payload: unknown): string {
  return `data: ${JSON.stringify(payload)}\n\n`;
}

/**
 * A fetch handler serving exact-URL JSON routes. Any URL that is not in the
 * table throws — this makes unexpected/unmocked requests fail the test
 * instead of silently succeeding.
 */
export function jsonRoutes(
  routes: Record<string, unknown | Response>,
): (url: string) => Response {
  return (url) => {
    const route = routes[url];
    if (route === undefined) {
      throw new Error(`Unexpected fetch in test: ${url}`);
    }
    return route instanceof Response ? route : jsonResponse(route);
  };
}
