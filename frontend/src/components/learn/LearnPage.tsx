import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams, Link } from "react-router-dom";
import {
  ArrowLeft,
  Layers,
  Plus,
  Play,
  Pencil,
  Trash2,
  Loader2,
  BookOpen,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { PageContainer, PageHeader } from "@/components/ui/primitives";
import { MobileMenuButton } from "@/components/layout/Sidebar";
import {
  flashcardsApi,
  type Deck,
  type Flashcard,
} from "@/api/flashcardsClient";
import { useT } from "@/store/settingsStore";
import { CardMarkdown } from "./CardMarkdown";
import { CardEditor, ConfirmDelete, fieldClass } from "./LearnDialogs";
import { StudySession } from "./StudySession";
import { SourceAction } from "./SourceAction";
import { DeckCardStack } from "./DeckCardStack";
import { LearnNav, NotesList, NotesPage } from "./NotesPage";
import { QuizzesPage, QuizzesList } from "./QuizzesPage";
import { NotebooksPage, NotebooksList } from "./NotebooksPage";

function DeckEditor({
  deck,
  onClose,
  onSaved,
}: {
  deck?: Deck;
  onClose: () => void;
  onSaved: (deck: Deck) => void;
}) {
  const t = useT();
  const [title, setTitle] = useState(deck?.title ?? "");
  const [description, setDescription] = useState(deck?.description ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {t(deck ? "learn.editDeck" : "learn.create")}
          </DialogTitle>
        </DialogHeader>
        <label className="space-y-1 text-sm">
          {t("learn.deckTitle")}
          <input
            autoFocus
            maxLength={200}
            className={fieldClass}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label className="space-y-1 text-sm">
          {t("learn.description")}
          <textarea
            maxLength={2000}
            rows={3}
            className={fieldClass}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            {t("learn.cancel")}
          </Button>
          <Button
            disabled={busy || !title.trim()}
            onClick={async () => {
              setBusy(true);
              try {
                const saved = deck
                  ? await flashcardsApi.edit(deck.id, title, description)
                  : await flashcardsApi.create(title, description);
                onSaved(saved);
                onClose();
              } catch (error) {
                setError(
                  error instanceof Error ? error.message : t("learn.error"),
                );
              } finally {
                setBusy(false);
              }
            }}
          >
            {t(busy ? "learn.saving" : "learn.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeckDetail({ id }: { id: string }) {
  const t = useT();
  const navigate = useNavigate();
  const [deck, setDeck] = useState<Deck | null>(null);
  const [error, setError] = useState("");
  const [editDeck, setEditDeck] = useState(false);
  const [editCard, setEditCard] = useState<Flashcard | "new" | null>(null);
  const [deleting, setDeleting] = useState<Flashcard | "deck" | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    flashcardsApi
      .get(id, controller.signal)
      .then(setDeck)
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      });
    return () => controller.abort();
  }, [id]);
  if (!deck)
    return (
      <div className="p-6">
        {error ? (
          <p role="alert">{error}</p>
        ) : (
          <Loader2 className="size-5 animate-spin" />
        )}
        <Button variant="ghost" onClick={() => navigate("/learn/flashcards")}>
          {t("learn.return")}
        </Button>
      </div>
    );
  return (
    <PageContainer>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => navigate("/learn/flashcards")}
      >
        <ArrowLeft className="size-4" />
        {t("learn.flashcards")}
      </Button>
      <PageHeader
        title={deck.title}
        description={deck.description ?? undefined}
        actions={
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditDeck(true)}
            >
              <Pencil className="size-4" />
              {t("learn.editDeck")}
            </Button>
            <Button
              size="sm"
              disabled={!deck.card_count}
              onClick={() => navigate(`/learn/flashcards/${id}/study`)}
            >
              <Play className="size-4" />
              {t("learn.study")}
            </Button>
          </div>
        }
      />
      <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground mb-6">
        {deck.source_note_id && (
          <Link
            className="text-xs hover:text-primary"
            to={`/learn/notes/${deck.source_note_id}`}
          >
            {t("learn.viewNote")}
          </Link>
        )}
        {deck.source_artifact && (
          <Link
            className="text-xs text-primary hover:underline"
            to={artifactSourceUrl(deck.source_artifact)}
          >
            {t("artifacts.viewSource")}
          </Link>
        )}
        <span>{t("learn.cardCount", { count: deck.card_count })}</span>
        <span>{t("learn.dueCount", { count: deck.due_count })}</span>
        {deck.source_conversation_id && (
          <Link
            className="text-primary hover:underline"
            to={`/${deck.source_conversation_id}`}
          >
            {t("learn.sourceConversation")}
          </Link>
        )}
        {deck.source_document_id && (
          <Link
            className="text-primary hover:underline"
            to={`/workspace/documents?document=${deck.source_document_id}`}
          >
            {t("learn.sourceDocument")}
          </Link>
        )}
      </div>
      <div className="flex justify-between items-center mb-4">
        <h2 className="font-medium">{t("learn.cards")}</h2>
        <Button
          variant="outline"
          size="sm"
          disabled={deck.card_count >= 100}
          onClick={() => setEditCard("new")}
        >
          <Plus className="size-4" />
          {t("learn.addCard")}
        </Button>
      </div>
      {!deck.cards?.length && (
        <p className="py-10 text-center text-muted-foreground">
          {t("learn.emptyCards")}
        </p>
      )}
      <DeckCardStack key={id} count={deck.cards?.length ?? 0}>
        {deck.cards?.map((card, index) => (
          <article
            className="rounded-xl border border-border/60 p-5 bg-card flashcard-deck-unfold"
            style={{
              animationDelay: `${Math.min(index * 35, 280)}ms`,
              animationFillMode: "both",
            }}
            key={card.id}
          >
            <div className="flex justify-between items-center mb-3">
              <span className="text-xs text-muted-foreground">{index + 1}</span>
              <div className="flex gap-1">
                <Button
                  size="icon"
                  variant="ghost"
                  title={t("learn.editCard")}
                  onClick={() => setEditCard(card)}
                >
                  <Pencil className="size-3.5" />
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  title={t("learn.delete")}
                  onClick={() => setDeleting(card)}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </div>
            </div>
            <CardMarkdown text={card.front} />
            <div className="border-t border-border/50 mt-4 pt-3 text-muted-foreground">
              <CardMarkdown text={card.back} />
            </div>
            {card.source_reference && (
              <p dir="auto" className="mt-3 text-xs text-muted-foreground">
                {card.source_reference}
              </p>
            )}
            <div className="mt-2">
              <SourceAction
                documentId={deck.source_document_id}
                conversationId={deck.source_conversation_id}
                page={card.source_page}
                chunk={card.source_chunk_id}
                artifact={card.source_artifact ?? deck.source_artifact}
              />
            </div>
          </article>
        ))}
      </DeckCardStack>
      <Button
        className="mt-6 text-destructive"
        variant="ghost"
        onClick={() => setDeleting("deck")}
      >
        <Trash2 className="size-4" />
        {t("learn.deleteDeck")}
      </Button>
      {editDeck && (
        <DeckEditor
          deck={deck}
          onClose={() => setEditDeck(false)}
          onSaved={setDeck}
        />
      )}
      {editCard && (
        <CardEditor
          initial={editCard === "new" ? undefined : editCard}
          onReplace={
            editCard === "new"
              ? undefined
              : async (cards) =>
                  setDeck(await flashcardsApi.replace(id, editCard.id, cards))
          }
          onClose={() => setEditCard(null)}
          onSave={async (input) =>
            setDeck(
              await (editCard === "new"
                ? flashcardsApi.addCard(id, input)
                : flashcardsApi.editCard(id, editCard.id, input)),
            )
          }
        />
      )}
      {deleting && (
        <ConfirmDelete
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            if (deleting === "deck") {
              await flashcardsApi.delete(id);
              navigate("/learn/flashcards");
            } else {
              await flashcardsApi.deleteCard(id, deleting.id);
              setDeck(await flashcardsApi.get(id));
            }
          }}
        />
      )}
    </PageContainer>
  );
}

export function LearnPage() {
  const { pathname } = useLocation();
  const { noteId } = useParams();
  if (pathname.startsWith("/learn/quizzes")) return <QuizzesPage />;
  if (pathname === "/learn/notebooks") return <NotebooksPage />;
  if (pathname === "/learn/notes" || pathname.startsWith("/learn/notes/"))
    return (
      <div className="h-full overflow-y-auto bg-background">
        <div className="md:hidden px-3 pt-2">
          <MobileMenuButton />
        </div>
        <NotesPage id={noteId} />
      </div>
    );
  return <FlashcardsPage />;
}

function FlashcardsPage() {
  const { deckId } = useParams();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const t = useT();
  const [decks, setDecks] = useState<Deck[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    if (deckId) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    flashcardsApi
      .list(controller.signal)
      .then(setDecks)
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [pathname, deckId]);
  const dashboard = pathname === "/learn";
  return (
    <div className="h-full flex flex-col bg-background">
      <div className="md:hidden shrink-0 px-3 pt-2">
        <MobileMenuButton />
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        {pathname === "/learn/review" ? (
          <StudySession deckId="review" />
        ) : deckId ? (
          pathname.endsWith("/study") ? (
            <StudySession key={deckId} deckId={deckId} />
          ) : (
            <DeckDetail key={deckId} id={deckId} />
          )
        ) : (
          <PageContainer>
            <PageHeader
              title={t(dashboard ? "learn.title" : "learn.flashcards")}
              description={t("learn.subtitle")}
              actions={
                <Button onClick={() => setCreating(true)}>
                  <Plus className="size-4" />
                  {t("learn.create")}
                </Button>
              }
            />
            <LearnNav active={dashboard ? "overview" : "flashcards"} />
            {loading ? (
              <Loader2 className="size-5 animate-spin" />
            ) : error ? (
              <p role="alert" className="text-destructive">
                {error}
              </p>
            ) : (
              <>
                {dashboard && (
                  <div className="grid sm:grid-cols-2 gap-4 mb-8">
                    <div className="rounded-xl border border-border/60 p-5 bg-card">
                      <p className="text-sm text-muted-foreground">
                        {t("learn.due")}
                      </p>
                      <p className="text-3xl font-semibold mt-2">
                        {decks.reduce((sum, deck) => sum + deck.due_count, 0)}
                      </p>
                      {decks.some((deck) => deck.due_count > 0) ? (
                        <Button
                          className="mt-4"
                          onClick={() => navigate("/learn/review")}
                        >
                          <Play className="size-4" />
                          {t("learn.reviewNow")}
                        </Button>
                      ) : (
                        <p className="mt-4 text-sm text-primary">
                          {t("learn.noDue")}
                        </p>
                      )}
                    </div>
                    <div className="rounded-xl border border-border/60 p-5 bg-card">
                      <p className="text-sm text-muted-foreground">
                        {t("learn.decks")}
                      </p>
                      <p className="text-3xl font-semibold mt-2">
                        {decks.length}
                      </p>
                    </div>
                  </div>
                )}
                {!decks.length ? (
                  <div className="text-center py-16 flex flex-col items-center gap-3">
                    <BookOpen className="size-9 text-muted-foreground/50" />
                    <h2 className="font-medium text-lg">{t("learn.empty")}</h2>
                    <p className="text-sm text-muted-foreground max-w-sm">
                      {t("learn.emptyDescription")}
                    </p>
                    <Button className="mt-3" onClick={() => setCreating(true)}>
                      {t("learn.create")}
                    </Button>
                  </div>
                ) : (
                  <>
                    <h2 className="font-medium mb-4">
                      {t(dashboard ? "learn.recent" : "learn.decks")}
                    </h2>
                    <div className="grid sm:grid-cols-2 gap-4">
                      {(dashboard ? decks.slice(0, 6) : decks).map((deck) => (
                        <article
                          key={deck.id}
                          className="rounded-xl border border-border/60 bg-card p-5 hover:border-primary/30 transition-colors"
                        >
                          <Link
                            className="flex items-start gap-3"
                            to={`/learn/flashcards/${deck.id}`}
                          >
                            <Layers className="size-5 text-primary mt-0.5 shrink-0" />
                            <div className="min-w-0">
                              <h3 className="font-medium truncate">
                                {deck.title}
                              </h3>
                              {deck.description && (
                                <p className="text-sm text-muted-foreground mt-1 line-clamp-2">
                                  {deck.description}
                                </p>
                              )}
                            </div>
                          </Link>
                          <div className="mt-4 flex items-center justify-between gap-2">
                            <span className="text-xs text-muted-foreground">
                              {t("learn.cardCount", { count: deck.card_count })}{" "}
                              · {t("learn.dueCount", { count: deck.due_count })}
                            </span>
                            <Button
                              className="bg-primary text-primary-foreground hover:bg-primary/80 shadow-sm cursor-pointer"
                              size="sm"
                              disabled={!deck.card_count}
                              onClick={() =>
                                navigate(`/learn/flashcards/${deck.id}/study`)
                              }
                            >
                              {t("learn.study")}
                            </Button>
                          </div>
                          <p className="mt-2 text-xs text-muted-foreground">
                            {t("learn.lastStudied")}:{" "}
                            {deck.last_studied_at
                              ? new Date(deck.last_studied_at).toLocaleString()
                              : t("learn.notStudied")}
                          </p>
                        </article>
                      ))}
                    </div>
                  </>
                )}
              </>
            )}
            {dashboard && (
              <div className="mt-8">
                <NotebooksList recent />
              </div>
            )}
            {dashboard && <NotesList recent />}
            {dashboard && <QuizzesList recent />}
          </PageContainer>
        )}
      </div>
      {creating && (
        <DeckEditor
          onClose={() => setCreating(false)}
          onSaved={(deck) => navigate(`/learn/flashcards/${deck.id}`)}
        />
      )}
    </div>
  );
}
import { artifactSourceUrl } from "@/api/artifactsClient";
