import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  studyQueue,
  shuffled,
  ratingForKey,
} from "../src/lib/flashcardStudy.ts";
import type { Flashcard } from "../src/api/flashcardsClient.ts";
import { isLearnRoute } from "../src/lib/appRoutes.ts";

test("study queue selects due cards; practice includes future cards without mutating input", () => {
  const cards = [
    { id: "future", due_at: "2026-01-03T00:00:00Z", position: 0 },
    { id: "due", due_at: "2026-01-01T00:00:00Z", position: 1 },
  ] as Flashcard[];
  assert.deepEqual(
    studyQueue(cards, false, Date.parse("2026-01-02")).map((c) => c.id),
    ["due"],
  );
  assert.equal(studyQueue(cards, true).length, 2);
  assert.equal(cards[0].id, "future");
  assert.deepEqual(
    shuffled([1, 2, 3], () => 0),
    [2, 3, 1],
  );
});

test("rating keyboard shortcuts and Learn route boundaries", () => {
  assert.deepEqual(["1", "2", "3", "4"].map(ratingForKey), [
    "again",
    "hard",
    "good",
    "easy",
  ]);
  assert.equal(ratingForKey("5"), null);
  assert.equal(isLearnRoute("/learn/flashcards/id/study"), true);
  assert.equal(isLearnRoute("/learning"), false);
});

test("Learn locales cover every visible learning label in all supported languages", () => {
  const locales = ["en", "fr", "ar"].map((language) =>
    JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${language}.json`, import.meta.url),
        "utf8",
      ),
    ),
  );
  const keys = Object.keys(locales[0]).filter((key) =>
    key.startsWith("learn."),
  );
  for (const locale of locales)
    for (const key of keys) assert.ok(locale[key], key);
});
