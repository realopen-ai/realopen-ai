import { useCallback, useEffect, useRef, useState } from "react";
import {
  ExternalLink,
  HardDrive,
  Loader2,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import {
  modelManager,
  type CatalogModel,
  type InstalledModel,
  type ModelInventory,
} from "@/api/modelManagerClient";
import {
  activeModelJob,
  modelBytes,
  modelJobProgress,
} from "@/lib/modelManager";
import { useModelManagerStore } from "@/store/modelManagerStore";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { fetchModels } from "@/api/client";
import { useChatStore } from "@/store/chatStore";
import { useAiStore } from "@/store/aiStore";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import {
  ModelConfiguration,
  ModelDetails,
} from "@/components/brain/ModelDetails";
import { LocalModelImport } from "@/components/brain/LocalModelImport";
import { SectionHeader } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const input =
  "h-10 rounded-xl border border-border bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring/50 min-w-0";

export function ModelsTab() {
  const t = useT();
  const [tab, setTab] = useState<"installed" | "discover" | "import">(
    "installed",
  );
  const [inventory, setInventory] = useState<ModelInventory | null>(null);
  const [catalogs, setCatalogs] = useState<ModelInventory["catalogs"]>([]);
  const [catalog, setCatalog] = useState("ollama");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CatalogModel[]>([]);
  const [detail, setDetail] = useState<CatalogModel | null>(null);
  const jobs = useModelManagerStore((s) => s.jobs);
  const setJobs = useModelManagerStore((s) => s.setJobs);
  const revision = useModelManagerStore((s) => s.revision);
  const [error, setError] = useState<string | null>(null);
  const disconnected = useModelManagerStore((s) => s.disconnected);
  const [busy, setBusy] = useState<string | null>(null);
  const [remove, setRemove] = useState<InstalledModel | null>(null);
  const [history, setHistory] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    const listing = await modelManager.inventory();
    setInventory(listing);
    setCatalogs(listing.catalogs);
    setCatalog((current) =>
      listing.catalogs.some((c) => c.id === current)
        ? current
        : (listing.catalogs[0]?.id ?? ""),
    );
    await useAiStore.getState().load();
    const models = await fetchModels();
    useChatStore.getState().setModels(models.models);
  }, []);

  useEffect(() => {
    let mounted = true;
    void modelManager
      .catalogs()
      .then((data) => {
        if (mounted) setCatalogs(data.catalogs);
      })
      .catch(() => {});
    void refresh().catch((e: Error) => {
      if (mounted) setError(e.message);
    });
    return () => {
      mounted = false;
      abort.current?.abort();
    };
  }, [refresh]);

  useEffect(() => {
    if (revision) void refresh().catch((e: Error) => setError(e.message));
  }, [revision, refresh]);

  async function act(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await action();
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError"))
        setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }
  async function inspect(reference: string, source = catalog) {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    const data = await modelManager.inspect(
      source,
      reference,
      controller.signal,
    );
    setDetail(data);
  }
  const shownJobs = history
    ? jobs
    : jobs.filter((job) => activeModelJob(job.status));

  return (
    <div className="space-y-6">
      <SectionHeader
        title={t("models.title")}
        description={t("models.description")}
        actions={
          <Button
            variant="ghost"
            size="sm"
            disabled={busy !== null}
            onClick={() => void act("refresh", refresh)}
            aria-label={t("models.refresh")}
          >
            <RefreshCw
              className={cn("h-4 w-4", busy === "refresh" && "animate-spin")}
            />
          </Button>
        }
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex rounded-xl border border-border p-1">
          {(
            [
              "installed",
              "discover",
              ...(inventory?.local_import ? ["import" as const] : []),
            ] as const
          ).map((value) => (
            <button
              key={value}
              onClick={() => setTab(value)}
              className={cn(
                "rounded-lg px-4 py-2 text-sm transition-colors",
                tab === value
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t(`models.${value}`)}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
          {inventory?.runtimes.map((r) => (
            <span key={r.id} className="inline-flex items-center gap-1.5">
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  r.available ? "bg-emerald-500" : "bg-amber-500",
                )}
              />
              {r.label}
              {r.version && ` · ${r.version}`} ·{" "}
              {t(r.available ? "models.online" : "models.offline")}
            </span>
          ))}
        </div>
      </div>
      {error && (
        <div
          role="alert"
          className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive"
        >
          {error}
        </div>
      )}
      {disconnected && (
        <p role="status" className="text-xs text-amber-500">
          {t("models.reconnecting")}
        </p>
      )}

      {jobs.length > 0 && (
        <section className="space-y-3" aria-label={t("models.downloads")}>
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">{t("models.downloads")}</h3>
            <button
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => setHistory(!history)}
            >
              {t(history ? "models.less" : "models.history")}
            </button>
          </div>
          {shownJobs.map((job) => {
            const progress = modelJobProgress(job.completed, job.total);
            const active = activeModelJob(job.status);
            return (
              <div
                key={job.id}
                className="rounded-2xl border border-border bg-muted/20 p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="break-all text-sm font-medium">
                      {job.request.name}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t(
                        `models.status.${job.status}` as Parameters<
                          typeof t
                        >[0],
                      )}{" "}
                      · {job.request.runtime}
                    </p>
                  </div>
                  {active ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy !== null}
                      onClick={() =>
                        void act(job.id, async () => {
                          await modelManager.cancel(job.id);
                        })
                      }
                    >
                      {t("models.cancel")}
                    </Button>
                  ) : (
                    job.status !== "completed" && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy !== null}
                        onClick={() =>
                          void act(job.id, async () => {
                            await modelManager.retry(job.id);
                          })
                        }
                      >
                        {t("models.retry")}
                      </Button>
                    )
                  )}
                </div>
                <p className="mt-2 break-words text-xs text-muted-foreground">
                  {job.message}
                </p>
                {active && (
                  <div className="mt-3">
                    <div
                      role="progressbar"
                      aria-label={job.request.name}
                      aria-valuenow={progress ?? undefined}
                      className="h-1.5 overflow-hidden rounded-full bg-muted"
                    >
                      <div
                        className={cn(
                          "h-full rounded-full bg-primary transition-[width]",
                          progress === null && "w-1/3 animate-pulse",
                        )}
                        style={
                          progress === null
                            ? undefined
                            : { width: `${progress}%` }
                        }
                      />
                    </div>
                    {job.total > 0 && (
                      <p className="mt-1 text-end text-[11px] text-muted-foreground">
                        {modelBytes(job.completed)} / {modelBytes(job.total)}
                      </p>
                    )}
                  </div>
                )}
                <details className="mt-2 text-xs text-muted-foreground">
                  <summary className="cursor-pointer">
                    {t("models.details")}
                  </summary>
                  <div className="mt-2 space-y-1 break-all">
                    <p>
                      {job.request.catalog} ·{" "}
                      {job.source_title ?? job.request.model} ·{" "}
                      {job.request.variant}
                    </p>
                    {job.request.revision && (
                      <p>
                        {t("models.revision")}: {job.request.revision}
                      </p>
                    )}
                    <p>{new Date(job.created_at).toLocaleString()}</p>
                    {job.source_url && (
                      <a
                        href={job.source_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-primary"
                      >
                        {t("models.source")}
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                    {job.configuration && (
                      <ModelConfiguration configuration={job.configuration} />
                    )}
                    {job.capabilities && <p>{job.capabilities.join(" · ")}</p>}
                  </div>
                </details>
              </div>
            );
          })}
        </section>
      )}

      {tab === "installed" && (
        <section className="space-y-3">
          <p className="text-xs text-muted-foreground">
            {t("models.selectionHelp")}
          </p>
          {inventory?.models.length === 0 && (
            <div className="rounded-2xl border border-dashed border-border p-10 text-center">
              <HardDrive className="mx-auto mb-3 h-7 w-7 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">
                {t("models.empty")}
              </p>
              <Button className="mt-4" onClick={() => setTab("discover")}>
                {t("models.discover")}
              </Button>
            </div>
          )}
          {inventory?.models.map((model) => (
            <article
              key={`${model.runtime}:${model.name}`}
              className="flex items-center gap-4 rounded-2xl border border-border p-4"
            >
              <div className="rounded-xl bg-muted p-3">
                <HardDrive className="h-5 w-5 text-muted-foreground" />
              </div>
              <div className="min-w-0 flex-1">
                <h3 className="break-all text-sm font-medium">{model.name}</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {[
                    model.runtime,
                    modelBytes(model.size),
                    model.quantization,
                    model.family,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`${t("models.remove")} ${model.name}`}
                onClick={() => setRemove(model)}
              >
                <Trash2 className="h-4 w-4 text-muted-foreground" />
              </Button>
            </article>
          ))}
        </section>
      )}

      {tab === "discover" && (
        <section className="space-y-4">
          <p className="text-xs text-muted-foreground">
            {t("models.connectivity")}
          </p>
          <form
            className="flex flex-wrap gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void act("search", async () => {
                setDetail(null);
                if (
                  query.includes("://") ||
                  (catalog === "ollama" && query.includes(":"))
                ) {
                  await inspect(query);
                } else {
                  abort.current?.abort();
                  abort.current = new AbortController();
                  const data = await modelManager.search(
                    catalog,
                    query,
                    abort.current.signal,
                  );
                  setResults(data.models);
                }
              });
            }}
          >
            <select
              className={input}
              value={catalog}
              onChange={(e) => {
                setCatalog(e.target.value);
                setResults([]);
                setDetail(null);
              }}
              aria-label={t("models.catalog")}
            >
              {catalogs.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
            <input
              className={cn(input, "flex-1 min-w-48")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("models.searchPlaceholder")}
              maxLength={600}
              aria-label={t("models.searchPlaceholder")}
            />
            <Button type="submit" disabled={!query.trim() || busy !== null}>
              {busy === "search" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Search className="h-4 w-4" />
              )}
              {t("models.search")}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!query.trim() || busy !== null}
              onClick={() => void act("inspect", () => inspect(query))}
            >
              {busy === "inspect" && (
                <Loader2 className="h-4 w-4 animate-spin" />
              )}
              {t("models.inspect")}
            </Button>
          </form>
          {detail && (
            <ModelDetails
              key={detail.id}
              detail={detail}
              runtimes={inventory?.runtimes ?? []}
              onClose={() => setDetail(null)}
              onStarted={(job) => {
                setJobs((rows) => [
                  job,
                  ...rows.filter((r) => r.id !== job.id),
                ]);
                setDetail(null);
              }}
            />
          )}
          {!detail &&
            results.map((result) => (
              <button
                key={`${result.catalog}:${result.id}`}
                onClick={() =>
                  void act(result.id, () => inspect(result.id, result.catalog))
                }
                disabled={busy !== null}
                className="block w-full rounded-2xl border border-border p-4 text-start transition-colors hover:bg-muted/30"
              >
                <h3 className="break-all text-sm font-medium">
                  {result.title}
                </h3>
                <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                  {result.description || result.catalog}
                </p>
              </button>
            ))}
        </section>
      )}
      {tab === "import" && inventory?.local_import && (
        <LocalModelImport
          options={inventory.local_import}
          runtimes={inventory.runtimes}
          onStarted={(job) => {
            setJobs((rows) => [job, ...rows.filter((r) => r.id !== job.id)]);
            setTab("installed");
          }}
        />
      )}
      {remove && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open) setRemove(null);
          }}
        >
          <DialogContent className="space-y-4">
            <DialogTitle className="font-medium">
              {t("models.remove")} {remove.name}?
            </DialogTitle>
            <DialogDescription className="text-sm text-muted-foreground">
              {t("models.removeHelp")}
            </DialogDescription>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setRemove(null)}>
                {t("models.cancel")}
              </Button>
              <Button
                variant="destructive"
                disabled={busy !== null}
                onClick={() =>
                  void act("remove", async () => {
                    await modelManager.remove(remove.runtime, remove.name);
                    setRemove(null);
                    await refresh();
                  })
                }
              >
                {t("models.remove")}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
