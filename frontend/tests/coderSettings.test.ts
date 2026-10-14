import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  fetchCoderSettings,
  saveCoderSettings,
  resetCoderSettings,
  validateCoderPreferences,
} from "../src/api/coderSettingsClient.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";

const settings = {
  context_window: 16384,
  generation_limit: 4096,
  max_rounds: 30,
};
const response = {
  settings,
  defaults: settings,
  safety_margin: 256,
  model_context_overrides: {},
};

test("coder settings load/save/reset separately from the general agent", async () => {
  const calls: unknown[] = [];
  const controller = new AbortController();
  const mock = installFetch((url, init) => {
    calls.push([
      String(url),
      init?.method,
      init?.body ? JSON.parse(String(init.body)) : undefined,
    ]);
    if (init?.method === "GET") assert.equal(init.signal, controller.signal);
    return jsonResponse(response);
  });
  try {
    assert.deepEqual(await fetchCoderSettings(controller.signal), response);
    assert.deepEqual(await saveCoderSettings(settings), response);
    assert.deepEqual(await resetCoderSettings(), response);
    assert.deepEqual(calls, [
      ["/api/coder/settings", "GET", undefined],
      ["/api/coder/settings", "PUT", settings],
      ["/api/coder/settings", "DELETE", undefined],
    ]);
  } finally {
    mock.restore();
  }
});

test("coder bounds and output/context budget are enforced", () => {
  assert.equal(validateCoderPreferences(settings, response), null);
  for (const change of [
    { context_window: 0 },
    { generation_limit: NaN },
    { max_rounds: 97 },
    { max_rounds: 3 },
    { generation_limit: 4096.5 },
  ]) {
    assert.equal(
      validateCoderPreferences({ ...settings, ...change }, response),
      "range",
    );
  }
  assert.equal(
    validateCoderPreferences(
      { ...settings, generation_limit: 16384 },
      response,
    ),
    "budget",
  );
  assert.equal(
    validateCoderPreferences(settings, {
      ...response,
      model_context_overrides: { small: 4096 },
    }),
    "budget",
  );
});

test("coder API failures never report saved settings", async () => {
  const mock = installFetch(() => jsonResponse({}, 500));
  try {
    await assert.rejects(
      fetchCoderSettings(),
      /Coder settings request failed: 500/,
    );
    await assert.rejects(
      saveCoderSettings(settings),
      /Coder settings request failed: 500/,
    );
    await assert.rejects(
      resetCoderSettings(),
      /Coder settings request failed: 500/,
    );
  } finally {
    mock.restore();
  }
});

test("coder settings labels exist in all supported languages", () => {
  for (const language of ["en", "fr", "ar"]) {
    const locale = JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${language}.json`, import.meta.url),
        "utf8",
      ),
    );
    for (const key of ["title", "description", "rounds", "roundsHint"])
      assert.ok(locale[`settings.ai.coder.${key}`]?.trim());
  }
});
