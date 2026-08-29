import { useState, useEffect } from "react";
import { X, Loader2, AlertCircle, Download } from "lucide-react";

/**
 * Modal for viewing PPTX presentations as PDF (converted by LibreOffice).
 *
 * Uses an <iframe> with the browser's native PDF viewer — no extra
 * dependencies needed. The PDF is generated on-demand by the backend
 * endpoint `/api/reports/{reportId}/pdf` and cached for subsequent views.
 */
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The PDF URL — the backend converts PPTX→PDF on first request, then
  // caches it. We add a cache-busting query param to force a fresh load
  // if the PPTX was regenerated.
  const pdfUrl = `/api/reports/${reportId}/pdf`;

  useEffect(() => {
    // Reset state when reportId changes
    setLoading(true);
    setError(null);
  }, [reportId]);

  // Close on Escape key
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

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

        {/* Body — PDF iframe */}
        <div className="flex-1 relative bg-secondary/30">
          {/* Loading overlay */}
          {loading && !error && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10">
              <Loader2 className="w-6 h-6 text-primary animate-spin" />
              <p className="text-[13px] text-muted-foreground">
                Converting to PDF...
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
              <button
                onClick={() => {
                  setError(null);
                  setLoading(true);
                  // Force iframe reload by changing the key
                  const iframe = document.getElementById(
                    "pptx-viewer-iframe",
                  ) as HTMLIFrameElement | null;
                  if (iframe) iframe.src = `${pdfUrl}?t=${Date.now()}`;
                }}
                className="mt-2 px-3 py-1.5 rounded-lg text-[12px] font-medium bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
              >
                Retry
              </button>
            </div>
          )}

          {/* PDF iframe — hidden while loading, shown on load */}
          {!error && (
            <iframe
              id="pptx-viewer-iframe"
              src={`${pdfUrl}#view=FitH`}
              className="w-full h-full border-0"
              onLoad={() => setLoading(false)}
              onError={() => {
                setError(
                  "Could not load the PDF. LibreOffice may not be available.",
                );
                setLoading(false);
              }}
              title={filename}
            />
          )}
        </div>
      </div>
    </div>
  );
}
