import assert from "node:assert/strict";
import test from "node:test";

import { cn } from "../src/lib/utils.ts";

test("cn joins conditional class names and drops falsy inputs", () => {
  assert.equal(cn("flex", "items-center"), "flex items-center");
  assert.equal(cn("flex", false && "hidden", null, undefined, ""), "flex");
  assert.equal(
    cn(["p-2", "m-2"], { hidden: false, block: true }),
    "p-2 m-2 block",
  );
});

test("cn lets the later tailwind utility win a conflict", () => {
  // The whole point of twMerge: a component default ("px-2") must lose to
  // a caller override ("px-4") instead of producing an ambiguous class list.
  assert.equal(cn("px-2 text-sm", "px-4"), "text-sm px-4");
  assert.equal(cn("text-red-500", "text-blue-500"), "text-blue-500");
  assert.equal(cn("font-bold", "font-normal", "font-bold"), "font-bold");
});

test("cn returns an empty string without input", () => {
  assert.equal(cn(), "");
});
