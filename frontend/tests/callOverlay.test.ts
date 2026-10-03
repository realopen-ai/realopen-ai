import test from "node:test";
import assert from "node:assert/strict";

import { nearestCallOverlayCorner } from "../src/voice/callOverlay.ts";

const bounds = { left: 100, top: 50, width: 800, height: 600 };

test("voice overlay snaps to the nearest quadrant", () => {
  assert.equal(
    nearestCallOverlayCorner({ x: 150, y: 100 }, bounds),
    "top-left",
  );
  assert.equal(
    nearestCallOverlayCorner({ x: 850, y: 100 }, bounds),
    "top-right",
  );
  assert.equal(
    nearestCallOverlayCorner({ x: 150, y: 600 }, bounds),
    "bottom-left",
  );
  assert.equal(
    nearestCallOverlayCorner({ x: 850, y: 600 }, bounds),
    "bottom-right",
  );
});
