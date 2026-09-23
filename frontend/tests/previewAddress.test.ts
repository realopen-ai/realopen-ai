import test from "node:test";
import assert from "node:assert/strict";

import {
  normalizePreviewAddress,
  previewHostUrl,
} from "../src/lib/previewAddress.ts";

test("preview ports display stable localhost addresses", () => {
  assert.equal(previewHostUrl(6969, "/internal"), "http://localhost:6969");
  assert.equal(previewHostUrl(6767, "/internal"), "http://localhost:6767");
});

test("preview address accepts local paths and rejects unrelated hosts", () => {
  assert.equal(
    normalizePreviewAddress("/health", "http://localhost:6969"),
    "http://localhost:6969/health",
  );
  assert.equal(
    normalizePreviewAddress("localhost:6969/notes", "http://localhost:6969"),
    "http://localhost:6969/notes",
  );
  assert.equal(
    normalizePreviewAddress("https://example.com", "http://localhost:6969"),
    null,
  );
});
