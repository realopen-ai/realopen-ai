import { StrictMode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { App } from "./App";
import { config } from "./generated";
import { createCrudContext, label } from "./data";
import { createContext, installPlugins, type AppContext } from "./extensions";
import type { AppConfig, Entity, Row } from "./types";

const close: Array<() => void> = [];
afterEach(() => {
  cleanup();
  close.splice(0).forEach((dispose) => dispose());
  localStorage.clear();
  vi.unstubAllGlobals();
});
const empty = () =>
  new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }));
const ready = () =>
  waitFor(() =>
    expect(screen.getByRole("status").textContent).not.toContain("Loading…"),
  );
function start(value: AppConfig, context = createCrudContext(value)) {
  return render(
    <StrictMode>
      <App config={value} context={context} />
    </StrictMode>,
  );
}
function fill(entity: Entity, value = "Example value") {
  const form = screen.getByRole("form", { name: "Create record" });
  const values: Record<string, unknown> = {};
  for (const field of entity.fields) {
    const input = within(form).getByLabelText(label(field));
    if (field.type === "boolean") {
      if (field.required) {
        if (!(input as HTMLInputElement).checked) fireEvent.click(input);
      } else fireEvent.change(input, { target: { value: "true" } });
      values[field.name] = true;
    } else {
      fireEvent.change(input, {
        target: { value: field.type === "string" ? value : "7" },
      });
      values[field.name] = field.type === "string" ? value : 7;
    }
  }
  return values;
}

it.each(config.entities)(
  "creates, edits, toggles and deletes $name through React components",
  async (entity) => {
    let rows: Row[] = [];
    const fetch = vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const row = { id: 1, ...JSON.parse(String(init.body)) };
        rows = [row];
        return new Response(JSON.stringify(row), { status: 201 });
      }
      if (init?.method === "PATCH") {
        rows[0] = { ...rows[0]!, ...JSON.parse(String(init.body)) };
        return new Response(JSON.stringify(rows[0]));
      }
      if (init?.method === "DELETE") {
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
    vi.stubGlobal("fetch", fetch);
    start({ ...config, local: false, entities: [entity] });
    await ready();
    const values = fill(entity);
    fireEvent.click(screen.getByRole("button", { name: "Add record" }));
    await screen.findByRole("article");
    await ready();
    expect(fetch).toHaveBeenCalledWith(
      `/api/${entity.name}`,
      expect.objectContaining({ method: "POST", body: JSON.stringify(values) }),
    );
    const boolean = entity.fields.find((field) => field.type === "boolean");
    if (boolean) {
      fireEvent.click(
        screen.getByRole("checkbox", {
          name: `${label(boolean)} for record 1`,
        }),
      );
      await waitFor(() =>
        expect(fetch).toHaveBeenCalledWith(
          `/api/${entity.name}/1`,
          expect.objectContaining({
            method: "PATCH",
            body: JSON.stringify({ [boolean.name]: false }),
          }),
        ),
      );
      await ready();
      expect(
        (
          screen.getByRole("checkbox", {
            name: `${label(boolean)} for record 1`,
          }) as HTMLInputElement
        ).checked,
      ).toBe(false);
    }
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const edited = entity.fields.find((field) => field.type !== "boolean");
    if (edited) {
      const form = screen.getByRole("form", { name: "Edit record" });
      fireEvent.change(within(form).getByLabelText(label(edited)), {
        target: { value: edited.type === "string" ? "Updated value" : "0" },
      });
    }
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(screen.getByRole("form", { name: "Create record" })).toBeTruthy(),
    );
    await ready();
    if (edited)
      expect(rows[0]![edited.name]).toBe(
        edited.type === "string" ? "Updated value" : 0,
      );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByRole("article")).toBeNull());
    await ready();
    expect(fetch).toHaveBeenCalledWith(
      `/api/${entity.name}/1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  },
);

it("recovers from load failures and clears pending state", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Offline")));
  start({ ...config, local: false });
  await screen.findByRole("alert");
  await ready();
  expect(screen.getByRole("alert").textContent).toContain("Offline");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => empty()),
  );
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  await ready();
});

it("keeps local records across remount and escapes user content", async () => {
  const entity = config.entities[0]!;
  const value = { ...config, local: true, entities: [entity] };
  const view = start(value);
  await ready();
  fill(entity, '<img src=x onerror="alert(1)">');
  fireEvent.click(screen.getByRole("button", { name: "Add record" }));
  const article = await screen.findByRole("article");
  await ready();
  expect(article.querySelector("img")).toBeNull();
  view.unmount();
  start(value);
  await screen.findByRole("article");
  await ready();
});

it("composes transport wrappers and invalidation without retaining stale requests", async () => {
  const pending: Array<(response: Response) => void> = [];
  const fetch = vi.fn(
    () => new Promise<Response>((resolve) => pending.push(resolve)),
  );
  vi.stubGlobal("fetch", fetch);
  const context = createContext();
  let invalidate!: () => Promise<void>;
  const dispose = installPlugins(context, [
    (shared) => {
      const next = shared.request;
      shared.request = (path, init) =>
        next(path, { ...init, headers: { "X-Test-Access": "allowed" } });
    },
    (shared) => {
      invalidate = () => shared.invalidate();
    },
  ]);
  close.push(dispose);
  start({ ...config, local: false }, context);
  await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThan(0));
  const count = fetch.mock.calls.length;
  await act(async () => {
    await invalidate();
  });
  await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThan(count));
  // Latest request wins even if older requests resolve afterward.
  await act(async () => {
    pending[pending.length - 1]!(empty());
  });
  await ready();
  await act(async () => {
    for (const resolve of pending.slice(0, -1))
      resolve(
        new Response(
          JSON.stringify({
            items: [{ id: 99 }],
            total: 1,
            limit: 25,
            offset: 0,
          }),
        ),
      );
  });
  await waitFor(() => expect(screen.queryByRole("article")).toBeNull());
});
