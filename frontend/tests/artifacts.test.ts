import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  artifactsApi,
  artifactSourceUrl,
  artifactSelectionSearch,
} from "../src/api/artifactsClient.ts";

test("artifact selections persist version and section independently in the URL", () => {
  const first = artifactSelectionSearch(
    "?version=2&section=slide-4&other=keep",
    { version: 1 },
  );
  const second = artifactSelectionSearch(first, {
    section: "sheet with spaces",
  });
  const query = new URLSearchParams(second);
  assert.equal(query.get("version"), "1");
  assert.equal(query.get("section"), "sheet with spaces");
  assert.equal(query.get("other"), "keep");
});

test("artifact client preserves exact versions, targeted edits and grounded links", async () => {
  const fetch = globalThis.fetch;
  const requests: { url: string; body: unknown }[] = [];
  globalThis.fetch = async (url, options) => {
    requests.push({
      url: String(url),
      body: options?.body ? JSON.parse(String(options.body)) : undefined,
    });
    return new Response("{}", { status: 200 });
  };
  try {
    await artifactsApi.detail("id", 2);
    await artifactsApi.read("id", 2, "slide-4", 12000);
    await artifactsApi.update("id", 2, "slide-4", "# Edited slide");
    await artifactsApi.restore("id", 3, 1);
    await artifactsApi.export("id", 1, "pdf");
    assert.ok(requests[0].url.endsWith("/id?version=2"));
    assert.ok(requests[1].url.includes("section_id=slide-4"));
    assert.deepEqual(requests[2].body, {
      expected_version: 2,
      changes: [{ section_id: "slide-4", content: "# Edited slide" }],
    });
    assert.deepEqual(requests[3].body, { expected_version: 3, version: 1 });
    assert.ok(requests[4].url.endsWith("/id/export/pdf?version=1"));
    assert.equal(
      artifactSourceUrl({
        artifact_id: "id",
        version: 2,
        section_id: "slide-4",
      }),
      "/workspace/artifacts/id?version=2&section=slide-4",
    );
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: "Version conflict" }), {
        status: 409,
      });
    await assert.rejects(artifactsApi.detail("id"), /Version conflict/);
  } finally {
    globalThis.fetch = fetch;
  }
});

test("artifact workspace routes and controls are localized in all languages", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.ok(app.includes('path="/workspace/artifacts/:artifactId"'));
  const locales = ["en", "fr", "ar"].map((language) =>
    JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${language}.json`, import.meta.url),
        "utf8",
      ),
    ),
  );
  for (const locale of locales)
    assert.deepEqual(
      Object.keys(locale)
        .filter((key) => key.startsWith("artifacts."))
        .sort(),
      Object.keys(locales[0])
        .filter((key) => key.startsWith("artifacts."))
        .sort(),
    );
});
