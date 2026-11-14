import assert from "node:assert/strict";
import test from "node:test";
import {
  sourceLocationUrl,
  artifactCitationLocations,
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
    artifactCitationLocations(
      JSON.stringify({
        artifact_id: "a",
        version: 3,
        id: "sheet-2",
        title: "Holdings",
      }),
    ),
    [
      {
        artifact: { artifact_id: "a", version: 3, section_id: "sheet-2" },
        title: "Holdings",
      },
    ],
  );
  assert.deepEqual(artifactCitationLocations("Page 3 of a document"), []);
  assert.deepEqual(
    artifactCitationLocations(
      JSON.stringify({ artifact_id: "a", version: 2, sections: [] }),
    ),
    [],
  );
  assert.deepEqual(
    artifactCitationLocations(
      JSON.stringify({ artifact_id: "a", version: -1, id: "part" }),
    ),
    [],
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

test("summary citations retain the summarized version and deduplicate sections", () => {
  const citations = artifactCitationLocations(
    JSON.stringify({
      artifact_id: "presentation",
      version: 2,
      source_references: [
        { id: "slide-4", title: "Findings", version: 9 },
        { id: "slide-4", title: "Findings" },
        null,
        { id: "slide-5", title: "Conclusions" },
      ],
    }),
  );
  assert.deepEqual(citations.map(sourceLocationUrl), [
    "/workspace/artifacts/presentation?version=2&section=slide-4",
    "/workspace/artifacts/presentation?version=2&section=slide-5",
  ]);
});
