import test from "node:test";
import assert from "node:assert/strict";

import {
  personaAfterDelete,
  removeCustomPersona,
  replaceCustomPersona,
} from "../src/voice/personaControls.ts";

const personas = [
  { id: "custom-a", name: "A", prompt: "Prompt A" },
  { id: "custom-b", name: "B", prompt: "Prompt B" },
];

test("editing preserves persona identity and list position", () => {
  const edited = replaceCustomPersona(personas, {
    id: "custom-a",
    name: "Updated",
    prompt: "Updated prompt",
  });
  assert.deepEqual(edited[0], {
    id: "custom-a",
    name: "Updated",
    prompt: "Updated prompt",
  });
  assert.equal(edited[1], personas[1]);
});

test("deleting removes only the selected custom persona", () => {
  assert.deepEqual(removeCustomPersona(personas, "custom-a"), [personas[1]]);
});

test("deleting the active persona falls back to friendly", () => {
  assert.equal(personaAfterDelete("custom-a", "custom-a"), "friendly");
  assert.equal(personaAfterDelete("technical", "custom-a"), "technical");
});
