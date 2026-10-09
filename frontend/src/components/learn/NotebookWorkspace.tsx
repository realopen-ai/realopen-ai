import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Plus,
  Upload,
  FileText,
  BookOpen,
  Layers,
  Loader2,
  X,
  ExternalLink,
  Pencil,
  Trash2,
} from "lucide-react";
import {
  notebooksApi,
  notebookSourceCount,
  type NotebookKind,
  type NotebookItem,
} from "@/api/notebooksClient";
import { listDocuments, uploadDocumentStream } from "@/api/documentsClient";
import { artifactsApi } from "@/api/artifactsClient";
import { notesApi } from "@/api/notesClient";
import { flashcardsApi } from "@/api/flashcardsClient";
import { useNotebookStore } from "@/store/notebookStore";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { fieldClass, ConfirmDelete } from "./LearnDialogs";
import { CardMarkdown } from "./CardMarkdown";
import { StudySession } from "./StudySession";
import { MobileMenuButton } from "@/components/layout/Sidebar";
import { NotebookSourcePreview } from "./NotebookSourcePreview";

const kinds: NotebookKind[] = ["document", "artifact", "note", "deck"];

function itemUrl(item: NotebookItem) {
  return item.kind === "note"
    ? `/learn/notes/${item.target_id}`
    : item.kind === "deck"
      ? `/learn/flashcards/${item.target_id}`
      : item.kind === "document"
        ? `/workspace/documents?document=${item.target_id}`
        : `/workspace/artifacts/${item.target_id}`;
}

export function NotebookWorkspace({ id }: { id: string }) {
  const t = useT();
  const navigate = useNavigate();
  const saved = useNotebookStore((s) => s.notebook);
  const notebook = saved?.id === id ? saved : null;
  const mobileTab = useNotebookStore((s) => s.mobileTab);
  const streaming = useChatStore(
    (s) =>
      s.conversations
        .find((c) => c.id === notebook?.conversation_id)
        ?.messages.some((m) => m.isStreaming) ?? false,
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [picker, setPicker] = useState(false);
  const [kind, setKind] = useState<NotebookKind>("document");
  const [candidates, setCandidates] = useState<{ id: string; title: string }[]>(
    [],
  );
  const [search, setSearch] = useState("");
  const [loadingCandidates, setLoadingCandidates] = useState(false);
  const [progress, setProgress] = useState("");
  const [preview, setPreview] = useState<{
    item: NotebookItem;
    content: string;
  } | null>(null);
  const [study, setStudy] = useState<string | null>(null);
  useEffect(() => {
    const listener = (event: Event) => {
      const detail = (
        event as CustomEvent<{ notebookId: string; deckId: string }>
      ).detail;
      if (detail?.notebookId === id && detail.deckId) setStudy(detail.deckId);
    };
    window.addEventListener("notebook-study", listener);
    return () => window.removeEventListener("notebook-study", listener);
  }, [id]);
  const [sourcePreview, setSourcePreview] = useState<NotebookItem | null>(null);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [newNote, setNewNote] = useState(false);
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const currentId = useRef(id);
  currentId.current = id;
  const refresh = useCallback(async () => {
    const value = await notebooksApi.get(id);
    if (currentId.current === id)
      useNotebookStore.getState().setNotebook(value);
  }, [id]);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    notebooksApi
      .get(id, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted)
          useNotebookStore.getState().setNotebook(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [id]);
  useEffect(() => {
    if (!streaming) refresh().catch((e) => setError(e.message));
  }, [streaming, refresh]);
  useEffect(() => {
    if (!picker) return;
    let cancelled = false;
    setLoadingCandidates(true);
    const load = async () => {
      if (kind === "document")
        return (await listDocuments()).map((d) => ({
          id: d.id,
          title: d.filename,
        }));
      if (kind === "note")
        return (await notesApi.list()).map((n) => ({
          id: n.id,
          title: n.title,
        }));
      if (kind === "deck")
        return (await flashcardsApi.list()).map((d) => ({
          id: d.id,
          title: d.title,
        }));
      const values = [];
      let offset: number | null = 0;
      while (offset !== null && !cancelled) {
        const result = await artifactsApi.list(offset);
        values.push(
          ...result.items
            .filter((a) => a.kind !== "upload")
            .map((a) => ({ id: a.id, title: a.title })),
        );
        offset = result.next_offset;
      }
      return values;
    };
    load()
      .then((values) => {
        if (!cancelled) setCandidates(values);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoadingCandidates(false);
      });
    return () => {
      cancelled = true;
    };
  }, [picker, kind]);
  const perform = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!notebook)
    return (
      <div className="absolute inset-0 z-40 flex items-center justify-center bg-background">
        {error ? (
          <div className="text-center">
            <p role="alert">{error}</p>
            <Link className="text-primary" to="/learn/notebooks">
              {t("notebooks.back")}
            </Link>
          </div>
        ) : (
          <Loader2 className="size-6 animate-spin" />
        )}
      </div>
    );
  const sources = notebook.items.filter(
    (i) => i.kind === "document" || i.kind === "artifact",
  );
  const materials = notebook.items.filter(
    (i) => i.kind === "note" || i.kind === "deck",
  );
  const prompt = (action: "notes" | "flashcards") => {
    useNotebookStore.getState().setMobileTab("chat");
    requestAnimationFrame(() =>
      window.dispatchEvent(
        new CustomEvent("notebook-prompt", {
          detail: {
            conversationId: notebook.conversation_id,
            prompt: t(
              action === "notes"
                ? "notebooks.notesPrompt"
                : "notebooks.cardsPrompt",
            ),
          },
        }),
      ),
    );
  };
  return (
    <div className="absolute inset-0 z-30 pointer-events-none">
      <header className="pointer-events-auto absolute inset-x-0 top-0 h-14 border-b border-border bg-background flex items-center gap-3 px-4">
        <MobileMenuButton />
        <Link
          to="/learn/notebooks"
          aria-label={t("notebooks.back")}
          className="p-2 hover:bg-accent rounded-lg"
        >
          <ArrowLeft className="size-4 rtl:rotate-180" />
        </Link>
        <BookOpen className="size-5 text-primary shrink-0" />
        <h1 className="font-medium truncate flex-1">{notebook.title}</h1>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("notebooks.edit")}
          onClick={() => {
            setTitle(notebook.title);
            setDescription(notebook.description);
            setEditing(true);
          }}
        >
          <Pencil className="size-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          disabled={streaming || busy}
          aria-label={t("notebooks.delete")}
          onClick={() => setDeleting(true)}
        >
          <Trash2 className="size-4" />
        </Button>
      </header>
      <nav className="pointer-events-auto absolute top-14 inset-x-0 h-10 flex xl:hidden bg-background border-b border-border">
        {(["sources", "chat", "studio"] as const).map((tab) => (
          <button
            key={tab}
            className={`flex-1 text-sm ${mobileTab === tab ? "text-primary border-b-2 border-primary" : "text-muted-foreground"}`}
            onClick={() => useNotebookStore.getState().setMobileTab(tab)}
          >
            {t(`notebooks.${tab}`)}
          </button>
        ))}
      </nav>
      <aside
        className={`pointer-events-auto absolute inset-y-24 xl:inset-y-14 start-0 w-full xl:w-64 border-e border-border bg-card overflow-y-auto p-4 ${mobileTab === "sources" ? "block" : "hidden xl:block"}`}
      >
        <div className="flex justify-between items-center mb-5">
          <h2 className="text-sm font-medium">{t("notebooks.sources")}</h2>
          <span className="text-xs text-muted-foreground">
            {notebookSourceCount(notebook.items)} / {sources.length}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-2 mb-5">
          <Button
            variant="outline"
            size="sm"
            disabled={busy || streaming}
            onClick={() => {
              setKind("document");
              setPicker(true);
            }}
          >
            <Plus className="size-4" />
            {t("notebooks.add")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || streaming}
            onClick={() => fileInput.current?.click()}
          >
            <Upload className="size-4" />
            {t("notebooks.upload")}
          </Button>
        </div>
        <input
          ref={fileInput}
          type="file"
          className="hidden"
          accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.md"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (!file) return;
            let documentId = "";
            await perform(async () => {
              await uploadDocumentStream(file, {
                scope: "private",
                conversationId: notebook.conversation_id,
                onProgress: (p) => setProgress(`${p.percent}% · ${p.details}`),
                onDone: (doc) => {
                  documentId = doc.id;
                },
                onError: (message) => {
                  throw new Error(message);
                },
              });
              if (!documentId) throw new Error(t("notebooks.uploadFailed"));
              await notebooksApi.attach(id, "document", documentId);
            });
            setProgress("");
          }}
        />
        {progress && (
          <p role="status" className="text-xs text-muted-foreground mb-4">
            {progress}
          </p>
        )}
        <p className="text-xs text-muted-foreground mb-4">
          {t("notebooks.sourceHint")}
        </p>
        {sources.length === 0 && (
          <div className="rounded-xl border border-dashed border-border p-5 text-center">
            <FileText className="size-6 mx-auto text-muted-foreground mb-3" />
            <p className="text-sm text-muted-foreground">
              {t("notebooks.noSources")}
            </p>
          </div>
        )}
        <div className="space-y-2">
          {sources.map((source) => (
            <div
              key={source.id}
              className="group flex items-start gap-2 rounded-xl p-3 border border-border/60 hover:bg-accent/40"
            >
              <input
                type="checkbox"
                className="mt-1 accent-primary"
                aria-label={t("notebooks.selectSource", {
                  title: source.title,
                })}
                checked={source.selected}
                disabled={busy || streaming}
                onChange={(e) =>
                  perform(async () => {
                    await notebooksApi.select(id, source.id, e.target.checked);
                  })
                }
              />
              <div className="min-w-0 flex-1">
                <button
                  onClick={() => setSourcePreview(source)}
                  className="text-start text-sm break-words hover:text-primary"
                >
                  {source.title}
                </button>
                {source.status && source.status !== "ready" && (
                  <p className="text-xs text-muted-foreground mt-1">
                    {t(
                      source.status === "failed"
                        ? "notebooks.sourceFailed"
                        : "workspace.documents.digesting",
                    )}
                  </p>
                )}
              </div>
              <button
                aria-label={t("notebooks.removeItem")}
                disabled={busy || streaming}
                onClick={() =>
                  perform(() => notebooksApi.unlink(id, source.id))
                }
                className="text-muted-foreground hover:text-destructive p-0.5"
              >
                <X className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      </aside>
      <aside
        className={`pointer-events-auto absolute inset-y-24 xl:inset-y-14 end-0 w-full xl:w-72 border-s border-border bg-card overflow-y-auto p-4 ${mobileTab === "studio" ? "block" : "hidden xl:block"}`}
      >
        <div className="flex justify-between items-center mb-5">
          <h2 className="text-sm font-medium">{t("notebooks.studio")}</h2>
          <button
            aria-label={t("notebooks.linkMaterials")}
            disabled={busy || streaming}
            onClick={() => {
              setKind("note");
              setPicker(true);
            }}
          >
            <Plus className="size-4" />
          </button>
        </div>
        <div className="grid grid-cols-2 gap-2 mb-5">
          <button
            disabled={streaming || !notebookSourceCount(notebook.items)}
            onClick={() => prompt("notes")}
            className="rounded-xl border border-border bg-background p-4 text-start hover:border-primary/40 disabled:opacity-40"
          >
            <FileText className="size-5 text-primary mb-3" />
            <span className="text-sm">{t("notebooks.generateNotes")}</span>
          </button>
          <button
            disabled={streaming || !notebookSourceCount(notebook.items)}
            onClick={() => prompt("flashcards")}
            className="rounded-xl border border-border bg-background p-4 text-start hover:border-primary/40 disabled:opacity-40"
          >
            <Layers className="size-5 text-primary mb-3" />
            <span className="text-sm">{t("notebooks.generateCards")}</span>
          </button>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="w-full mb-5"
          disabled={busy || streaming}
          onClick={() => setNewNote(true)}
        >
          <Plus className="size-4" />
          {t("notebooks.newNote")}
        </Button>
        {materials.length === 0 && (
          <p className="text-sm text-muted-foreground py-4">
            {t("notebooks.noMaterials")}
          </p>
        )}
        <div className="space-y-2">
          {materials.map((material) => (
            <div
              key={material.id}
              className="rounded-xl border border-border bg-background p-3"
            >
              <div className="flex gap-2 items-center">
                {material.kind === "note" ? (
                  <FileText className="size-4 text-primary shrink-0" />
                ) : (
                  <Layers className="size-4 text-primary shrink-0" />
                )}
                <button
                  className="text-sm text-start truncate flex-1 hover:text-primary"
                  onClick={async () => {
                    if (material.kind === "deck") {
                      setStudy(material.target_id);
                      return;
                    }
                    try {
                      const note = await notesApi.get(material.target_id);
                      setPreview({ item: material, content: note.content });
                    } catch (e) {
                      setError((e as Error).message);
                    }
                  }}
                >
                  {material.title}
                </button>
                <button
                  disabled={busy || streaming}
                  aria-label={t("notebooks.removeItem")}
                  className="text-muted-foreground hover:text-destructive"
                  onClick={() =>
                    perform(() => notebooksApi.unlink(id, material.id))
                  }
                >
                  <X className="size-3.5" />
                </button>
              </div>
              <Link
                className="text-xs text-muted-foreground hover:text-primary block mt-3"
                to={itemUrl(material)}
              >
                {t("notebooks.openLearn")}
              </Link>
            </div>
          ))}
        </div>
      </aside>
      {error && (
        <div className="pointer-events-auto absolute top-24 xl:top-14 start-4 end-4 z-50 bg-destructive/10 border border-destructive/30 rounded-xl p-3 text-sm flex gap-2">
          <p role="alert" className="flex-1">
            {error}
          </p>
          <button aria-label={t("common.close")} onClick={() => setError("")}>
            <X className="size-4" />
          </button>
        </div>
      )}
      {picker && (
        <Dialog open onOpenChange={setPicker}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("notebooks.linkExisting")}</DialogTitle>
            </DialogHeader>
            <select
              aria-label={t("notebooks.kind")}
              className={fieldClass}
              value={kind}
              onChange={(e) => setKind(e.target.value as NotebookKind)}
            >
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {t(`notebooks.kind.${k}`)}
                </option>
              ))}
            </select>
            <input
              aria-label={t("notebooks.search")}
              placeholder={t("notebooks.search")}
              className={fieldClass}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <div className="max-h-80 overflow-y-auto space-y-1">
              {loadingCandidates ? (
                <Loader2 className="size-5 animate-spin" />
              ) : (
                candidates
                  .filter(
                    (c) =>
                      c.title
                        .toLocaleLowerCase()
                        .includes(search.toLocaleLowerCase()) &&
                      !notebook.items.some(
                        (i) => i.kind === kind && i.target_id === c.id,
                      ),
                  )
                  .map((c) => (
                    <button
                      key={c.id}
                      disabled={busy || streaming}
                      className="w-full text-start flex items-center justify-between gap-3 rounded-lg p-3 hover:bg-accent disabled:opacity-50"
                      onClick={() =>
                        perform(async () => {
                          await notebooksApi.attach(id, kind, c.id);
                        })
                      }
                    >
                      <span className="text-sm truncate">{c.title}</span>
                      <Plus className="size-4 shrink-0" />
                    </button>
                  ))
              )}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setPicker(false)}>
                {t("common.close")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {sourcePreview && (
        <NotebookSourcePreview
          key={sourcePreview.target_id}
          id={sourcePreview.target_id}
          title={sourcePreview.title}
          onClose={() => setSourcePreview(null)}
        />
      )}
      {preview && (
        <Dialog open onOpenChange={() => setPreview(null)}>
          <DialogContent className="max-w-3xl">
            <DialogHeader>
              <DialogTitle>{preview.item.title}</DialogTitle>
            </DialogHeader>
            <div className="max-h-[65vh] overflow-y-auto">
              <CardMarkdown text={preview.content} />
            </div>
            <DialogFooter>
              <Button asChild variant="outline">
                <Link to={itemUrl(preview.item)}>
                  <ExternalLink className="size-4" />
                  {t("notebooks.openLearn")}
                </Link>
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {study && (
        <Dialog open onOpenChange={() => setStudy(null)}>
          <DialogContent className="max-w-3xl h-[85vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>{t("learn.study")}</DialogTitle>
            </DialogHeader>
            <StudySession deckId={study} onClose={() => setStudy(null)} />
          </DialogContent>
        </Dialog>
      )}
      {editing && (
        <Dialog open onOpenChange={setEditing}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("notebooks.edit")}</DialogTitle>
            </DialogHeader>
            <label className="space-y-1 text-sm">
              {t("notebooks.title")}
              <input
                className={fieldClass}
                maxLength={200}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>
            <label className="space-y-1 text-sm">
              {t("learn.description")}
              <textarea
                className={fieldClass}
                maxLength={2000}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <DialogFooter>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setEditing(false)}
              >
                {t("learn.cancel")}
              </Button>
              <Button
                disabled={busy || !title.trim()}
                onClick={() =>
                  perform(async () => {
                    await notebooksApi.update(id, title.trim(), description);
                    setEditing(false);
                  })
                }
              >
                {t("learn.save")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {newNote && (
        <Dialog open onOpenChange={setNewNote}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("notebooks.newNote")}</DialogTitle>
            </DialogHeader>
            <input
              aria-label={t("learn.noteTitle")}
              placeholder={t("notebooks.title")}
              className={fieldClass}
              value={noteTitle}
              maxLength={200}
              onChange={(e) => setNoteTitle(e.target.value)}
            />
            <textarea
              aria-label={t("learn.noteContent")}
              className={`${fieldClass} font-mono`}
              rows={10}
              maxLength={40000}
              value={noteContent}
              onChange={(e) => setNoteContent(e.target.value)}
            />
            <DialogFooter>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setNewNote(false)}
              >
                {t("learn.cancel")}
              </Button>
              <Button
                disabled={busy || streaming || !noteTitle.trim()}
                onClick={() =>
                  perform(async () => {
                    const note = await notesApi.create(noteTitle.trim());
                    await notesApi.save(
                      note.id,
                      noteTitle.trim(),
                      noteContent,
                      false,
                    );
                    await notebooksApi.attach(id, "note", note.id);
                    setNewNote(false);
                    setNoteTitle("");
                    setNoteContent("");
                  })
                }
              >
                {t("learn.save")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {deleting && (
        <ConfirmDelete
          warning={t("notebooks.deleteWarning")}
          onClose={() => setDeleting(false)}
          onConfirm={async () => {
            await notebooksApi.delete(id);
            useNotebookStore.getState().setNotebook(null);
            navigate("/learn/notebooks");
          }}
        />
      )}
    </div>
  );
}
