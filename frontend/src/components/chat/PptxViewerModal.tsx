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
  X,
} from "lucide-react";

/**
 * Modal for viewing PPTX presentations as slide images.
 *
 * The backend renders each slide as a JPEG (via LibreOffice + PyMuPDF,
 * cached server-side) and exposes:
 *   GET /api/reports/{reportId}/slides          → manifest {count, slides[]}
 *   GET /api/reports/{reportId}/slides/{n}      → full-size slide JPEG
 *   GET /api/reports/{reportId}/slides/{n}?variant=thumb → thumbnail
 *
 * This component fetches the manifest on open and presents the deck with
 * prev/next controls, a page indicator, a collapsible thumbnail filmstrip,
 * keyboard navigation (←/→, Home/End, Esc), touch swipe, and adjacent-slide
 * preloading.
 */

type SlideInfo = {
  index: number;
  url: string;
  thumb_url: string;
};

type SlidesManifest = {
  report_id: string;
  count: number;
  width: number;
  height: number;
  slides: SlideInfo[];
};

export function PptxViewerModal({
  reportId,
  filename,
  downloadUrl,
  onClose,
}: {
  reportId: string;
  filename: string;
  downloadUrl: string;
  onClose: () => void;
}) {
  const [manifest, setManifest] = useState<SlidesManifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState(1); // 1-based
  const [slideLoaded, setSlideLoaded] = useState(false);
  const [showThumbs, setShowThumbs] = useState(true);
  const [retryCount, setRetryCount] = useState(0);

  const filmstripRef = useRef<HTMLDivElement>(null);
  const touchStartX = useRef<number | null>(null);

  const count = manifest?.count ?? 0;

  // ── Fetch the slide manifest ────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    setLoading(true);
    setError(null);
    setManifest(null);
    setCurrent(1);

    fetch(`/api/reports/${reportId}/slides`)
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
        setError(e.message || "Failed to load slides");
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [reportId, retryCount]);

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
        className="relative w-full max-w-5xl h-[90vh] mx-4 bg-card border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-border shrink-0">
          <p className="text-[13px] font-medium text-foreground truncate flex-1">
            {filename}
          </p>

          {/* Page indicator */}
          {manifest && count > 0 && (
            <div className="flex items-center gap-1 text-[12px] tabular-nums text-muted-foreground shrink-0">
              <span className="text-foreground font-semibold">{current}</span>
              <span>/</span>
              <span>{count}</span>
            </div>
          )}

          {/* Filmstrip toggle */}
          {manifest && count > 1 && (
            <button
              onClick={() => setShowThumbs((s) => !s)}
              className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors ${
                showThumbs
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent"
              }`}
              title={showThumbs ? "Hide slide overview" : "Show slide overview"}
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
          )}

          {/* Download button */}
          <a
            href={downloadUrl}
            download={filename}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[12px] font-medium text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
            title="Download PPTX"
          >
            <Download className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">Download</span>
          </a>

          {/* Close button */}
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
            title="Close (Esc)"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body — slide stage */}
        <div
          className="flex-1 relative bg-secondary/30 overflow-hidden flex items-center justify-center"
          onTouchStart={onTouchStart}
          onTouchEnd={onTouchEnd}
        >
          {/* Converting overlay (first render of the manifest) */}
          {loading && !error && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10 px-8 text-center">
              <Loader2 className="w-6 h-6 text-primary animate-spin" />
              <p className="text-[13px] text-muted-foreground">
                Rendering slides...
              </p>
              <p className="text-[11px] text-muted-foreground/60">
                This happens once per presentation, then it&apos;s cached.
              </p>
            </div>
          )}

          {/* Error overlay */}
          {error && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10 px-8 text-center">
              <AlertCircle className="w-8 h-8 text-red-400" />
              <p className="text-[13px] text-foreground font-medium">
                Failed to load presentation
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
                aria-label="Previous slide"
                className={`absolute left-2 sm:left-3 z-20 w-10 h-10 rounded-full flex items-center justify-center bg-background/80 border border-border shadow-sm backdrop-blur transition-all ${
                  atFirst
                    ? "opacity-30 cursor-not-allowed"
                    : "hover:bg-background hover:shadow-md active:scale-95"
                }`}
              >
                <ChevronLeft className="w-5 h-5" />
              </button>

              {/* Next button */}
              <button
                onClick={next}
                disabled={atLast}
                aria-label="Next slide"
                className={`absolute right-2 sm:right-3 z-20 w-10 h-10 rounded-full flex items-center justify-center bg-background/80 border border-border shadow-sm backdrop-blur transition-all ${
                  atLast
                    ? "opacity-30 cursor-not-allowed"
                    : "hover:bg-background hover:shadow-md active:scale-95"
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

              {/* The slide */}
              <img
                key={currentSlide.url}
                src={currentSlide.url}
                alt={`Slide ${current} of ${count}`}
                draggable={false}
                onLoad={() => setSlideLoaded(true)}
                onError={() => {
                  // Image missing — offer a retry (re-fetches the manifest).
                  setError(
                    `Slide ${current} failed to load. The cache may be incomplete.`,
                  );
                }}
                className={`max-w-full max-h-full object-contain select-none transition-opacity duration-150 px-12 sm:px-14 ${
                  slideLoaded ? "opacity-100" : "opacity-0"
                }`}
              />

              {/* Mobile page badge (desktop shows it in the header) */}
              <div className="sm:hidden absolute bottom-2 left-1/2 -translate-x-1/2 px-2.5 py-1 rounded-full bg-background/85 border border-border text-[11px] tabular-nums text-muted-foreground backdrop-blur">
                <span className="text-foreground font-semibold">{current}</span>
                <span> / {count}</span>
              </div>
            </>
          )}
        </div>

        {/* Filmstrip — slide overview */}
        {manifest && count > 1 && showThumbs && (
          <div
            ref={filmstripRef}
            className="shrink-0 border-t border-border bg-card overflow-x-auto flex gap-2 px-3 py-2.5"
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
                  aria-label={`Go to slide ${s.index}`}
                  onClick={() => goTo(s.index)}
                  className={`relative shrink-0 w-28 aspect-video rounded-md overflow-hidden border transition-all ${
                    active
                      ? "border-primary ring-2 ring-primary/30"
                      : "border-border opacity-60 hover:opacity-100 hover:border-muted-foreground/40"
                  }`}
                >
                  <img
                    src={s.thumb_url}
                    alt=""
                    loading="lazy"
                    draggable={false}
                    className="w-full h-full object-cover"
                  />
                  <span className="absolute bottom-0.5 right-0.5 px-1 rounded bg-black/70 text-[9px] font-medium tabular-nums text-white">
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
