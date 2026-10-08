import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  FileText,
  Loader2,
  Pin,
  Plus,
  Search,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageContainer, PageHeader } from "@/components/ui/primitives";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  notesApi,
  type StudyNote,
  type NoteAction,
  type NoteProposal,
} from "@/api/notesClient";
import { useT } from "@/store/settingsStore";
import { CardMarkdown } from "./CardMarkdown";
import { artifactSourceUrl } from "@/api/artifactsClient";
import { SourceAction } from "./SourceAction";
import { ConfirmDelete, fieldClass } from "./LearnDialogs";
import { DeckArtifact } from "./DeckArtifact";
import {
  applyNoteProposal,
  selectedNoteSection,
  type NoteSelection,
} from "@/lib/studyNotes";

export function LearnNav({
  active,
}: {
  active: "overview" | "flashcards" | "notes";
}) {
  const t = useT();
  return (
    <nav className="flex gap-4 mb-7 text-sm">
      {(["overview", "flashcards", "notes"] as const).map((item) => (
        <Link
          key={item}
          className={
            active === item
              ? "text-primary"
              : "text-muted-foreground hover:text-foreground"
          }
          to={item === "overview" ? "/learn" : `/learn/${item}`}
        >
          {t(`learn.${item}`)}
        </Link>
      ))}
    </nav>
  );
}

export function NotesList({ recent = false }: { recent?: boolean }) {
  const t = useT();
  const navigate = useNavigate();
  const [notes, setNotes] = useState<StudyNote[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(
      () => {
        setLoading(true);
        setError("");
        notesApi
          .list(search, controller.signal)
          .then(setNotes)
          .catch((e) => {
            if (!controller.signal.aborted) setError(e.message);
          })
          .finally(() => {
            if (!controller.signal.aborted) setLoading(false);
          });
      },
      search ? 200 : 0,
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [search]);
  const create = async () => {
    setBusy(true);
    try {
      const note = await notesApi.create(t("learn.untitledNote"));
      navigate(`/learn/notes/${note.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const displayed = recent
    ? [...notes]
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .slice(0, 4)
    : notes;
  return (
    <section className={recent ? "mt-8" : ""}>
      <div className="flex items-center justify-between gap-3 mb-4">
        <h2 className="font-medium">
          {t(recent ? "learn.recentNotes" : "learn.notes")}
        </h2>
        <Button size="sm" disabled={busy} onClick={create}>
          {busy ? <Loader2 className="animate-spin" /> : <Plus />}
          {t("learn.newNote")}
        </Button>
      </div>
      {!recent && (
        <div className="relative mb-6">
          <Search className="absolute start-3 top-3 size-4 text-muted-foreground" />
          <input
            aria-label={t("learn.searchNotes")}
            maxLength={200}
            className={`${fieldClass} ps-10`}
            placeholder={t("learn.searchNotes")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      )}
      {error && (
        <p role="alert" className="text-destructive mb-3">
          {error}
        </p>
      )}
      {loading ? (
        <Loader2
          aria-label={t("learn.loading")}
          className="size-5 animate-spin"
        />
      ) : !displayed.length ? (
        <div className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          {t(search ? "learn.noMatchingNotes" : "learn.noNotes")}
        </div>
      ) : (
        (recent ? [false] : [true, false]).map((pinned) => {
          const group = recent
            ? displayed
            : displayed.filter((n) => n.pinned === pinned);
          return (
            group.length > 0 && (
              <div key={String(pinned)} className="mb-6">
                {!recent && (
                  <h3 className="text-xs uppercase tracking-wide text-muted-foreground mb-3">
                    {t(pinned ? "learn.pinned" : "learn.notesRecent")}
                  </h3>
                )}
                <div className="grid sm:grid-cols-2 gap-4">
                  {group.map((note) => (
                    <Link
                      key={note.id}
                      to={`/learn/notes/${note.id}`}
                      className="block rounded-xl border border-border/60 bg-card p-5 hover:border-primary/40 transition-colors"
                    >
                      <div className="flex items-center gap-2">
                        <FileText className="size-4 shrink-0" />
                        <h3 className="font-medium truncate">{note.title}</h3>
                        {note.pinned && (
                          <Pin className="size-3 ms-auto shrink-0 text-muted-foreground" />
                        )}
                      </div>
                      <p
                        dir="auto"
                        className="text-sm text-muted-foreground line-clamp-2 mt-3 whitespace-pre-line"
                      >
                        {note.excerpt}
                      </p>
                      <p className="text-xs text-muted-foreground mt-4">
                        {new Date(note.updated_at).toLocaleString()}
                      </p>
                    </Link>
                  ))}
                </div>
              </div>
            )
          );
        })
      )}
    </section>
  );
}

export function NotesPage({ id }: { id?: string }) {
  const t = useT();
  return (
    <PageContainer>
      <PageHeader
        title={t("learn.notes")}
        description={t("learn.notesSubtitle")}
      />
      <LearnNav active="notes" />
      {id ? <NoteDetail key={id} id={id} /> : <NotesList />}
    </PageContainer>
  );
}

function NoteDetail({ id }: { id: string }) {
  const t = useT();
  const navigate = useNavigate();
  const [note, setNote] = useState<StudyNote | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [selection, setSelection] = useState<NoteSelection | null>(null);
  const [proposal, setProposal] = useState<NoteProposal | null>(null);
  const [proposedSelection, setProposedSelection] =
    useState<typeof selection>(null);
  const [count, setCount] = useState(10);
  const [deck, setDeck] = useState<{
    id: string;
    title: string;
    card_count: number;
  } | null>(null);
  const inFlight = useRef(false);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    notesApi
      .get(id, controller.signal)
      .then((n) => {
        setNote(n);
        setTitle(n.title);
        setContent(n.content);
        setPreview(!!n.content);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [id]);
  const dirty = !!note && (title !== note.title || content !== note.content);
  useEffect(() => {
    if (!dirty) return;
    const listener = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", listener);
    return () => window.removeEventListener("beforeunload", listener);
  }, [dirty]);
  async function run(task: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await task();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  async function save(pinned = note!.pinned) {
    const saved = await notesApi.save(id, title, content, pinned);
    setNote(saved);
    return saved;
  }
  function propose(action: NoteAction) {
    const editor = editorRef.current;
    const scope = editor
      ? selectedNoteSection(
          editor.value,
          editor.selectionStart,
          editor.selectionEnd,
        )
      : null;
    void run(async () => {
      await save();
      const result = await notesApi.propose(id, action, scope?.text, count);
      setProposedSelection(scope);
      setProposal(result);
    });
  }
  if (!note)
    return error ? (
      <p role="alert" className="text-destructive">
        {error}
      </p>
    ) : (
      <Loader2
        aria-label={t("learn.loading")}
        className="size-5 animate-spin"
      />
    );
  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          if (dirty) setLeaving(true);
          else navigate("/learn/notes");
        }}
      >
        <ArrowLeft />
        {t("learn.notes")}
      </Button>
      <div className="my-4">
        <label className="sr-only" htmlFor="note-title">
          {t("learn.noteTitle")}
        </label>
        <input
          id="note-title"
          dir="auto"
          disabled={busy}
          className="w-full text-2xl font-semibold bg-transparent border-b border-transparent focus:border-border outline-none py-2"
          maxLength={200}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </div>
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <Button
          size="sm"
          variant={preview ? "ghost" : "secondary"}
          onClick={() => setPreview(false)}
        >
          {t("learn.edit")}
        </Button>
        <Button
          size="sm"
          variant={preview ? "secondary" : "ghost"}
          onClick={() => {
            setPreview(true);
            setSelection(null);
          }}
        >
          {t("learn.preview")}
        </Button>
        <Button
          size="sm"
          disabled={busy || !title.trim() || !dirty}
          onClick={() =>
            void run(async () => {
              await save();
            })
          }
        >
          {busy && <Loader2 className="animate-spin" />}
          {t("learn.save")}
        </Button>
        <span className="text-xs text-muted-foreground">
          {t(dirty ? "learn.unsaved" : "learn.saved")}
        </span>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy || !title.trim()}
          onClick={() =>
            void run(async () => {
              await save(!note.pinned);
            })
          }
        >
          <Pin />
          {t(note.pinned ? "learn.unpin" : "learn.pin")}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("learn.deleteNote")}
          disabled={busy}
          onClick={() => setDeleting(true)}
        >
          <Trash2 />
        </Button>
      </div>
      <div className="flex flex-wrap gap-3 mb-5">
        {note.source_artifact && (
          <Link
            className="text-xs text-primary hover:underline"
            to={artifactSourceUrl(note.source_artifact)}
          >
            {t("artifacts.viewSource")}
          </Link>
        )}
        <SourceAction
          documentId={note.source_document_id}
          page={note.source_page}
          chunk={note.source_chunk_id}
        />
        {note.source_conversation_id && (
          <Link
            className="text-xs text-muted-foreground hover:text-primary"
            to={`/${note.source_conversation_id}`}
          >
            {t("learn.viewConversation")}
          </Link>
        )}
      </div>
      <div className="rounded-xl border border-border/60 bg-card p-4 sm:p-6">
        {preview ? (
          <CardMarkdown text={content || t("learn.emptyNote")} />
        ) : (
          <>
            <label className="sr-only" htmlFor="note-content">
              {t("learn.noteContent")}
            </label>
            <textarea
              ref={editorRef}
              id="note-content"
              dir="auto"
              disabled={busy}
              maxLength={40000}
              className="w-full min-h-80 resize-y bg-transparent font-mono text-sm leading-7 outline-none"
              placeholder={t("learn.notePlaceholder")}
              value={content}
              onChange={(e) => {
                setContent(e.target.value);
                setSelection(null);
              }}
              onSelect={(e) => {
                const el = e.currentTarget;
                setSelection(
                  selectedNoteSection(
                    el.value,
                    el.selectionStart,
                    el.selectionEnd,
                  ),
                );
              }}
            />
          </>
        )}
      </div>
      <div className="mt-5 flex flex-wrap items-center gap-2">
        <span className="w-full text-xs text-muted-foreground mb-1">
          {t(selection ? "learn.selectionActions" : "learn.noteActions")}
        </span>
        {(["shorter", "clearer", "expand", "bullets"] as const).map(
          (action) => (
            <Button
              key={action}
              size="sm"
              variant="outline"
              disabled={busy || !content.trim() || !title.trim()}
              onClick={() => propose(action)}
            >
              {t(`learn.noteAction.${action}`)}
            </Button>
          ),
        )}
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          {t("learn.cardCountLabel")}
          <input
            aria-label={t("learn.cardCountLabel")}
            type="number"
            min={1}
            max={30}
            className={`${fieldClass} !w-16`}
            value={count}
            onChange={(e) =>
              setCount(Math.min(30, Math.max(1, Number(e.target.value) || 1)))
            }
          />
        </label>
        <Button
          size="sm"
          disabled={busy || !content.trim() || !title.trim()}
          onClick={() => propose("flashcards")}
        >
          {t("learn.noteFlashcards")}
        </Button>
      </div>
      {busy && (
        <p
          role="status"
          className="flex items-center gap-2 text-sm text-muted-foreground mt-4"
        >
          <Loader2 className="size-4 animate-spin" />
          {t("learn.working")}
        </p>
      )}
      {error && (
        <p role="alert" className="text-destructive mt-4">
          {error}
        </p>
      )}
      {deck && (
        <div className="mt-5">
          <DeckArtifact
            id={deck.id}
            title={deck.title}
            count={deck.card_count}
          />
        </div>
      )}
      {proposal && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open && !busy) setProposal(null);
          }}
        >
          <DialogContent className="max-h-[90dvh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>{t("learn.reviewProposal")}</DialogTitle>
            </DialogHeader>
            <p className="text-xs text-muted-foreground">
              {t("learn.proposalHint")}
            </p>
            {proposal.content && <CardMarkdown text={proposal.content} />}{" "}
            {proposal.cards?.map((card, i) => (
              <article key={i} className="rounded-lg border border-border p-3">
                <CardMarkdown text={card.front} />
                <hr className="my-2 border-border" />
                <CardMarkdown text={card.back} />
              </article>
            ))}
            {error && (
              <p role="alert" className="text-destructive">
                {error}
              </p>
            )}
            <DialogFooter>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setProposal(null)}
              >
                {t("learn.discard")}
              </Button>
              <Button
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    if (proposal.cards) {
                      setDeck(
                        await notesApi.flashcards(id, title, proposal.cards),
                      );
                    } else if (proposal.content) {
                      const next = applyNoteProposal(
                        content,
                        proposal.content,
                        proposedSelection,
                      );
                      const saved = await notesApi.save(
                        id,
                        title,
                        next,
                        note.pinned,
                      );
                      setContent(saved.content);
                      setNote(saved);
                      setSelection(null);
                    }
                    setProposal(null);
                  })
                }
              >
                {t(proposal.cards ? "learn.create" : "learn.acceptSave")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {deleting && (
        <ConfirmDelete
          warning={t("learn.deleteNoteWarning")}
          onClose={() => setDeleting(false)}
          onConfirm={async () => {
            await notesApi.delete(id);
            navigate("/learn/notes");
          }}
        />
      )}
      {leaving && (
        <Dialog open onOpenChange={setLeaving}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("learn.unsaved")}</DialogTitle>
            </DialogHeader>
            <p className="text-sm text-muted-foreground">
              {t("learn.unsavedNote")}
            </p>
            <DialogFooter>
              <Button variant="outline" onClick={() => setLeaving(false)}>
                {t("learn.cancel")}
              </Button>
              <Button
                variant="destructive"
                onClick={() => navigate("/learn/notes")}
              >
                {t("learn.discard")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}
