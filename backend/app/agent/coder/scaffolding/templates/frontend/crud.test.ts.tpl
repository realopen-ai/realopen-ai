import { afterEach, expect, it, vi } from "vitest";
import { mountCrud, type Entity } from "./crud";
import { config } from "./generated";
import { createContext, installPlugins } from "./extensions";

const disposers: Array<() => void> = [];
afterEach(() => {
  disposers.splice(0).forEach((dispose) => dispose());
  document.body.replaceChildren();
  localStorage.clear();
  vi.unstubAllGlobals();
});
const submit = (form: Element) =>
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

function start(entity: Entity, local = false) {
  const root = document.createElement("main");
  document.body.append(root);
  disposers.push(mountCrud(root, { ...config, local, entities: [entity] }));
  return root;
}

it.each(config.entities)(
  "creates, edits and deletes $name through the real form",
  async (entity) => {
    let rows: Array<Record<string, unknown>> = [];
    const requests = vi.fn(async (path: string, init?: RequestInit) => {
      const method = init?.method || "GET";
      if (method === "POST") {
        const row = { id: 1, ...JSON.parse(String(init?.body)) };
        rows = [row];
        return new Response(JSON.stringify(row), { status: 201 });
      }
      if (method === "PATCH") {
        rows[0] = { ...rows[0], ...JSON.parse(String(init?.body)) };
        return new Response(JSON.stringify(rows[0]));
      }
      if (method === "DELETE") {
        rows = [];
        return new Response(null, { status: 204 });
      }
      return new Response(
        JSON.stringify({
          items: rows,
          total: rows.length,
          limit: 25,
          offset: 0,
        }),
      );
    });
    vi.stubGlobal("fetch", requests);
    const root = start(entity);
    await vi.waitFor(() =>
      expect(root.getAttribute("aria-busy")).toBe("false"),
    );
    const form = root.querySelector<HTMLFormElement>(
      'form[aria-label="Create record"]',
    )!;
    const values: Record<string, unknown> = {};
    for (const field of entity.fields) {
      const input = form.elements.namedItem(field.name) as HTMLInputElement;
      if (field.type === "boolean") {
        input.checked = true;
        input.indeterminate = false;
        values[field.name] = true;
      } else {
        input.value = field.type === "string" ? "Example value" : "7";
        values[field.name] = field.type === "string" ? "Example value" : 7;
      }
    }
    submit(form);
    await vi.waitFor(() =>
      expect(root.querySelectorAll("article")).toHaveLength(1),
    );
    expect(requests).toHaveBeenCalledWith(
      `/api/${entity.name}`,
      expect.objectContaining({ method: "POST", body: JSON.stringify(values) }),
    );
    expect(root.textContent).not.toContain("Loading…");
    const boolean = entity.fields.find((field) => field.type === "boolean");
    if (boolean) {
      const toggle = root.querySelector<HTMLInputElement>(
        "article input[type=checkbox]",
      )!;
      toggle.checked = false;
      toggle.dispatchEvent(new Event("change"));
      await vi.waitFor(() =>
        expect(requests).toHaveBeenCalledWith(
          `/api/${entity.name}/1`,
          expect.objectContaining({
            method: "PATCH",
            body: JSON.stringify({ [boolean.name]: false }),
          }),
        ),
      );
      await vi.waitFor(() =>
        expect(root.getAttribute("aria-busy")).toBe("false"),
      );
      expect(
        root.querySelector<HTMLInputElement>("article input[type=checkbox]")!
          .checked,
      ).toBe(false);
    }
    Array.from(root.querySelectorAll<HTMLButtonElement>("article button"))
      .find((b) => b.textContent === "Edit")!
      .click();
    submit(root.querySelector('form[aria-label="Edit record"]')!);
    await vi.waitFor(() =>
      expect(
        root.querySelector('form[aria-label="Create record"]'),
      ).not.toBeNull(),
    );
    Array.from(root.querySelectorAll<HTMLButtonElement>("article button"))
      .find((b) => b.textContent === "Delete")!
      .click();
    await vi.waitFor(() =>
      expect(root.querySelectorAll("article")).toHaveLength(0),
    );
    await vi.waitFor(() =>
      expect(root.getAttribute("aria-busy")).toBe("false"),
    );
    expect(requests).toHaveBeenCalledWith(
      `/api/${entity.name}/1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  },
);

it("clears pending state on errors and can retry", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new Error("Offline"))
    .mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }),
      ),
    );
  vi.stubGlobal("fetch", fetch);
  const root = start(config.entities[0]!);
  await vi.waitFor(() =>
    expect(root.querySelector('[role="status"]')?.textContent).toBe("Offline"),
  );
  expect(root.getAttribute("aria-busy")).toBe("false");
  submit(root.querySelector("form.toolbar")!);
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  await vi.waitFor(() => expect(root.getAttribute("aria-busy")).toBe("false"));
});

it("uses safe text nodes for stored content and persists frontend-only records across remount", async () => {
  const entity = config.entities[0]!;
  let root = start(entity, true);
  await vi.waitFor(() => expect(root.getAttribute("aria-busy")).toBe("false"));
  const form = root.querySelector<HTMLFormElement>(
    'form[aria-label="Create record"]',
  )!;
  for (const field of entity.fields) {
    const input = form.elements.namedItem(field.name) as HTMLInputElement;
    if (field.type === "boolean") {
      input.checked = true;
      input.indeterminate = false;
    } else
      input.value =
        field.type === "string" ? '<img src=x onerror="alert(1)">' : "1";
  }
  submit(form);
  await vi.waitFor(() =>
    expect(root.querySelectorAll("article")).toHaveLength(1),
  );
  expect(root.querySelector("article img")).toBeNull();
  disposers.pop()!();
  root = start(entity, true);
  await vi.waitFor(() =>
    expect(root.querySelectorAll("article")).toHaveLength(1),
  );
});

it("composes transport and invalidation plugins and closes their resources", async () => {
  const root = document.createElement("main"),
    context = createContext();
  const close = vi.fn(),
    requested = vi.fn(async () => ({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
    }));
  const cleanup = installPlugins(context, [
    (shared) => {
      shared.request = requested as typeof shared.request;
      return close;
    },
  ]);
  disposers.push(
    cleanup,
    mountCrud(root, { ...config, local: false }, context),
  );
  await vi.waitFor(() => expect(root.getAttribute("aria-busy")).toBe("false"));
  await context.invalidate();
  expect(requested).toHaveBeenCalledTimes(2);
  cleanup();
  expect(close).toHaveBeenCalledOnce();
});

it("keeps transport wrappers and queues event invalidation during an active request", async () => {
  let resolve!: (response: Response) => void;
  const empty = () =>
    new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }));
  const fetch = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    )
    .mockImplementation(async () => empty());
  vi.stubGlobal("fetch", fetch);
  const context = createContext();
  let notify!: () => Promise<void>;
  const cleanup = installPlugins(context, [
    (shared) => {
      const next = shared.request;
      shared.request = (path, init) =>
        next(path, {
          ...init,
          headers: { ...init?.headers, "X-Test-Access": "allowed" },
        });
    },
    (shared) => {
      notify = () => shared.invalidate();
    },
  ]);
  const root = document.createElement("main");
  disposers.push(
    cleanup,
    mountCrud(root, { ...config, local: false }, context),
  );
  await notify(); // The initial GET is still pending: this must not be discarded.
  resolve(empty());
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  await vi.waitFor(() => expect(root.getAttribute("aria-busy")).toBe("false"));
  expect(
    fetch.mock.calls.every(
      (call) => call[1].headers["X-Test-Access"] === "allowed",
    ),
  ).toBe(true);
});

it("cleans up installed plugins if a later plugin cannot initialize", () => {
  const close = vi.fn();
  expect(() =>
    installPlugins(createContext(), [
      () => close,
      () => {
        throw new Error("Setup failed");
      },
    ]),
  ).toThrow("Setup failed");
  expect(close).toHaveBeenCalledOnce();
});
