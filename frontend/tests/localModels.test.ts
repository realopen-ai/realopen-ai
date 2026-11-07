import assert from "node:assert/strict";
import test from "node:test";
import { localModelFiles } from "../src/lib/localModels.ts";

const options = {
  extensions: [".gguf", ".safetensors"],
  companion_files: ["config.json", "tokenizer_config.json"],
  chunk_size: 4_194_304,
  max_bytes: 100,
};
test("folder picks preserve shards but exclude scripts and supplied Modelfiles", () => {
  const picked = localModelFiles(
    [
      {
        name: "model.gguf",
        size: 10,
        webkitRelativePath: "MyModel/model.gguf",
      },
      {
        name: "config.json",
        size: 2,
        webkitRelativePath: "MyModel/config.json",
      },
      {
        name: "modeling.py",
        size: 20,
        webkitRelativePath: "MyModel/modeling.py",
      },
      { name: "Modelfile", size: 10, webkitRelativePath: "MyModel/Modelfile" },
    ],
    options,
  );
  assert.deepEqual(
    picked.files.map((file) => file.path),
    ["config.json", "model.gguf"],
  );
  assert.equal(picked.ignored, 2);
});
test("multiple individual file selections retain exact filenames", () => {
  const picked = localModelFiles(
    [
      { name: "Q4.gguf", size: 20 },
      { name: "other.gguf", size: 20 },
    ],
    options,
  );
  assert.deepEqual(
    picked.files.map((file) => file.path),
    ["Q4.gguf", "other.gguf"],
  );
});
test("unsafe paths, case collisions and oversized selections are rejected", () => {
  assert.throws(
    () => localModelFiles([{ name: "../secret.gguf", size: 1 }], options),
    /unsafeFiles/,
  );
  assert.throws(
    () =>
      localModelFiles(
        [
          { name: "Model.gguf", size: 1 },
          { name: "model.gguf", size: 1 },
        ],
        options,
      ),
    /duplicateFiles/,
  );
  assert.throws(
    () => localModelFiles([{ name: "model.gguf", size: 101 }], options),
    /tooLarge/,
  );
  assert.throws(
    () => localModelFiles([{ name: "config.json", size: 2 }], options),
    /noWeights/,
  );
});

test("chunk uploads back off on proxy rate limits without changing the payload", async (t) => {
  const { modelManager } = await import("../src/api/modelManagerClient.ts");
  let count = 0;
  const payload = new Blob(["GGUF test"]);
  t.mock.method(
    globalThis,
    "fetch",
    async (_url: unknown, init?: RequestInit) => {
      assert.equal(init?.body, payload);
      count++;
      return count === 1
        ? new Response("rate limited", {
            status: 429,
            headers: { "Retry-After": "0" },
          })
        : new Response(JSON.stringify({ offset: payload.size }), {
            status: 200,
          });
    },
  );
  const result = await modelManager.uploadLocalChunk(
    "source",
    "model.gguf",
    0,
    payload,
  );
  assert.equal(count, 2);
  assert.equal(result.offset, payload.size);
});

test("cancel stops chunk retry backoff", async (t) => {
  const { modelManager } = await import("../src/api/modelManagerClient.ts");
  const abort = new AbortController();
  let count = 0;
  t.mock.method(globalThis, "fetch", async () => {
    count++;
    setTimeout(() => abort.abort(), 10);
    return new Response("rate limited", { status: 429 });
  });
  await assert.rejects(
    modelManager.uploadLocalChunk(
      "source",
      "model.gguf",
      0,
      new Blob(["test"]),
      abort.signal,
    ),
    (error: unknown) =>
      error instanceof DOMException && error.name === "AbortError",
  );
  assert.equal(count, 1);
});
