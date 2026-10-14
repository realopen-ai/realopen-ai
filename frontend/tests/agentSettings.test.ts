import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  fetchAgentSettings,
  saveAgentSettings,
  resetAgentSettings,
  validateAgentPreferences,
  type AgentPreferences,
  type AgentSettingsResponse,
} from "../src/api/agentSettingsClient.ts";
import { installFetch, jsonResponse } from "./helpers/fetchMock.ts";

const preferences: AgentPreferences = {
  context_window: 8192,
  generation_limit: 2048,
  history_compaction: true,
  compaction_threshold: 0.8,
  recent_turns: 6,
  tool_calling: "auto",
  repair_attempts: 2,
};
const response: AgentSettingsResponse = {
  settings: preferences,
  defaults: preferences,
  model_context_overrides: {},
  safety_margin: 256,
};

test("advanced AI settings load, persist, and reset through the database API", async () => {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const controller = new AbortController();
  const mock = installFetch((url, init) => {
    calls.push({
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    if (init?.method === "GET") assert.equal(init.signal, controller.signal);
    if (init?.method === "PUT") {
      assert.equal(
        (init.headers as Record<string, string>)["Content-Type"],
        "application/json",
      );
    }
    return jsonResponse(response);
  });
  try {
    assert.deepEqual(await fetchAgentSettings(controller.signal), response);
    assert.deepEqual(await saveAgentSettings(preferences), response);
    assert.deepEqual(await resetAgentSettings(), response);
    assert.deepEqual(calls, [
      { url: "/api/agent/settings", method: "GET", body: undefined },
      { url: "/api/agent/settings", method: "PUT", body: preferences },
      { url: "/api/agent/settings", method: "DELETE", body: undefined },
    ]);
  } finally {
    mock.restore();
  }
});

test("failed settings requests never report successful persistence", async () => {
  const mock = installFetch(() => jsonResponse({ detail: "Unavailable" }, 500));
  try {
    for (const operation of [
      fetchAgentSettings,
      () => saveAgentSettings(preferences),
      resetAgentSettings,
    ]) {
      await assert.rejects(operation(), /Agent settings request failed: 500/);
    }
  } finally {
    mock.restore();
  }
});

test("settings validation checks numeric bounds and the model-specific context budget", () => {
  assert.equal(validateAgentPreferences(preferences, response), null);
  for (const change of [
    { context_window: 1024 },
    { context_window: 262145 },
    { generation_limit: 0 },
    { generation_limit: 65537 },
    { compaction_threshold: 0.49 },
    { compaction_threshold: NaN },
    { recent_turns: 1.5 },
    { recent_turns: 33 },
    { repair_attempts: -1 },
    { repair_attempts: 6 },
    { tool_calling: "native" },
    { history_compaction: "yes" },
  ]) {
    assert.equal(
      validateAgentPreferences(
        { ...preferences, ...change } as AgentPreferences,
        response,
      ),
      "range",
    );
  }
  assert.equal(
    validateAgentPreferences(
      { ...preferences, context_window: 2048 },
      response,
    ),
    "budget",
  );
  const overridden = {
    ...response,
    model_context_overrides: { "small-model": 4096 },
  };
  assert.equal(
    validateAgentPreferences(
      { ...preferences, generation_limit: 3840 },
      overridden,
    ),
    "budget",
  );
  assert.equal(
    validateAgentPreferences(
      { ...preferences, generation_limit: 3839 },
      overridden,
    ),
    null,
  );
});

test("advanced AI settings have matching English, French, and Arabic translations", () => {
  const locales = ["en", "fr", "ar"].map((language) =>
    JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${language}.json`, import.meta.url),
        "utf8",
      ),
    ),
  );
  const keys = Object.keys(locales[0]).filter((key) =>
    key.startsWith("settings.ai.advanced."),
  );
  assert.ok(keys.length >= 20);
  for (const locale of locales) {
    assert.deepEqual(
      Object.keys(locale).filter((key) =>
        key.startsWith("settings.ai.advanced."),
      ),
      keys,
    );
    for (const key of keys)
      assert.ok(typeof locale[key] === "string" && locale[key].trim());
  }
});
