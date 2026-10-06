import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { notesApi } from "../src/api/notesClient.ts";
import {
  selectedNoteSection,
  applyNoteProposal,
} from "../src/lib/studyNotes.ts";
import { isLearnRoute } from "../src/lib/appRoutes.ts";

test("note selections preserve Markdown around the confirmed replacement", () => {
  const text = "## Intro\nFirst paragraph.\n\n## Detail\nOther text.";
  const start = text.indexOf("First");
  const selection = selectedNoteSection(text, start, start + 16);
  assert.ok(selection);
  assert.equal(
    applyNoteProposal(text, "Shorter.", selection),
    "## Intro\nShorter.\n\n## Detail\nOther text.",
  );
  assert.throws(() =>
    applyNoteProposal(text.replace("First", "Changed"), "Shorter", selection),
  );
  assert.equal(selectedNoteSection(text, 0, 0), null);
  assert.equal(selectedNoteSection(text, -1, 4), null);
  assert.equal(applyNoteProposal(text, "Replacement", null), "Replacement");
});

test("notes client sends only focused action and explicit confirmed deck data", async () => {
  const originalFetch = globalThis.fetch;
  const requests: { url: string; body: any; method: string }[] = [];
  globalThis.fetch = async (url, options) => {
    requests.push({
      url: String(url),
      body: options?.body ? JSON.parse(String(options.body)) : undefined,
      method: options?.method ?? "GET",
    });
    return new Response(JSON.stringify({ content: "Proposal" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await notesApi.list("SQL & Markdown");
    await notesApi.propose("note-id", "shorter", "Only this section", 5);
    await notesApi.flashcards("note-id", "Deck", [
      { front: "Question?", back: "Answer." },
    ]);
    assert.equal(requests[0].url, "/api/learn/notes?q=SQL%20%26%20Markdown");
    assert.deepEqual(requests[1].body, {
      action: "shorter",
      selection: "Only this section",
      count: 5,
    });
    assert.deepEqual(Object.keys(requests[2].body), ["title", "cards"]);
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ detail: "Note not found" }), {
        status: 404,
      });
    await assert.rejects(notesApi.get("missing"), /Note not found/);
    globalThis.fetch = async () => new Response(null, { status: 204 });
    assert.equal(await notesApi.delete("note-id"), undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Notes routes, compact artifacts and labels use existing Learn infrastructure", () => {
  assert.equal(isLearnRoute("/learn/notes/id"), true);
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.ok(app.includes('path="/learn/notes/:noteId"'));
  const locales = ["en", "fr", "ar"].map((lang) =>
    JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${lang}.json`, import.meta.url),
        "utf8",
      ),
    ),
  );
  for (const key of Object.keys(locales[0]).filter((k) =>
    k.startsWith("learn."),
  ))
    for (const locale of locales) assert.ok(locale[key], key);
  for (const locale of locales) assert.ok(locale["chat.toolFailed"]);
  const bubble = readFileSync(
    new URL("../src/components/chat/MessageBubble.tsx", import.meta.url),
    "utf8",
  );
  assert.ok(bubble.includes('translate("chat.toolFailed")'));
  assert.ok(bubble.includes('role="alert"'));
  assert.ok(bubble.includes("tc.error || tc.output || tc.title"));
});
