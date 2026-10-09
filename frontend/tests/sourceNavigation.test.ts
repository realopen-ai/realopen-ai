import assert from "node:assert/strict";
import test from "node:test";
import {
  sourceLocationUrl,
  artifactReadLocation,
  isNotebookSourceRoute,
} from "../src/lib/sourceNavigation.ts";

test("cached notebook state cannot intercept links outside its active route", () => {
  assert.equal(isNotebookSourceRoute("/learn/notebooks/n", "n"), true);
  assert.equal(isNotebookSourceRoute("/learn/notebooks/n/", "n"), true);
  assert.equal(isNotebookSourceRoute("/learn/notebooks/other", "n"), false);
  assert.equal(isNotebookSourceRoute("/learn/flashcards/deck", "n"), false);
  assert.equal(
    isNotebookSourceRoute("/learn/flashcards/deck/study", "n"),
    false,
  );
  assert.equal(isNotebookSourceRoute("/learn/notes/note", "n"), false);
});

test("read citations use structured IDs, never titles or model prose", () => {
  assert.deepEqual(
    artifactReadLocation(
      JSON.stringify({
        artifact_id: "a",
        version: 3,
        id: "sheet-2",
        title: "Holdings",
      }),
    ),
    {
      artifact: { artifact_id: "a", version: 3, section_id: "sheet-2" },
      title: "Holdings",
    },
  );
  assert.equal(artifactReadLocation("Page 3 of a document"), null);
  assert.equal(
    artifactReadLocation(
      JSON.stringify({ artifact_id: "a", version: 2, sections: [] }),
    ),
    null,
  );
  assert.equal(
    artifactReadLocation(
      JSON.stringify({ artifact_id: "a", version: -1, id: "part" }),
    ),
    null,
  );
});

test("document citations retain page and chunk without guessing a location", () => {
  const url = sourceLocationUrl({ documentId: "doc", page: 5, chunk: "chunk" });
  assert.equal(url, "/workspace/documents?document=doc&page=5&chunk=chunk");
  assert.equal(
    sourceLocationUrl({ documentId: "doc", page: -1 }),
    "/workspace/documents?document=doc",
  );
  assert.equal(sourceLocationUrl({}), null);
});

test("artifact citations retain exact version and encode section IDs", () => {
  assert.equal(
    sourceLocationUrl({
      artifact: { artifact_id: "id", version: 2, section_id: "sheet & one" },
      documentId: "doc",
    }),
    "/workspace/artifacts/id?version=2&section=sheet+%26+one",
  );
});
