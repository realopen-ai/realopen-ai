import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type TouchEvent,
} from "react";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Download,
  LayoutGrid,
  Loader2,
  StickyNote,
  X,
} from "lucide-react";

/** Formats the in-app document viewer supports. */
export type ViewerFormat = "pptx" | "pdf" | "docx" | "xlsx";

/**
 * Modal for viewing documents (PPTX / PDF / DOCX / XLSX) as page images.
 *
 * The backend renders every page as a JPEG (PPTX/DOCX/XLSX via LibreOffice,
 * PDF directly via PyMuPDF — all cached server-side) and exposes:
 *   GET /api/reports/{reportId}/slides?format=pptx|pdf|docx|xlsx → manifest
 *   GET /api/reports/{reportId}/slides/{n}                 → page JPEG
 *   GET /api/reports/{reportId}/slides/{n}?variant=thumb   → thumbnail
 *
 * Spreadsheets are paginated by the workbook's print setup (landscape,
 * fit-to-width, repeated header rows — written by the excel_gen service),
 * so an XLSX preview reads like a print preview of the workbook.
 *
 * This component fetches the manifest on open and presents the document
 * with prev/next controls, a page indicator, a collapsible thumbnail
 * filmstrip, a speaker-notes panel (PPTX decks that carry notes only),
 * keyboard navigation (←/→, Home/End, Esc, N for notes), touch swipe,
 * and adjacent-page preloading.
 */

type SlideInfo = {
  index: number;
  url: string;
  thumb_url: string;
  /** Per-slide speaker notes ("" when the slide has none). */
  notes?: string;
};

type SlidesManifest = {
  report_id: string;
  count: number;
  width: number;
  height: number;
  slides: SlideInfo[];
};

export function FileViewerModal({
  reportId,
  filename,
  downloadUrl,
  format = "pptx",
  onClose,
}: {
  reportId: string;
  filename: string;
  downloadUrl: string;
  /** Deliverable format — selects the backend rendering pipeline. */
  format?: ViewerFormat;
  onClose: () => void;
}) {
  const [manifest, setManifest] = useState<SlidesManifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState(1); // 1-based
  const [slideLoaded, setSlideLoaded] = useState(false);
  const [showThumbs, setShowThumbs] = useState(true);
  const [showNotes, setShowNotes] = useState(true);
  const [retryCount, setRetryCount] = useState(0);

  const filmstripRef = useRef<HTMLDivElement>(null);
  const touchStartX = useRef<number | null>(null);

  const count = manifest?.count ?? 0;
  const hasAnyNotes =
    manifest?.slides.some((s) => (s.notes ?? "").trim().length > 0) ?? false;
  // PPTX decks have "slides"; PDF/DOCX/XLSX documents have "pages".
  const pageWord = format === "pptx" ? "slide" : "page";
  const isPresentation = format === "pptx";
  const documentLabel =
    format === "pptx"
      ? "presentation"
      : format === "xlsx"
        ? "spreadsheet"
        : "document";

  // ── Fetch the slide manifest ────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    setLoading(true);
    setError(null);
    setManifest(null);
    setCurrent(1);

    fetch(`/api/reports/${reportId}/slides?format=${format}`)
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          throw new Error(body?.detail ?? `HTTP ${res.status}`);
        }
        return res.json();
      })
      .then((data: SlidesManifest) => {
        if (cancelled) return;
        setManifest(data);
        setCurrent(1);
        setLoading(false);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setError(e.message || `Failed to load ${documentLabel}`);
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [reportId, format, retryCount]);

  // ── Navigation ──────────────────────────────────────────────────────
  const goTo = useCallback(
    (n: number) => {
      setCurrent(Math.min(Math.max(1, n), Math.max(count, 1)));
    },
    [count],
  );
  const next = useCallback(() => goTo(current + 1), [goTo, current]);
  const prev = useCallback(() => goTo(current - 1), [goTo, current]);

  // Track the slide image load state so we can show a per-slide spinner
  // while a (pre-loaded or fresh) image decodes.
  useEffect(() => {
    setSlideLoaded(false);
  }, [current, manifest]);

  // Preload adjacent slides for instant prev/next.
  useEffect(() => {
    if (!manifest) return;
    for (const n of [current + 1, current - 1]) {
      const slide = manifest.slides[n - 1];
      if (slide) {
        const img = new Image();
        img.src = slide.url;
      }
    }
  }, [manifest, current]);

  // ── Keyboard controls ───────────────────────────────────────────────
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      } else if (
        e.key === "ArrowRight" ||
        e.key === "PageDown" ||
        e.key === " "
      ) {
        e.preventDefault();
        next();
      } else if (e.key === "ArrowLeft" || e.key === "PageUp") {
        e.preventDefault();
        prev();
      } else if (e.key === "Home") {
        goTo(1);
      } else if (e.key === "End") {
        goTo(count);
      } else if (
        (e.key === "n" || e.key === "N") &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        setShowNotes((s) => !s);
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [next, prev, goTo, onClose, count]);

  // ── Keep the active thumbnail visible in the filmstrip ─────────────
  useEffect(() => {
    if (!showThumbs || !filmstripRef.current) return;
    const active = filmstripRef.current.querySelector<HTMLElement>(
      `[data-slide-thumb="${current}"]`,
    );
    active?.scrollIntoView({
      behavior: "smooth",
      inline: "center",
      block: "nearest",
    });
  }, [current, showThumbs, manifest]);

  // ── Touch swipe ─────────────────────────────────────────────────────
  const onTouchStart = (e: TouchEvent<HTMLDivElement>) => {
    touchStartX.current = e.touches[0]?.clientX ?? null;
  };
  const onTouchEnd = (e: TouchEvent<HTMLDivElement>) => {
    if (touchStartX.current === null) return;
    const endX = e.changedTouches[0]?.clientX ?? touchStartX.current;
    const delta = endX - touchStartX.current;
    if (Math.abs(delta) > 40) {
      if (delta < 0) next();
      else prev();
    }
    touchStartX.current = null;
  };

  const currentSlide = manifest?.slides[current - 1] ?? null;
  const currentNotes = (currentSlide?.notes ?? "").trim();
  const atFirst = current <= 1;
  const atLast = current >= count;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal */}
      <div
        className="relative w-full max-w-5xl h-[90vh] mx-4 bg-card border border-border/60 rounded-xl shadow-[0_24px_70px_-12px_var(--color-shadow-strong)] overflow-hidden flex flex-col animate-modal-in"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border/60 shrink-0">
          <p className="text-[13.5px] font-medium text-foreground truncate flex-1">
            {filename}
          </p>

          {/* Page indicator */}
          {manifest && count > 0 && (
            <div className="flex items-center gap-1 text-[12px] tabular-nums text-muted-foreground shrink-0 px-1">
              <span className="text-foreground font-semibold">{current}</span>
              <span>/</span>
              <span>{count}</span>
            </div>
          )}

          {/* Speaker-notes toggle */}
          {manifest && hasAnyNotes && (
            <button
              onClick={() => setShowNotes((s) => !s)}
              className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 ${
                showNotes
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:text-foreground hover:bg-surface-hover"
              }`}
              title={
                showNotes ? "Hide speaker notes (N)" : "Show speaker notes (N)"
              }
              aria-pressed={showNotes}
              aria-label="Toggle speaker notes"
            >
              <StickyNote className="w-4 h-4" />
            </button>
          )}

          {/* Filmstrip toggle */}
          {manifest && count > 1 && (
            <button
              onClick={() => setShowThumbs((s) => !s)}
              className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 ${
                showThumbs
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:text-foreground hover:bg-surface-hover"
              }`}
              title={
                showThumbs
                  ? `Hide ${pageWord} overview`
                  : `Show ${pageWord} overview`
              }
              aria-label={`Toggle ${pageWord} overview`}
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
          )}

          {/* Download button */}
          <a
            href={downloadUrl}
            download={filename}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[12px] font-medium text-muted-foreground hover:text-foreground hover:bg-surface-hover transition-colors"
            title={`Download ${format.toUpperCase()}`}
          >
            <Download className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">Download</span>
          </a>

          {/* Close button */}
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-surface-hover transition-colors shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            aria-label="Close (Esc)"
            title="Close (Esc)"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body — slide stage */}
        <div
          className="flex-1 relative bg-background/40 overflow-hidden flex items-center justify-center"
          onTouchStart={onTouchStart}
          onTouchEnd={onTouchEnd}
        >
          {/* Converting overlay (first render of the manifest) */}
          {loading && !error && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10 px-8 text-center">
              <Loader2 className="w-6 h-6 text-primary animate-spin" />
              <p className="text-[13px] text-muted-foreground">
                Rendering {isPresentation ? "slides" : "pages"}...
              </p>
              <p className="text-[11px] text-muted-foreground/80">
                This happens once per {documentLabel}, then it&apos;s cached.
              </p>
            </div>
          )}

          {/* Error overlay */}
          {error && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10 px-8 text-center">
              <AlertCircle className="w-8 h-8 text-danger" />
              <p className="text-[13px] text-foreground font-medium">
                Failed to load {documentLabel}
              </p>
              <p className="text-[12px] text-muted-foreground/70 max-w-md">
                {error}
              </p>
              <div className="flex items-center gap-2 mt-1">
                <button
                  onClick={() => {
                    setError(null);
                    setLoading(true);
                    setRetryCount((c) => c + 1);
                  }}
                  className="px-3 py-1.5 rounded-lg text-[12px] font-medium bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
                >
                  Retry
                </button>
                <a
                  href={downloadUrl}
                  download={filename}
                  className="px-3 py-1.5 rounded-lg text-[12px] font-medium text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                >
                  Download instead
                </a>
              </div>
            </div>
          )}

          {/* Slide image + controls */}
          {!error && currentSlide && (
            <>
              {/* Previous button */}
              <button
                onClick={prev}
                disabled={atFirst}
                aria-label={`Previous ${pageWord}`}
                className={`absolute left-2 sm:left-3 z-20 w-10 h-10 rounded-full flex items-center justify-center bg-card border border-border/60 text-muted-foreground shadow-[0_8px_30px_var(--color-shadow-soft)] backdrop-blur transition-all ${
                  atFirst
                    ? "opacity-30 cursor-not-allowed"
                    : "hover:bg-surface-hover hover:text-foreground active:scale-95"
                }`}
              >
                <ChevronLeft className="w-5 h-5" />
              </button>

              {/* Next button */}
              <button
                onClick={next}
                disabled={atLast}
                aria-label={`Next ${pageWord}`}
                className={`absolute right-2 sm:right-3 z-20 w-10 h-10 rounded-full flex items-center justify-center bg-card border border-border/60 text-muted-foreground shadow-[0_8px_30px_var(--color-shadow-soft)] backdrop-blur transition-all ${
                  atLast
                    ? "opacity-30 cursor-not-allowed"
                    : "hover:bg-surface-hover hover:text-foreground active:scale-95"
                }`}
              >
                <ChevronRight className="w-5 h-5" />
              </button>

              {/* Per-slide loading spinner (while image decodes) */}
              {!slideLoaded && (
                <div className="absolute inset-0 flex items-center justify-center z-10 pointer-events-none">
                  <Loader2 className="w-6 h-6 text-primary animate-spin" />
                </div>
              )}

              {/* The page */}
              <img
                key={currentSlide.url}
                src={currentSlide.url}
                alt={`${pageWord === "slide" ? "Slide" : "Page"} ${current} of ${count}`}
                draggable={false}
                onLoad={() => setSlideLoaded(true)}
                onError={() => {
                  // Image missing — offer a retry (re-fetches the manifest).
                  setError(
                    `${pageWord === "slide" ? "Slide" : "Page"} ${current} failed to load. The cache may be incomplete.`,
                  );
                }}
                className={`max-w-full max-h-full object-contain select-none transition-opacity duration-150 px-12 sm:px-14 ${
                  slideLoaded ? "opacity-100" : "opacity-0"
                }`}
              />

              {/* Mobile page badge (desktop shows it in the header) */}
              <div className="sm:hidden absolute bottom-2 left-1/2 -translate-x-1/2 px-2.5 py-1 rounded-full bg-card/90 border border-border/60 text-[11px] tabular-nums text-muted-foreground backdrop-blur">
                <span className="text-foreground font-semibold">{current}</span>
                <span> / {count}</span>
              </div>
            </>
          )}
        </div>

        {/* Speaker notes — current slide */}
        {manifest && showNotes && hasAnyNotes && (
          <div
            className="shrink-0 border-t border-border/60 bg-card/80 px-4 py-3"
            role="region"
            aria-label={`Speaker notes for ${pageWord} ${current}`}
          >
            <div className="flex items-center gap-1.5 mb-1.5">
              <StickyNote className="w-3.5 h-3.5 text-primary shrink-0" />
              <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Speaker notes — {pageWord} {current} of {count}
              </p>
            </div>
            <div
              className="max-h-24 sm:max-h-32 overflow-y-auto pr-1 text-[13px] leading-relaxed text-foreground/90 whitespace-pre-wrap"
              style={{ scrollbarWidth: "thin" }}
            >
              {currentNotes ? (
                currentNotes
              ) : (
                <span className="italic text-muted-foreground/80">
                  No notes for this slide.
                </span>
              )}
            </div>
          </div>
        )}

        {/* Filmstrip — slide overview */}
        {manifest && count > 1 && showThumbs && (
          <div
            ref={filmstripRef}
            className="shrink-0 border-t border-border/60 bg-card overflow-x-auto flex gap-2 px-3 py-2.5"
            style={{ scrollbarWidth: "thin" }}
            role="tablist"
            aria-label="Slides"
          >
            {manifest.slides.map((s) => {
              const active = s.index === current;
              return (
                <button
                  key={s.index}
                  data-slide-thumb={s.index}
                  role="tab"
                  aria-selected={active}
                  aria-label={`Go to ${pageWord} ${s.index}`}
                  onClick={() => goTo(s.index)}
                  className={`relative shrink-0 w-28 aspect-video rounded-md overflow-hidden border transition-all ${
                    active
                      ? "border-primary ring-2 ring-primary/30"
                      : "border-border/60 opacity-60 hover:opacity-100 hover:border-muted-foreground/40"
                  }`}
                >
                  <img
                    src={s.thumb_url}
                    alt=""
                    loading="lazy"
                    draggable={false}
                    className="w-full h-full object-cover"
                  />
                  {(s.notes ?? "").trim().length > 0 && (
                    <span
                      className="absolute top-0.5 left-0.5 p-0.5 rounded bg-black/70 text-warning"
                      title="Has speaker notes"
                      aria-hidden="true"
                    >
                      <StickyNote className="w-2.5 h-2.5" />
                    </span>
                  )}
                  <span className="absolute bottom-0.5 right-0.5 px-1 rounded bg-black/70 text-[10.5px] font-medium tabular-nums text-white">
                    {s.index}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
