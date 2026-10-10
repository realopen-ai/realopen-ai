import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { quizzesApi } from "../src/api/quizzesClient.ts";

test("quiz client separates drafts, submission, and explicit answer-key editing", async () => {
  const original = globalThis.fetch;
  const requests: { url: string; method: string; body: unknown }[] = [];
  globalThis.fetch = async (url, init) => {
    requests.push({
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return Response.json({ id: "quiz" });
  };
  try {
    await quizzesApi.get("quiz");
    await quizzesApi.editor("quiz");
    await quizzesApi.draft("quiz", "attempt", { q: 0 });
    await quizzesApi.submit("quiz", "attempt", { q: "Answer" });
    await quizzesApi.override("quiz", "attempt", "q", true);
    await quizzesApi.saveMistakes("quiz", "attempt", "Review", ["q"]);
    assert.equal(requests[0].url, "/api/learn/quizzes/quiz");
    assert.equal(requests[1].url, "/api/learn/quizzes/quiz/editor");
    assert.equal(requests[2].method, "PUT");
    assert.deepEqual(requests[2].body, { answers: { q: 0 } });
    assert.equal(requests[3].method, "POST");
    assert.ok(requests[3].url.endsWith("/submit"));
    assert.deepEqual(requests[4].body, { correct: true });
    assert.deepEqual(requests[5].body, {
      title: "Review",
      question_ids: ["q"],
    });
    globalThis.fetch = async () =>
      Response.json({ detail: "Quiz changed" }, { status: 409 });
    await assert.rejects(quizzesApi.save({}, "quiz"), /Quiz changed/);
  } finally {
    globalThis.fetch = original;
  }
});

test("quiz labels are translated in all supported languages", () => {
  const en = JSON.parse(
    readFileSync(
      new URL("../src/i18n/locales/en.json", import.meta.url),
      "utf8",
    ),
  );
  for (const language of ["fr", "ar"]) {
    const locale = JSON.parse(
      readFileSync(
        new URL(`../src/i18n/locales/${language}.json`, import.meta.url),
        "utf8",
      ),
    );
    for (const key of Object.keys(en).filter(
      (key) => key.startsWith("quiz.") || key === "learn.quizzes",
    ))
      assert.ok(locale[key], `${language}: ${key}`);
  }
});

test("routed utility pages hide chat without unmounting its runtime", () => {
  const layout = readFileSync(
    new URL("../src/components/layout/AppLayout.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    layout,
    /showBrainPage \|\| showWorkspacePage \|\| \(showLearnPage && !showNotebook\)/,
  );
  assert.match(layout, /hideChatPage \? "invisible"/);
  assert.match(layout, /mobileTab === "chat" && !hideChatPage/);
  assert.equal(
    (layout.match(/absolute inset-0 z-30 overflow-y-auto bg-background/g) ?? [])
      .length,
    6,
  );
  assert.equal((layout.match(/<ChatArea/g) ?? []).length, 2);
});

test("quiz choices distinguish selection and reveal outcomes only after submission", () => {
  const session = readFileSync(
    new URL("../src/components/learn/QuizSession.tsx", import.meta.url),
    "utf8",
  );
  assert.match(session, /checked=\{answers\[q.id\] === i\}/);
  assert.match(session, /border-primary bg-primary\/10/);
  assert.match(session, /focus-within:ring-2/);
  assert.match(session, /attempt.submitted_at && q.correct_option === i/);
  assert.match(session, /bg-emerald-500\/10/);
  assert.match(session, /bg-red-500\/10/);
  assert.match(session, /bg-amber-500\/10/);
});

test("quiz artifacts match flashcard framing and play action", () => {
  const quiz = readFileSync(
    new URL("../src/components/learn/QuizArtifact.tsx", import.meta.url),
    "utf8",
  );
  const deck = readFileSync(
    new URL("../src/components/learn/DeckArtifact.tsx", import.meta.url),
    "utf8",
  );
  const frame = "my-3 max-w-md rounded-xl border border-border/60 bg-card p-4";
  assert.ok(quiz.includes(frame) && deck.includes(frame));
  assert.match(quiz, /<Play[^>]*className="size-3.5"/);
  assert.match(quiz, /variant="outline" asChild/);
});

test("perfect quiz celebration is submission-only and respects reduced motion", () => {
  const session = readFileSync(
    new URL("../src/components/learn/QuizSession.tsx", import.meta.url),
    "utf8",
  );
  const celebration = readFileSync(
    new URL("../src/components/learn/QuizCelebration.tsx", import.meta.url),
    "utf8",
  );
  assert.match(session, /celebrate &&\s*!attempt\?\.submitted_at/);
  assert.match(session, /next.score === next.snapshot.questions.length/);
  assert.match(session, /next.needs_review === 0/);
  assert.match(
    session,
    /quizzesApi.submit\(quizId, attempt.id, answers\),\s*true/,
  );
  assert.match(session, /playQuizCelebrationSound\(\)/);
  assert.match(session, /if \(celebrate\) prepareQuizCelebrationSound\(\)/);
  assert.match(celebration, /motion-reduce:hidden/);
  assert.match(celebration, /clearTimeout\(timer\)/);
  assert.match(celebration, /pointer-events-none/);
  assert.match(session, /submitButton.current\?\.getBoundingClientRect\(\)/);
  assert.match(session, /ref=\{submitButton\}/);
  assert.match(celebration, /const x = origin.x/);
  assert.match(celebration, /const y = origin.y/);
});
