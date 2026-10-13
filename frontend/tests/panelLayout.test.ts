import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { rightPanelVisibilityAfterResize } from "../src/lib/panelLayout.ts";

test("initial panel measurements do not override persisted visibility", () => {
  assert.equal(rightPanelVisibilityAfterResize(40, undefined, false), null);
  assert.equal(rightPanelVisibilityAfterResize(0, undefined, false), null);
});

test("collapse and expand synchronize visibility without reopening notebook panes", () => {
  assert.equal(rightPanelVisibilityAfterResize(0, 40, false), false);
  assert.equal(rightPanelVisibilityAfterResize(40, 0, false), true);
  assert.equal(rightPanelVisibilityAfterResize(40, 0, true), null);
  assert.equal(rightPanelVisibilityAfterResize(0, 40, true), false);
  assert.equal(rightPanelVisibilityAfterResize(35, 40, false), null);
});

test("all nonzero panel size constraints use explicit percentages in v4", () => {
  for (const filename of ["AppLayout", "RightPanel"]) {
    const source = readFileSync(
      new URL(`../src/components/layout/${filename}.tsx`, import.meta.url),
      "utf8",
    );
    let sizes = 0;
    for (const panel of source.matchAll(/<Panel\b[^>]*>/g)) {
      for (const size of panel[0].matchAll(
        /\b(defaultSize|minSize|maxSize)=([^\s>]+)/g,
      )) {
        assert.match(
          size[2],
          /^"\d+%"$/,
          `${filename}: ${size[1]} must use percentages, not pixels`,
        );
        sizes++;
      }
    }
    assert.ok(sizes > 0);
  }
});

test("pane transitions target v4 panel roots and are disabled while dragging", () => {
  const css = readFileSync(
    new URL("../src/index.css", import.meta.url),
    "utf8",
  );
  const layout = readFileSync(
    new URL("../src/components/layout/AppLayout.tsx", import.meta.url),
    "utf8",
  );
  assert.match(layout, /data-animate-panels=\{!isResizing\}/);
  assert.match(
    css,
    /\[data-group\]\[data-animate-panels="true"\] > \[data-panel\]\s*\{\s*transition: flex-grow 100ms ease-in-out;/,
  );
  assert.match(
    css,
    /\[data-group\]:has\(> \[data-separator="active"\]\) > \[data-panel\]\s*\{\s*transition: none;/,
  );
  assert.match(css, /prefers-reduced-motion: reduce/);
});
