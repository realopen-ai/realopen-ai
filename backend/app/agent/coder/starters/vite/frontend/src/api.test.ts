import { afterEach, expect, test, vi } from "vitest";
import { request } from "./api";

afterEach(() => vi.unstubAllGlobals());

test("returns API data using the supplied method and body", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{"id":1}'));
  vi.stubGlobal("fetch", fetch);
  const init = { method: "POST", body: '{"title":"Example"}' };
  expect(await request("/api/items", init)).toEqual({ id: 1 });
  expect(fetch).toHaveBeenCalledWith("/api/items", init);
});

test("reports API errors instead of treating them as success", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response('{"detail":"Invalid input"}', { status: 422 }),
      ),
  );
  await expect(request("/api/items")).rejects.toThrow("Invalid input");
});

test("accepts successful empty and 204 responses", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(new Response("", { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  await expect(request("/api/items/1", { method: "DELETE" })).resolves.toBe(
    undefined,
  );
  await expect(request("/api/items/1", { method: "PUT" })).resolves.toBe(
    undefined,
  );
});
