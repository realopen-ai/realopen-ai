import assert from "node:assert/strict";
import test from "node:test";
import {
  activeModelJob,
  modelBytes,
  modelInstallName,
  modelJobProgress,
} from "../src/lib/modelManager.ts";
import { getBrainRoute, brainTabPath } from "../src/lib/appRoutes.ts";

test("Models is a native Brain destination", () => {
  assert.equal(brainTabPath("models"), "/brain/models");
  assert.deepEqual(getBrainRoute("/brain/models"), {
    tab: "models",
    toolId: null,
  });
});
test("model progress handles unknown sizes and bounds", () => {
  assert.equal(modelJobProgress(12, 0), null);
  assert.equal(modelJobProgress(150, 100), 100);
  assert.equal(modelJobProgress(-1, 100), 0);
  assert.equal(modelJobProgress(50, 100), 50);
  assert.equal(modelBytes(5_200_000_000), "5.2 GB");
  assert.equal(modelBytes(null), "—");
});
test("model names preserve Ollama tags and normalize HF variants", () => {
  assert.equal(modelInstallName("ollama", "qwen3", "8b"), "qwen3:8b");
  assert.equal(
    modelInstallName("huggingface", "publisher/Model", "model-Q4_K_M.gguf"),
    "publisher-model:model-Q4_K_M",
  );
});
test("completed and interrupted jobs are not running", () => {
  assert.equal(activeModelJob("running"), true);
  assert.equal(activeModelJob("queued"), true);
  assert.equal(activeModelJob("completed"), false);
  assert.equal(activeModelJob("interrupted"), false);
});
