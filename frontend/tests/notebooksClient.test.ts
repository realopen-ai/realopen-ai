import assert from "node:assert/strict";
import test from "node:test";
import {
  notebooksApi,
  notebookSourceCount,
  type NotebookItem,
} from "../src/api/notebooksClient.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";

test("notebook source count excludes studio items and unchecked sources", () => {
  const items = [
    { kind: "document", selected: true },
    { kind: "artifact", selected: true },
    { kind: "document", selected: false },
    { kind: "note", selected: true },
    { kind: "deck", selected: true },
  ] as NotebookItem[];
  assert.equal(notebookSourceCount(items), 2);
  assert.equal(notebookSourceCount([]), 0);
});

test("notebook mutations send only application-managed links and selection", async () => {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const mock = installFetch((url, init) => {
    calls.push({
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return init?.method === "DELETE"
      ? new Response(null, { status: 204 })
      : jsonResponse({ id: "n", items: [] });
  });
  try {
    await notebooksApi.create("Physics");
    await notebooksApi.update("n", "Biology", "Cells");
    await notebooksApi.attach("n", "document", "d");
    await notebooksApi.select("n", "item", false);
    await notebooksApi.unlink("n", "item");
    await notebooksApi.delete("n");
    assert.deepEqual(calls, [
      {
        url: "/api/learn/notebooks",
        method: "POST",
        body: { title: "Physics" },
      },
      {
        url: "/api/learn/notebooks/n",
        method: "PUT",
        body: { title: "Biology", description: "Cells" },
      },
      {
        url: "/api/learn/notebooks/n/items",
        method: "POST",
        body: { kind: "document", target_id: "d" },
      },
      {
        url: "/api/learn/notebooks/n/items/item",
        method: "PATCH",
        body: { selected: false },
      },
      {
        url: "/api/learn/notebooks/n/items/item",
        method: "DELETE",
        body: undefined,
      },
      { url: "/api/learn/notebooks/n", method: "DELETE", body: undefined },
    ]);
  } finally {
    mock.restore();
  }
});

test("notebook loading supports cancellation and surfaces server errors", async () => {
  const controller = new AbortController();
  let fail = false;
  const mock = installFetch((url, init) => {
    assert.equal(init?.signal, controller.signal);
    return fail
      ? jsonResponse({ detail: "Notebook not found" }, 404)
      : jsonResponse([]);
  });
  try {
    assert.deepEqual(await notebooksApi.list(controller.signal), []);
    fail = true;
    await assert.rejects(
      notebooksApi.get("missing", controller.signal),
      /Notebook not found/,
    );
  } finally {
    mock.restore();
  }
});
