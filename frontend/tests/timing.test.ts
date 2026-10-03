import assert from "node:assert/strict";
import test from "node:test";

import {
  elapsedMilliseconds,
  epochMilliseconds,
} from "../src/lib/timing.ts";

test("normalizes legacy epoch seconds to epoch milliseconds", () => {
  assert.equal(epochMilliseconds(1_700_000_000), 1_700_000_000_000);
  assert.equal(epochMilliseconds(1_700_000_000_123), 1_700_000_000_123);
});

test("tool elapsed time accepts either timestamp unit", () => {
  assert.equal(elapsedMilliseconds(1_700_000_000, 1_700_000_001.25), 1250);
  assert.equal(
    elapsedMilliseconds(1_700_000_000_000, 1_700_000_001_250),
    1250,
  );
});

test("invalid and reversed timestamps never render negative durations", () => {
  assert.equal(elapsedMilliseconds(2000, 1000), null);
  assert.equal(elapsedMilliseconds(Number.NaN, 1000), null);
});
