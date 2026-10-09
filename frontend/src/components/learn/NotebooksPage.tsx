import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { BookOpen, Plus, Loader2, Search } from "lucide-react";
import { notebooksApi, type Notebook } from "@/api/notebooksClient";
import { Button } from "@/components/ui/button";
import { PageContainer, PageHeader } from "@/components/ui/primitives";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogHeader,
  DialogFooter,
} from "@/components/ui/dialog";
import { useT } from "@/store/settingsStore";
import { LearnNav } from "./NotesPage";
import { fieldClass } from "./LearnDialogs";
import { MobileMenuButton } from "@/components/layout/Sidebar";

export function NotebooksList({ recent = false }: { recent?: boolean }) {
  const t = useT();
  const navigate = useNavigate();
  const [notebooks, setNotebooks] = useState<Notebook[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    notebooksApi
      .list(controller.signal)
      .then(setNotebooks)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);
  const displayed = notebooks
    .filter((n) =>
      `${n.title} ${n.description}`
        .toLocaleLowerCase()
        .includes(search.toLocaleLowerCase()),
    )
    .slice(0, recent ? 4 : undefined);
  return (
    <section className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-medium">
          {t(recent ? "notebooks.recent" : "learn.notebooks")}
        </h2>
        <Button size="sm" onClick={() => setCreating(true)}>
          <Plus className="size-4" />
          {t("notebooks.create")}
        </Button>
      </div>
      {!recent && (
        <div className="relative">
          <Search className="absolute start-3 top-3 size-4 text-muted-foreground" />
          <input
            aria-label={t("notebooks.search")}
            placeholder={t("notebooks.search")}
            className={`${fieldClass} ps-10`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {loading ? (
        <Loader2 className="size-5 animate-spin" />
      ) : displayed.length ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {displayed.map((n) => (
            <Link
              key={n.id}
              to={`/learn/notebooks/${n.id}`}
              className="rounded-2xl border border-border bg-card p-5 hover:border-primary/40 transition-colors group"
            >
              <BookOpen className="size-6 text-primary mb-5" />
              <h3 className="font-medium truncate group-hover:text-primary">
                {n.title}
              </h3>
              <p className="text-sm text-muted-foreground line-clamp-2 min-h-10 mt-2">
                {n.description || t("notebooks.description")}
              </p>
              <p className="text-xs text-muted-foreground mt-5">
                {t("notebooks.items", { count: n.items.length })} ·{" "}
                {new Date(n.updated_at).toLocaleDateString()}
              </p>
            </Link>
          ))}
        </div>
      ) : (
        <div className="rounded-2xl border border-dashed border-border py-14 text-center">
          <BookOpen className="size-8 mx-auto text-muted-foreground mb-4" />
          <h3 className="font-medium">
            {t(search ? "notebooks.noResults" : "notebooks.empty")}
          </h3>
          <p className="text-sm text-muted-foreground mt-2">
            {t("notebooks.description")}
          </p>
        </div>
      )}
      {creating && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open && !busy) setCreating(false);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("notebooks.create")}</DialogTitle>
            </DialogHeader>
            <label className="space-y-2 text-sm">
              {t("notebooks.title")}
              <input
                autoFocus
                className={fieldClass}
                maxLength={200}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </label>
            <DialogFooter>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setCreating(false)}
              >
                {t("learn.cancel")}
              </Button>
              <Button
                disabled={busy || !title.trim()}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const n = await notebooksApi.create(title.trim());
                    navigate(`/learn/notebooks/${n.id}`);
                  } catch (e) {
                    setError((e as Error).message);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {t(busy ? "learn.saving" : "notebooks.create")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </section>
  );
}

export function NotebooksPage() {
  const t = useT();
  return (
    <div className="h-full overflow-y-auto bg-background">
      <PageContainer>
        <MobileMenuButton />
        <PageHeader
          title={t("learn.notebooks")}
          description={t("notebooks.description")}
        />
        <LearnNav active="notebooks" />
        <NotebooksList />
      </PageContainer>
    </div>
  );
}
