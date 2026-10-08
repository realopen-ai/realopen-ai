import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, Check, Pencil, Shuffle, Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  flashcardsApi,
  type Deck,
  type Flashcard,
  type Rating,
} from "@/api/flashcardsClient";
import { CardMarkdown } from "./CardMarkdown";
import { CardEditor } from "./LearnDialogs";
import { useT } from "@/store/settingsStore";
import { studyQueue, shuffled, ratingForKey } from "@/lib/flashcardStudy";
import { CardSpeaker } from "./CardSpeaker";
import { SourceAction } from "./SourceAction";

export function StudySession({
  deckId,
  onClose,
}: {
  deckId: string;
  onClose?: () => void;
}) {
  const t = useT();
  const navigate = useNavigate();
  const [deck, setDeck] = useState<Deck | null>(null);
  const [queue, setQueue] = useState<Flashcard[]>([]);
  const [index, setIndex] = useState(0);
  const [revealed, setRevealed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [results, setResults] = useState<{ rating: Rating; due: string }[]>([]);
  const inFlight = useRef(false);
  const reviewKey = useRef<{ card: string; rating: Rating; id: string } | null>(
    null,
  );
  const card = queue[index];
  const daily = deckId === "review";
  const cardDeckId = card?.deck_id ?? deckId;
  const load = useCallback(
    async (signal?: AbortSignal): Promise<Deck> => {
      if (!daily) return flashcardsApi.get(deckId, signal);
      const session = await flashcardsApi.due(signal);
      return {
        id: "review",
        title: "",
        description: null,
        source_document_id: null,
        source_conversation_id: null,
        card_count: session.cards.length,
        due_count: session.cards.length,
        next_review_at: null,
        updated_at: "",
        cards: session.cards,
      };
    },
    [daily, deckId],
  );
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal)
      .then((loaded) => {
        setDeck(loaded);
        setIndex(0);
        setRevealed(false);
        setResults([]);
        reviewKey.current = null;
        setQueue(
          daily ? (loaded.cards ?? []) : studyQueue(loaded.cards ?? [], false),
        );
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      });
    return () => controller.abort();
  }, [load, daily]);
  const close = () => {
    if (onClose) onClose();
    else navigate("/learn");
  };
  const rate = useCallback(
    async (rating: Rating) => {
      if (!card || !revealed || inFlight.current || editing) return;
      inFlight.current = true;
      setBusy(true);
      setError("");
      if (
        reviewKey.current?.card !== card.id ||
        reviewKey.current.rating !== rating
      )
        reviewKey.current = { card: card.id, rating, id: crypto.randomUUID() };
      try {
        const result = await flashcardsApi.review(
          cardDeckId,
          card,
          rating,
          reviewKey.current.id,
        );
        setQueue((cards) =>
          cards.map((item) =>
            item.id === card.id
              ? { ...item, reviews: result.reviews, due_at: result.due_at }
              : item,
          ),
        );
        setResults((items) => [...items, { rating, due: result.due_at }]);
        setIndex((value) => value + 1);
        setRevealed(false);
        reviewKey.current = null;
      } catch (error) {
        setError(error instanceof Error ? error.message : t("learn.error"));
      } finally {
        inFlight.current = false;
        setBusy(false);
      }
    },
    [card, revealed, editing, cardDeckId, t],
  );
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.repeat ||
        target.closest(
          "input, textarea, select, button, [contenteditable], [role=dialog]",
        ) ||
        editing ||
        busy ||
        !card
      )
        return;
      if (event.code === "Space") {
        event.preventDefault();
        setRevealed(true);
      }
      const rating = ratingForKey(event.key, event.code);
      if (rating && revealed) {
        event.preventDefault();
        void rate(rating);
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [card, revealed, busy, editing, rate]);
  const errorView = error && (
    <p role="alert" className="text-sm text-destructive my-3">
      {error}
    </p>
  );
  if (!deck)
    return (
      <div className="p-6">
        {errorView || <Loader2 className="size-5 animate-spin" />}
        <Button variant="ghost" onClick={close}>
          {t("learn.return")}
        </Button>
      </div>
    );
  if (!card)
    return (
      <div className="p-6 flex flex-col items-center justify-center h-full gap-5 text-center">
        <Check className="size-10 text-primary" />
        <h2 className="text-xl font-semibold">
          {t(results.length ? "learn.complete" : "learn.noDue")}
        </h2>
        <p className="text-sm text-muted-foreground">
          {t("learn.reviewed", { count: results.length })}
        </p>
        {results.length > 0 && (
          <>
            <div className="grid grid-cols-2 gap-3 text-sm">
              {(["again", "hard", "good", "easy"] as Rating[]).map((rating) => (
                <div key={rating}>
                  {t(`learn.${rating}`)}:{" "}
                  {results.filter((item) => item.rating === rating).length}
                </div>
              ))}
            </div>
            <p className="text-sm text-muted-foreground">
              {t("learn.nextReview")}:{" "}
              {new Date(
                Math.min(...results.map((item) => Date.parse(item.due))),
              ).toLocaleString()}
            </p>
          </>
        )}
        <div className="flex flex-wrap justify-center gap-2">
          {!daily && (
            <>
              <Button
                disabled={!deck.cards?.length || busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const refreshed = await load();
                    setDeck(refreshed);
                    setQueue(studyQueue(refreshed.cards ?? [], true));
                    setResults([]);
                    setIndex(0);
                    setRevealed(false);
                  } catch {
                    setError(t("learn.error"));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {t("learn.reviewAgain")}
              </Button>
            </>
          )}
          <Button variant="outline" onClick={close}>
            {t("learn.return")}
          </Button>
        </div>
        {errorView}
      </div>
    );
  return (
    <div className="flex h-full flex-col overflow-y-auto bg-background p-5 sm:p-7">
      <header className="flex items-center justify-between gap-2 mb-5">
        <Button variant="ghost" size="sm" onClick={close}>
          <ArrowLeft className="size-4" />
          {t("learn.return")}
        </Button>
        <span className="text-xs tabular-nums text-muted-foreground">
          {index + 1} / {queue.length}
        </span>
      </header>
      <div className="flex items-center justify-between mb-4 gap-2">
        <div className="min-w-0">
          <h2 className="font-medium truncate">
            {daily ? card.deck_title : deck.title}
          </h2>
          <p className="text-xs text-muted-foreground">
            {t(daily ? "learn.dailyReview" : "learn.dueReview")}
          </p>
        </div>
        <div className="flex gap-1">
          <Button
            variant="ghost"
            size="icon"
            disabled={busy}
            title={t("learn.shuffle")}
            onClick={() => {
              setQueue((cards) => [
                ...cards.slice(0, index),
                ...shuffled(cards.slice(index)),
              ]);
              setRevealed(false);
            }}
          >
            <Shuffle className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            disabled={busy}
            title={t("learn.editCard")}
            onClick={() => setEditing(true)}
          >
            <Pencil className="size-4" />
          </Button>
        </div>
      </div>
      <div className="h-1 rounded-full bg-muted mb-6">
        <div
          className="h-full rounded-full bg-primary transition-all"
          style={{ width: `${(index / queue.length) * 100}%` }}
        />
      </div>
      <section className="flex-1 min-h-56 rounded-2xl border border-border/60 bg-card p-6 sm:p-8 shadow-sm">
        <div className="flex items-center justify-between mb-4">
          <p className="text-[11px] uppercase tracking-wider text-muted-foreground">
            {t("learn.front")}
          </p>
          <CardSpeaker key={card.front} text={card.front} />
        </div>
        <CardMarkdown text={card.front} />
        {revealed && (
          <div className="mt-6 pt-5 border-t border-border/60 animate-in fade-in slide-in-from-bottom-2 duration-200 motion-reduce:animate-none">
            <div className="flex items-center justify-between mb-3">
              <p className="text-[11px] uppercase tracking-wider text-muted-foreground">
                {t("learn.back")}
              </p>
              <CardSpeaker key={card.back} text={card.back} />
            </div>
            <CardMarkdown text={card.back} />
            {card.source_reference && (
              <p dir="auto" className="mt-4 text-xs text-muted-foreground">
                {card.source_reference}
              </p>
            )}
          </div>
        )}
        <div className="mt-4">
          <SourceAction
            artifact={card.source_artifact ?? deck.source_artifact}
            documentId={
              daily ? card.source_document_id : deck.source_document_id
            }
            conversationId={
              daily ? card.source_conversation_id : deck.source_conversation_id
            }
            page={card.source_page}
            chunk={card.source_chunk_id}
          />
        </div>
      </section>
      {errorView}
      <div className="mt-5">
        {!revealed ? (
          <Button className="w-full" onClick={() => setRevealed(true)}>
            {t("learn.reveal")}{" "}
            <span className="text-xs opacity-60">{t("learn.space")}</span>
          </Button>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            {(["again", "hard", "good", "easy"] as Rating[]).map(
              (rating, i) => (
                <Button
                  variant={rating === "good" ? "default" : "outline"}
                  disabled={busy}
                  key={rating}
                  onClick={() => void rate(rating)}
                >
                  {t(`learn.${rating}`)}
                  <span className="text-xs opacity-50">{i + 1}</span>
                </Button>
              ),
            )}
          </div>
        )}
      </div>
      {editing && (
        <CardEditor
          initial={card}
          onReplace={async (inputs) => {
            const updated = await flashcardsApi.replace(
              cardDeckId,
              card.id,
              inputs,
            );
            const edited = updated.cards?.find((item) => item.id === card.id);
            if (edited)
              setQueue((cards) =>
                cards.map((item) =>
                  item.id === card.id ? { ...item, ...edited } : item,
                ),
              );
            if (!daily) setDeck(updated);
          }}
          onClose={() => setEditing(false)}
          onSave={async (input) => {
            const updated = await flashcardsApi.editCard(
              cardDeckId,
              card.id,
              input,
            );
            if (!daily) setDeck(updated);
            const edited = updated.cards?.find((item) => item.id === card.id);
            if (edited)
              setQueue((cards) =>
                cards.map((item) =>
                  item.id === card.id ? { ...item, ...edited } : item,
                ),
              );
          }}
        />
      )}
    </div>
  );
}
