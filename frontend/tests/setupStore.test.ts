/**
 * Tests for src/store/setupStore.ts — the first-run setup wizard store.
 *
 * Direct import requires the alias hooks (`@/api/setupClient` — see
 * tests/helpers/viteCompat.ts); REST/SSE traffic is served by the
 * globalThis.fetch mock. The heart of the store is pullModels(), which
 * turns the /api/setup/pull-models SSE stream into per-artifact progress
 * rows — including the "never invent percentages" rule (hasPercent only
 * when the backend actually sent one) and the StrictMode double-mount
 * guard.
 */
import assert from "node:assert/strict";
import test from "node:test";

import "./helpers/viteCompat.ts";
import {
  installFetch,
  jsonResponse,
  sseData,
  sseResponse,
} from "./helpers/fetchMock.ts";

const { useSetupStore } = await import("../src/store/setupStore.ts");

const hardwareInfo = {
  platform: "linux",
  platform_display: "Linux",
  ram_gb: 16,
  cpu_cores: 8,
  cpu_name: "CPU",
  gpu_type: "none",
  gpu_vram_mb: 0,
  gpu_vram_gb: 0,
  gpu_name: "",
  recommended_profile: "cpu_medium",
  ollama_installed: true,
  ollama_running: true,
  docker_available: true,
};

const assistantModule = {
  name: "assistant",
  required: true,
  label: "Assistant",
  description: "",
  icon: "sparkles",
  estimated_size: "3 GB",
  availability: { cpu_small: true, cpu_medium: true },
  requirements_met: true,
  models: [],
  profile_models: {},
};

const visionModule = {
  ...assistantModule,
  name: "vision",
  required: false,
  availability: { cpu_small: false, cpu_medium: true },
};

function reset(overrides: Record<string, unknown> = {}) {
  useSetupStore.setState({
    setupComplete: false,
    isChecking: false,
    step: "welcome",
    hardwareInfo: null,
    isLoadingHardware: false,
    profiles: {},
    isLoadingProfiles: false,
    selectedProfile: "cpu_small",
    modules: [],
    isLoadingModules: false,
    enabledModules: ["assistant"],
    isApplying: false,
    isPulling: false,
    pullProgress: {
      currentModel: "",
      currentModule: "",
      currentIndex: 0,
      totalModels: 0,
      percent: 0,
      status: "",
      errors: [],
      completed: [],
      rows: [],
    },
    error: null,
    ...overrides,
  });
}

test("checkSetupStatus() reports completion and survives failures", async () => {
  reset();
  let complete = true;
  let fail = false;
  const mock = installFetch((url) => {
    assert.equal(url, "/api/setup/status");
    if (fail) throw new TypeError("fetch failed");
    return jsonResponse({ setup_complete: complete, profile: "cpu_small" });
  });
  try {
    assert.equal(await useSetupStore.getState().checkSetupStatus(), true);
    assert.equal(useSetupStore.getState().isChecking, false);

    complete = false;
    assert.equal(await useSetupStore.getState().checkSetupStatus(), false);
    assert.equal(useSetupStore.getState().setupComplete, false);

    fail = true;
    assert.equal(await useSetupStore.getState().checkSetupStatus(), false);
    assert.equal(useSetupStore.getState().isChecking, false);
  } finally {
    mock.restore();
  }
});

test("loadHardwareInfo() adopts the recommended profile", async () => {
  reset({ selectedProfile: "cpu_small" });
  const mock = installFetch((url) => {
    assert.equal(url, "/api/setup/hardware");
    return jsonResponse(hardwareInfo);
  });
  try {
    await useSetupStore.getState().loadHardwareInfo();
    assert.deepEqual(useSetupStore.getState().hardwareInfo, hardwareInfo);
    assert.equal(useSetupStore.getState().selectedProfile, "cpu_medium");
    assert.equal(useSetupStore.getState().isLoadingHardware, false);
  } finally {
    mock.restore();
  }
});

test("loadHardwareInfo() keeps the profile when the probe fails", async () => {
  reset({ selectedProfile: "cpu_small" });
  const mock = installFetch(() => jsonResponse({}, 500));
  try {
    await useSetupStore.getState().loadHardwareInfo();
    assert.equal(useSetupStore.getState().hardwareInfo, null);
    assert.equal(useSetupStore.getState().selectedProfile, "cpu_small");
    assert.equal(useSetupStore.getState().isLoadingHardware, false);
  } finally {
    mock.restore();
  }
});

test("loadProfiles() stores the profile catalog and survives failures", async () => {
  reset({
    profiles: {
      stale: { description: "", label: "Stale", engine: "", models: [] },
    },
  });
  let works = true;
  const catalog = {
    cpu_small: {
      description: "small",
      label: "Small",
      engine: "ollama",
      models: [],
    },
  };
  const mock = installFetch((url) => {
    assert.equal(url, "/api/setup/profiles");
    return works ? jsonResponse(catalog) : jsonResponse({}, 500);
  });
  try {
    await useSetupStore.getState().loadProfiles();
    assert.deepEqual(useSetupStore.getState().profiles, catalog);
    assert.equal(useSetupStore.getState().isLoadingProfiles, false);

    // A failing probe keeps the loaded catalog instead of wiping it.
    works = false;
    await useSetupStore.getState().loadProfiles();
    assert.deepEqual(useSetupStore.getState().profiles, catalog);
    assert.equal(useSetupStore.getState().isLoadingProfiles, false);
  } finally {
    mock.restore();
  }
});

test("loadModules() auto-enables exactly the required modules", async () => {
  reset({ enabledModules: ["assistant", "stale"] });
  const mock = installFetch((url) => {
    assert.equal(url, "/api/setup/modules");
    return jsonResponse({ modules: [assistantModule, visionModule] });
  });
  try {
    await useSetupStore.getState().loadModules();
    assert.deepEqual(useSetupStore.getState().enabledModules, ["assistant"]);
    assert.equal(useSetupStore.getState().isLoadingModules, false);
  } finally {
    mock.restore();
  }
});

test("toggleModule() refuses required and profile-unavailable modules", () => {
  reset({
    modules: [assistantModule, visionModule],
    selectedProfile: "cpu_small",
    enabledModules: ["assistant"],
  });
  const s = useSetupStore.getState();

  // Required modules cannot be turned off.
  s.toggleModule("assistant");
  // vision is not available on cpu_small.
  s.toggleModule("vision");
  // Unknown modules are ignored.
  s.toggleModule("ghost");
  assert.deepEqual(useSetupStore.getState().enabledModules, ["assistant"]);

  // On cpu_medium vision IS available and toggles on, then off.
  useSetupStore.setState({ selectedProfile: "cpu_medium" });
  useSetupStore.getState().toggleModule("vision");
  assert.deepEqual(useSetupStore.getState().enabledModules, [
    "assistant",
    "vision",
  ]);
  useSetupStore.getState().toggleModule("vision");
  assert.deepEqual(useSetupStore.getState().enabledModules, ["assistant"]);
});

test("applySetup() sends profile + modules and reports failures", async () => {
  reset({
    selectedProfile: "cpu_medium",
    enabledModules: ["assistant", "vision"],
  });
  let outcome: "ok" | "reject" | "throw" = "ok";
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/setup/apply");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      profile: "cpu_medium",
      enabled_modules: ["assistant", "vision"],
    });
    if (outcome === "throw") throw new TypeError("fetch failed");
    return outcome === "ok"
      ? jsonResponse({
          status: "ok",
          profile: "cpu_medium",
          enabled_modules: [],
          models_to_pull: [],
        })
      : jsonResponse({ detail: "nope" }, 500);
  });
  try {
    assert.equal(await useSetupStore.getState().applySetup(), true);
    assert.equal(useSetupStore.getState().isApplying, false);
    assert.equal(useSetupStore.getState().error, null);

    outcome = "reject";
    assert.equal(await useSetupStore.getState().applySetup(), false);
    assert.equal(
      useSetupStore.getState().error,
      "Failed to apply setup configuration",
    );

    outcome = "throw";
    // setupClient.applySetup catches transport errors and resolves null, so
    // the store's own catch (String(err)) is unreachable through the real
    // client — both failure shapes surface the same generic message.
    assert.equal(await useSetupStore.getState().applySetup(), false);
    assert.equal(
      useSetupStore.getState().error,
      "Failed to apply setup configuration",
    );
  } finally {
    mock.restore();
  }
});

test("pullModels() replays the SSE stream into progress rows without inventing percentages", async () => {
  reset({ selectedProfile: "cpu_medium", enabledModules: ["assistant"] });
  const events = [
    { event: "pull_start_all", total: 2 },
    {
      event: "pull_start",
      model: "qwen3:4b",
      module: "assistant",
      index: 0,
      provider: "ollama",
      kind: "ollama",
    },
    {
      event: "pull_progress",
      model: "qwen3:4b",
      percent: 42,
      status: "pulling qwen3",
    },
    {
      event: "pull_status",
      model: "pocket-tts",
      module: "voice",
      provider: "pip",
      kind: "voice_runtime",
      status: "Installing ffmpeg",
      output: "Reading package lists…",
    },
    { event: "pull_done", model: "qwen3:4b", already_installed: false },
    { event: "pull_error", model: "pocket-tts", error: "pip failed" },
    { event: "setup_error", error: "voice setup incomplete" },
    { event: "pull_all_done" },
  ];
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/setup/pull-models");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      profile: "cpu_medium",
      enabled_modules: ["assistant"],
    });
    return sseResponse(events.map((e) => sseData(e)));
  });
  try {
    const ok = await useSetupStore.getState().pullModels();
    assert.equal(ok, true);

    const p = useSetupStore.getState().pullProgress;
    assert.equal(useSetupStore.getState().isPulling, false);
    assert.equal(p.totalModels, 2);
    assert.equal(p.currentModel, "qwen3:4b");
    assert.equal(p.currentModule, "assistant");
    assert.equal(p.status, "done");
    assert.equal(p.percent, 100);
    assert.deepEqual(p.completed, ["qwen3:4b"]);
    assert.deepEqual(p.errors, [
      "pocket-tts: pip failed",
      "voice setup incomplete",
    ]);

    const qwen = p.rows.find((r) => r.model === "qwen3:4b")!;
    assert.equal(qwen.status, "done");
    assert.equal(qwen.percent, 100);
    assert.equal(qwen.hasPercent, true);
    assert.equal(qwen.alreadyInstalled, false);
    assert.equal(qwen.provider, "ollama");

    const pip = p.rows.find((r) => r.model === "pocket-tts")!;
    assert.equal(pip.status, "error");
    assert.equal(pip.error, "pip failed");
    assert.equal(pip.statusText, "Installing ffmpeg");
    assert.equal(pip.outputText, "Reading package lists…");
    // pull_status never fabricates a percentage — the row stays indeterminate.
    assert.equal(pip.hasPercent, false);
    assert.equal(pip.percent, undefined);
  } finally {
    mock.restore();
  }
});

test("pullModels() marks already-installed artifacts and skips malformed SSE lines", async () => {
  reset({ selectedProfile: "cpu_small", enabledModules: ["assistant"] });
  const body = [
    sseData({ event: "pull_start", model: "qwen3:4b" }),
    "data: {not json\n\n",
    sseData({ event: "pull_done", model: "qwen3:4b", already_installed: true }),
  ].join("");
  const mock = installFetch(() => sseResponse([body]));
  try {
    const ok = await useSetupStore.getState().pullModels();
    assert.equal(ok, true);
    const row = useSetupStore.getState().pullProgress.rows[0];
    assert.equal(row.status, "done");
    assert.equal(row.alreadyInstalled, true);
  } finally {
    mock.restore();
  }
});

test("pullModels() returns false when the stream cannot start", async () => {
  reset();
  const mock = installFetch(() => jsonResponse({}, 500));
  try {
    assert.equal(await useSetupStore.getState().pullModels(), false);
    assert.equal(useSetupStore.getState().isPulling, false);
  } finally {
    mock.restore();
  }
});

test("a second concurrent pullModels() reuses the in-flight stream (StrictMode)", async () => {
  reset();
  let finish!: () => void;
  const mock = installFetch(
    () =>
      new Promise<Response>((resolve) => {
        finish = () =>
          resolve(sseResponse([sseData({ event: "pull_all_done" })]));
      }),
  );
  try {
    const first = useSetupStore.getState().pullModels();
    // StrictMode mounts the effect twice: the remount must NOT open a
    // second SSE stream (its waiting events could clobber completed state).
    const second = useSetupStore.getState().pullModels();
    assert.equal(await second, true);
    assert.equal(useSetupStore.getState().isPulling, true);

    finish();
    assert.equal(await first, true);
    assert.equal(useSetupStore.getState().isPulling, false);
    assert.equal(mock.calls.length, 1);
    assert.equal(useSetupStore.getState().pullProgress.status, "done");
  } finally {
    mock.restore();
  }
});

test("completeSetup() flips the wizard to done only on success", async () => {
  reset({ step: "installing" });
  let ok = true;
  const mock = installFetch((url, init) => {
    assert.equal(url, "/api/setup/complete");
    assert.equal(init?.method, "POST");
    return jsonResponse({}, ok ? 200 : 500);
  });
  try {
    ok = false;
    await useSetupStore.getState().completeSetup();
    assert.equal(useSetupStore.getState().setupComplete, false);
    assert.equal(useSetupStore.getState().step, "installing");

    ok = true;
    await useSetupStore.getState().completeSetup();
    assert.equal(useSetupStore.getState().setupComplete, true);
    assert.equal(useSetupStore.getState().step, "done");
  } finally {
    mock.restore();
  }
});

test("setStep() clears the error and reset() rewinds the wizard", () => {
  reset({ step: "installing", error: "boom" });
  useSetupStore.getState().setStep("review");
  assert.equal(useSetupStore.getState().step, "review");
  assert.equal(useSetupStore.getState().error, null);

  useSetupStore.setState({ step: "done", error: "x" });
  useSetupStore.getState().reset();
  assert.equal(useSetupStore.getState().step, "welcome");
  assert.equal(useSetupStore.getState().error, null);
  assert.deepEqual(useSetupStore.getState().pullProgress.rows, []);
  assert.equal(useSetupStore.getState().pullProgress.percent, 0);
});
