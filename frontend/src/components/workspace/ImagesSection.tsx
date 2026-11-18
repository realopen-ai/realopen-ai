import { useEffect, useRef, useState } from "react";
import { ImagePlus, Loader2, RefreshCw, Search, X } from "lucide-react";
import { Link } from "react-router-dom";
import { lensClient, lensViewerImage, type LensImage } from "@/api/lensClient";
import { Button } from "@/components/ui/button";
import { useT } from "@/store/settingsStore";
import { useImageViewerStore } from "@/store/imageViewerStore";

type Results = Awaited<ReturnType<typeof lensClient.list>>;

export function ImagesSection() {
  const t = useT();
  const openViewer = useImageViewerStore((s) => s.open);
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [reference, setReference] = useState<File | null>(null);
  const [result, setResult] = useState<Results | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  const tRef = useRef(t);
  tRef.current = t;

  async function load(text: string, file: File | null, offset = 0) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(null);
    if (!offset) setResult(null);
    try {
      const data = file
        ? await lensClient.similar(file, controller.signal)
        : await lensClient.list(text, offset, controller.signal);
      if (request.current !== controller) return;
      setResult((prev) =>
        offset && prev
          ? { ...data, items: [...prev.items, ...data.items] }
          : data,
      );
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(
          (reason instanceof Error && reason.message) ||
            tRef.current("lens.loadError"),
        );
      }
    } finally {
      if (request.current === controller) setBusy(false);
    }
  }

  useEffect(() => {
    void load("", null);
    return () => request.current?.abort();
  }, []);

  const state = result?.state;
  const stages = state
    ? [
        { label: "lens.ocrProgress", done: state.ocr, total: state.total },
        ...(state.siglip2_installed
          ? [
              {
                label: "lens.visualProgress",
                done: state.visual,
                total: state.total,
              },
            ]
          : []),
        ...(state.e5_installed
          ? [
              {
                label: "lens.semanticProgress",
                done: state.semantic,
                total: state.with_text,
              },
            ]
          : []),
      ].filter(({ done, total }) => done < total)
    : [];
  useEffect(() => {
    if (busy || !result || submitted || reference || result.items.length > 48)
      return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void lensClient
        .list("", 0, controller.signal)
        .then((data) => {
          setResult((prev) => (prev === result ? data : prev));
        })
        .catch(() => {});
    }, 5000);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [busy, reference, result, submitted]);

  return (
    <section className="h-full overflow-y-auto px-6 pb-12 pt-5 lg:px-10">
      <div className="mx-auto max-w-300 space-y-4">
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            setReference(null);
            setSubmitted(query.trim());
            void load(query.trim(), null);
          }}
        >
          <div className="relative min-w-52 flex-1">
            <Search
              aria-hidden
              className="pointer-events-none absolute start-3 top-3 size-4 text-muted-foreground"
            />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              maxLength={500}
              aria-label={t("lens.search")}
              placeholder={t("lens.search")}
              className="h-10 w-full rounded-xl border border-border bg-secondary/40 ps-9 pe-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
            />
          </div>
          <Button type="submit" size="sm">
            {t("models.search")}
          </Button>
          <input
            ref={fileInput}
            type="file"
            accept="image/*"
            hidden
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              event.currentTarget.value = "";
              if (!file) return;
              setQuery("");
              setSubmitted("");
              setReference(file);
              void load("", file);
            }}
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => fileInput.current?.click()}
          >
            <ImagePlus className="size-4" /> {t("lens.reference")}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={busy}
            aria-label={t("models.refresh")}
            onClick={() => void load(submitted, reference)}
          >
            <RefreshCw className="size-4" />
          </Button>
        </form>

        {reference && (
          <div className="flex w-fit max-w-full items-center gap-2 rounded-lg border border-border px-2 py-1 text-xs">
            <span className="truncate">{reference.name}</span>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={t("lens.clearReference")}
              onClick={() => {
                setReference(null);
                void load("", null);
              }}
            >
              <X className="size-3.5" />
            </Button>
          </div>
        )}
        {state && (
          <div
            aria-live="polite"
            className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
          >
            {stages.map(({ label, done, total }) => (
              <span key={label}>
                {t(label)
                  .replace("{count}", String(done))
                  .replace("{total}", String(total))}
              </span>
            ))}
            {state.failed > 0 && (
              <span>
                {t("lens.failed").replace("{count}", String(state.failed))}
              </span>
            )}
            {(!state.siglip2_installed || !state.e5_installed) && (
              <Link
                to="/brain/models"
                className="text-foreground underline underline-offset-4"
              >
                {t("lens.installModels")}
              </Link>
            )}
          </div>
        )}
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
        {busy && !result && (
          <Loader2
            aria-label={t("lens.loading")}
            className="size-5 animate-spin text-muted-foreground"
          />
        )}
        {result && !result.items.length && (
          <p className="py-10 text-center text-sm text-muted-foreground">
            {t(submitted || reference ? "lens.noMatches" : "lens.empty")}
          </p>
        )}
        <div className="grid grid-cols-2 gap-3 min-[1100px]:grid-cols-[repeat(auto-fill,minmax(170px,1fr))]">
          {result?.items.map((image: LensImage, index) => (
            <button
              key={image.id}
              type="button"
              title={image.filename}
              onClick={(event) =>
                openViewer(
                  result.items.map(lensViewerImage),
                  index,
                  event.currentTarget,
                )
              }
              className="overflow-hidden rounded-xl border border-border bg-secondary/40 text-start transition-colors hover:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <img
                alt=""
                src={lensClient.thumbnail(image.id)}
                loading="lazy"
                decoding="async"
                className="aspect-4/3 w-full bg-background/60 object-contain"
              />
              <span className="block truncate px-3 py-2 text-sm">
                {image.filename}
              </span>
            </button>
          ))}
        </div>
        {result?.more && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => void load(submitted, reference, result.items.length)}
          >
            {t("lens.loadMore")}
          </Button>
        )}
      </div>
    </section>
  );
}
