import { afterEach, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { createContext } from "../extensions";
import { install } from "./session";

afterEach(() => {
  vi.unstubAllGlobals();
});
const result = {
  user: { id: 1, username: "alice" },
  csrf_token: "session-bound-token",
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

it("restores cookie sessions without storing credentials in browser storage", async () => {
  const fetch = vi.fn(async (_path: string, _init?: RequestInit) =>
    Response.json(result),
  );
  vi.stubGlobal("fetch", fetch);
  const context = createContext();
  const close = install(context);
  await settle();
  expect(context.session!.getSnapshot().user).toEqual(result.user);
  expect(fetch.mock.calls[0]![0]).toBe("/api/auth/session");
  expect(localStorage.length).toBe(0);
  close();
  expect(context.session).toBeUndefined();
});

it("adds session headers to the shared transport and removes expired UI state", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json(result)),
  );
  const request = vi.fn().mockResolvedValue({ id: 1 });
  const context = createContext(request);
  const close = install(context);
  await settle();
  const headers = new Headers({ "Content-Type": "application/json" });
  await context.request("/api/example", {
    method: "POST",
    headers,
    body: "{}",
  });
  const sent = request.mock.calls[0]![1];
  expect(sent.credentials).toBe("same-origin");
  expect(sent.headers.get("X-CSRF-Token")).toBe(result.csrf_token);
  expect(sent.headers.get("Content-Type")).toBe("application/json");
  expect(headers.has("X-CSRF-Token")).toBe(false);
  const outside = { method: "GET" };
  await context.request("https://external.example/data", outside);
  expect(request.mock.calls[1]).toEqual([
    "https://external.example/data",
    outside,
  ]);
  request.mockRejectedValueOnce(new ApiError("Validation failed", 422));
  await expect(context.request("/api/example")).rejects.toThrow(
    "Validation failed",
  );
  expect(context.session!.getSnapshot().status).toBe("authenticated");
  request.mockRejectedValueOnce(new ApiError("Expired", 401));
  await expect(context.request("/api/example")).rejects.toThrow("Expired");
  expect(context.session!.getSnapshot().status).toBe("anonymous");
  close();
});

it("signs in, signs out, and does not silently retry writes", async () => {
  const fetch = vi.fn(async (path: string) =>
    path.endsWith("session")
      ? new Response(null, { status: 401 })
      : path.endsWith("logout")
        ? new Response(null, { status: 204 })
        : Response.json(result),
  );
  vi.stubGlobal("fetch", fetch);
  const context = createContext();
  const close = install(context);
  await settle();
  expect(context.session!.getSnapshot().status).toBe("anonymous");
  await context.session!.authenticate(
    "login",
    "alice",
    "example-long-password",
  );
  expect(context.session!.getSnapshot().status).toBe("authenticated");
  await context.session!.logout();
  expect(context.session!.getSnapshot().user).toBeNull();
  expect(fetch).toHaveBeenCalledTimes(3);
  close();
});

it("does not interpret network errors as an anonymous session and can retry", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new Error("Offline"))
    .mockResolvedValueOnce(Response.json(result));
  vi.stubGlobal("fetch", fetch);
  const context = createContext();
  const close = install(context);
  await settle();
  expect(context.session!.getSnapshot().status).toBe("error");
  await context.session!.refresh();
  expect(context.session!.getSnapshot().status).toBe("authenticated");
  close();
});

it("rejects malformed session responses rather than exposing protected UI", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ user: null })),
  );
  const context = createContext();
  const close = install(context);
  await settle();
  expect(context.session!.getSnapshot().status).toBe("error");
  close();
});

it("ignores an old unauthorized request after a new account session is accepted", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json(result)),
  );
  let reject!: (error: Error) => void;
  const previous = vi.fn(
    () =>
      new Promise<never>((_resolve, fail) => {
        reject = fail;
      }),
  );
  const context = createContext(previous);
  const close = install(context);
  await settle();
  const pending = context.request("/api/example");
  const failed = expect(pending).rejects.toThrow("Old session");
  await context.session!.authenticate(
    "login",
    "alice",
    "example-long-password",
  );
  reject(new ApiError("Old session", 401));
  await failed;
  expect(context.session!.getSnapshot().status).toBe("authenticated");
  close();
});

it("ignores a late bootstrap response after a new login or disposal", async () => {
  let resolve!: (response: Response) => void;
  const fetch = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    )
    .mockResolvedValue(Response.json(result));
  vi.stubGlobal("fetch", fetch);
  const context = createContext();
  const original = context.request;
  const close = install(context);
  await context.session!.authenticate(
    "register",
    "alice",
    "example-long-password",
  );
  resolve(new Response(null, { status: 401 }));
  await settle();
  expect(context.session!.getSnapshot().status).toBe("authenticated");
  close();
  expect(context.request).toBe(original);
});
