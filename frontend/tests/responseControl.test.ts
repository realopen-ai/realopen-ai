import test from "node:test";
import assert from "node:assert/strict";

import {
  responseTransportForStop,
  shouldCaptureMicrophone,
} from "../src/voice/responseControl.ts";

test("microphone mute gates all capture", () => {
  assert.equal(shouldCaptureMicrophone(false), true);
  assert.equal(shouldCaptureMicrophone(true), false);
});

test("stop targets an active voice response", () => {
  assert.equal(responseTransportForStop("processing"), "voice");
  assert.equal(responseTransportForStop("speaking"), "voice");
  assert.equal(responseTransportForStop("interrupting"), "voice");
});

test("stop otherwise targets the text stream", () => {
  for (const state of [
    "inactive",
    "connecting",
    "listening",
    "stopping",
    "error",
  ] as const) {
    assert.equal(responseTransportForStop(state), "text");
  }
});
