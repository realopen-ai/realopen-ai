import { useEffect, useState } from "react";
import { Check, CheckCircle2, XCircle, CircleHelp, Circle } from "lucide-react";
import { useLocation, useSearchParams } from "react-router-dom";
import { quizzesApi, type QuizAttempt } from "@/api/quizzesClient";
import { Button } from "@/components/ui/button";
import { useT } from "@/store/settingsStore";
import { CardMarkdown } from "./CardMarkdown";
import { SourceAction } from "./SourceAction";
import { fieldClass } from "./LearnDialogs";
import { DeckArtifact } from "./DeckArtifact";
import type { Deck } from "@/api/flashcardsClient";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function QuizSession({
  quizId,
  initial,
  onClose,
}: {
  quizId: string;
  initial?: QuizAttempt;
  onClose: () => void;
}) {
  const t = useT();
  const { pathname } = useLocation();
  const [params, setParams] = useSearchParams();
  const fullPage = pathname === `/learn/quizzes/${quizId}/take`;
  const requested = fullPage ? params.get("attempt") : null;
  const [attempt, setAttempt] = useState<QuizAttempt | null>(initial ?? null);
  const [answers, setAnswers] = useState<QuizAttempt["answers"]>(
    initial?.answers ?? {},
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cards, setCards] = useState<
    { question_id: string; front: string; back: string }[] | null
  >(null);
  const [deck, setDeck] = useState<Deck | null>(null);
  useEffect(() => {
    if (initial || (requested && attempt?.id === requested)) return;
    let alive = true;
    (requested
      ? quizzesApi.attempt(quizId, requested)
      : quizzesApi.start(quizId)
    )
      .then((a) => {
        if (alive) {
          setAttempt(a);
          setAnswers(a.answers);
        }
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, [quizId, initial, requested]);
  useEffect(() => {
    if (fullPage && attempt && params.get("attempt") !== attempt.id) {
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          next.set("attempt", attempt.id);
          return next;
        },
        { replace: true },
      );
    }
  }, [fullPage, attempt?.id, setParams]);
  async function run(operation: () => Promise<QuizAttempt>) {
    setBusy(true);
    setError("");
    try {
      setAttempt(await operation());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function change(id: string, value: string | number) {
    setAnswers((previous) => ({ ...previous, [id]: value }));
  }
  return (
    <div className="p-5 space-y-5 overflow-y-auto h-full">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-medium">
          {attempt?.snapshot.title ?? t("learn.quizzes")}
        </h2>
        <Button variant="ghost" onClick={onClose}>
          {t("common.close")}
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {!attempt ? (
        <p>{t("learn.loading")}</p>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            {attempt.submitted_at
              ? `${attempt.score} / ${attempt.snapshot.questions.length}`
              : `${Object.values(answers).filter((a) => a !== null && a !== "").length} / ${attempt.snapshot.questions.length}`}
          </p>
          {attempt.needs_review > 0 && (
            <p>
              {t("quiz.needsReview")}: {attempt.needs_review}
            </p>
          )}
          {attempt.snapshot.questions.map((q, index) => (
            <section
              key={q.id}
              className="border border-border rounded-xl p-5 space-y-3"
            >
              <p className="text-xs text-muted-foreground">
                {index + 1} / {attempt.snapshot.questions.length}
              </p>
              <CardMarkdown text={q.prompt} />
              {q.type === "multiple_choice" ? (
                <div className="space-y-2">
                  {q.options.map((option, i) => (
                    <label
                      key={i}
                      className={`relative flex gap-3 items-start rounded-lg border p-3 transition-colors focus-within:ring-2 focus-within:ring-primary/60 ${
                        attempt.submitted_at && q.correct_option === i
                          ? "border-emerald-500/70 bg-emerald-500/10"
                          : attempt.submitted_at && answers[q.id] === i
                            ? "border-red-500/70 bg-red-500/10"
                            : answers[q.id] === i
                              ? "border-primary bg-primary/10 ring-1 ring-primary/40"
                              : "border-border hover:border-primary/40"
                      } ${attempt.submitted_at || busy ? "" : "cursor-pointer"}`}
                    >
                      <input
                        type="radio"
                        className="sr-only"
                        name={q.id}
                        checked={answers[q.id] === i}
                        disabled={!!attempt.submitted_at || busy}
                        onChange={() => change(q.id, i)}
                      />
                      <span
                        aria-hidden="true"
                        className={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border-2 ${answers[q.id] === i ? "border-primary bg-primary text-primary-foreground" : "border-muted-foreground/60"}`}
                      >
                        {answers[q.id] === i && <Circle className="size-3.5" />}
                      </span>
                      <div className="min-w-0 flex-1">
                        <CardMarkdown text={option} />
                      </div>
                      {attempt.submitted_at && q.correct_option === i && (
                        <CheckCircle2
                          aria-hidden="true"
                          className="mt-0.5 size-5 shrink-0 text-emerald-600 dark:text-emerald-400"
                        />
                      )}
                      {attempt.submitted_at &&
                        answers[q.id] === i &&
                        q.correct_option !== i && (
                          <XCircle
                            aria-hidden="true"
                            className="mt-0.5 size-5 shrink-0 text-red-600 dark:text-red-400"
                          />
                        )}
                    </label>
                  ))}
                </div>
              ) : (
                <textarea
                  aria-label={t("quiz.yourAnswer")}
                  dir="auto"
                  className={fieldClass}
                  value={String(answers[q.id] ?? "")}
                  maxLength={2000}
                  disabled={!!attempt.submitted_at || busy}
                  onChange={(e) => change(q.id, e.target.value)}
                />
              )}
              {attempt.submitted_at && attempt.results && (
                <div className="space-y-2 border-t border-border pt-3">
                  <p
                    className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-sm font-semibold ${attempt.results[q.id].correct === null ? "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400" : attempt.results[q.id].correct ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400" : "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400"}`}
                  >
                    {attempt.results[q.id].correct === null ? (
                      <CircleHelp aria-hidden="true" className="size-4" />
                    ) : attempt.results[q.id].correct ? (
                      <CheckCircle2 aria-hidden="true" className="size-4" />
                    ) : (
                      <XCircle aria-hidden="true" className="size-4" />
                    )}
                    {t(
                      attempt.results[q.id].correct === null
                        ? "quiz.needsReview"
                        : attempt.results[q.id].correct
                          ? "quiz.correct"
                          : "quiz.incorrect",
                    )}
                  </p>
                  <CardMarkdown
                    text={
                      q.type === "multiple_choice"
                        ? q.options[q.correct_option!]
                        : (q.answer ?? "")
                    }
                  />
                  <CardMarkdown text={q.explanation ?? ""} />
                  {attempt.results[q.id].feedback && (
                    <p className="text-sm text-muted-foreground">
                      {attempt.results[q.id].feedback}
                    </p>
                  )}
                  <SourceAction
                    conversationId={attempt.snapshot.source_conversation_id}
                    documentId={q.source_document_id}
                    page={q.source_page}
                    chunk={q.source_chunk_id}
                    artifact={q.source_artifact}
                  />
                  {q.type === "short_answer" && (
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          run(() =>
                            quizzesApi.override(quizId, attempt.id, q.id, true),
                          )
                        }
                      >
                        {t("quiz.acceptAnswer")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() =>
                          run(() =>
                            quizzesApi.override(
                              quizId,
                              attempt.id,
                              q.id,
                              false,
                            ),
                          )
                        }
                      >
                        {t("quiz.markIncorrect")}
                      </Button>
                    </div>
                  )}
                </div>
              )}
            </section>
          ))}
          {!attempt.submitted_at ? (
            <div className="flex gap-3">
              <Button
                disabled={busy}
                onClick={() =>
                  run(() => quizzesApi.submit(quizId, attempt.id, answers))
                }
              >
                {t(busy ? "quiz.grading" : "quiz.submit")}
              </Button>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() =>
                  run(() => quizzesApi.draft(quizId, attempt.id, answers))
                }
              >
                {t("learn.save")}
              </Button>
            </div>
          ) : (
            <div className="flex gap-3">
              <Button
                disabled={busy}
                onClick={async () => {
                  await run(() => quizzesApi.start(quizId));
                  setAnswers({});
                  setDeck(null);
                }}
              >
                {t("quiz.retry")}
              </Button>
              {attempt.score !== attempt.snapshot.questions.length && (
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    quizzesApi
                      .mistakes(quizId, attempt.id)
                      .then(setCards)
                      .catch((e) => setError(e.message))
                  }
                >
                  {t("quiz.mistakes")}
                </Button>
              )}
            </div>
          )}
          {deck && (
            <DeckArtifact
              id={deck.id}
              title={deck.title}
              count={deck.card_count}
            />
          )}
          {cards && (
            <Dialog open onOpenChange={() => setCards(null)}>
              <DialogContent className="max-h-[80vh] overflow-y-auto">
                <DialogHeader>
                  <DialogTitle>{t("quiz.preview")}</DialogTitle>
                </DialogHeader>
                {cards.map((card) => (
                  <div
                    key={card.question_id}
                    className="border border-border rounded-lg p-3"
                  >
                    <CardMarkdown text={card.front} />
                    <CardMarkdown text={card.back} />
                  </div>
                ))}
                <Button
                  disabled={busy || cards.length === 0}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      setDeck(
                        await quizzesApi.saveMistakes(
                          quizId,
                          attempt.id,
                          attempt.snapshot.title,
                          cards.map((c) => c.question_id),
                        ),
                      );
                      setCards(null);
                    } catch (e) {
                      setError((e as Error).message);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {t("learn.save")}
                </Button>
              </DialogContent>
            </Dialog>
          )}
        </>
      )}
    </div>
  );
}
