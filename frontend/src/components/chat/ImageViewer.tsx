import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  Loader2,
  Maximize2,
  MessageSquare,
  MoreHorizontal,
  RotateCw,
  ScanText,
  Search,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { lensClient, lensViewerImage } from "@/api/lensClient";
import {
  useImageViewerStore,
  type ViewerImage,
} from "@/store/imageViewerStore";
import { useUIStore } from "@/store/uiStore";
import { sideTabRouteKey } from "@/lib/rightPanelTabs";
import { useT } from "@/store/settingsStore";

const controlStyle =
  "inline-flex size-9 shrink-0 items-center justify-center rounded-md text-zinc-100 hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70 disabled:opacity-35";

async function imageFile(
  image: ViewerImage,
  signal: AbortSignal,
): Promise<File> {
  if (image.file) return image.file;
  const response = await fetch(image.downloadSrc ?? image.src, { signal });
  if (!response.ok) throw new Error("image-unavailable");
  const blob = await response.blob();
  if (!blob.type.startsWith("image/")) throw new Error("image-unavailable");
  const extension = blob.type.split("/")[1].replace("jpeg", "jpg");
  const filename = image.lensId
    ? image.filename.replace(/\.[^.]+$/, "") + "." + extension
    : image.filename;
  return new File([blob], filename, {
    type: blob.type,
  });
}

export function ImageViewer() {
  const t = useT();
  const location = useLocation();
  const navigate = useNavigate();
  const { images, selected, close, select, returnFocus, attach } =
    useImageViewerStore();
  const openSideTab = useUIStore((s) => s.openSideTab);
  const current = selected === null ? undefined : images[selected];
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [action, setAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ocrText, setOcrText] = useState<string | null>(null);
  const [showText, setShowText] = useState(false);
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [dimensions, setDimensions] = useState({ width: 1, height: 1 });
  const operation = useRef<AbortController | null>(null);
  const composerFocus = useRef(false);
  const viewport = useRef<HTMLDivElement>(null);
  const [viewportSize, setViewportSize] = useState({ width: 800, height: 600 });

  useEffect(() => {
    operation.current?.abort();
    operation.current = null;
    setZoom(1);
    setRotation(0);
    setAction(null);
    setError(null);
    setOcrText(current?.ocrText ?? null);
    setSourceUrl(current?.sourceUrl ?? null);
    setShowText(false);
    setCopied(false);
    setDimensions({ width: 1, height: 1 });
    return () => operation.current?.abort();
  }, [current?.src]);

  useEffect(() => {
    if (!current?.lensId) return;
    const controller = new AbortController();
    void lensClient
      .detail(current.lensId, controller.signal)
      .then((image) => {
        if (controller.signal.aborted) return;
        setOcrText(image.ocr_text ?? null);
        setSourceUrl(lensViewerImage(image).sourceUrl ?? null);
      })
      .catch(() => {
        // The image can still be viewed and processed if its index entry is missing.
      });
    return () => controller.abort();
  }, [current?.src, current?.lensId]);

  useEffect(() => {
    const target = viewport.current;
    if (!target) return;
    const observer = new ResizeObserver(([entry]) =>
      setViewportSize({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      }),
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [current?.src]);

  async function run(kind: "similar" | "ask" | "text") {
    if (!current || action) return;
    const controller = new AbortController();
    operation.current = controller;
    setAction(kind);
    setError(null);
    try {
      const file = await imageFile(current, controller.signal);
      if (controller.signal.aborted) return;
      if (kind === "similar") {
        const result = await lensClient.similar(file, controller.signal);
        if (controller.signal.aborted) return;
        openSideTab(
          {
            kind: "lens-results",
            id: crypto.randomUUID(),
            title: t("chat.image.similar"),
            images: result.items,
          },
          sideTabRouteKey(location),
        );
        close();
      } else if (kind === "ask") {
        composerFocus.current = true;
        useUIStore.getState().setMobileTab("chat");
        const conversationId =
          location.pathname.match(/^\/([0-9a-f]{8}-[0-9a-f-]{27})$/i)?.[1] ??
          null;
        attach(file, conversationId);
        if (!conversationId && location.pathname !== "/") navigate("/");
      } else {
        const result = await lensClient.extractText(file, controller.signal);
        if (controller.signal.aborted) return;
        setOcrText(result.text);
        setShowText(true);
      }
    } catch (reason) {
      if (!controller.signal.aborted)
        setError(
          reason instanceof Error &&
            reason.message !== "image-unavailable" &&
            reason.message
            ? reason.message
            : t("chat.image.actionError"),
        );
    } finally {
      if (operation.current === controller) setAction(null);
    }
  }

  function move(direction: number) {
    if (selected !== null && images[selected + direction])
      select(selected + direction);
  }

  const swapped = rotation % 180 !== 0;
  const width = swapped ? dimensions.height : dimensions.width;
  const height = swapped ? dimensions.width : dimensions.height;
  const fit = Math.min(
    viewportSize.width / width,
    viewportSize.height / height,
  );
  const renderWidth = Math.max(1, width * fit * zoom);
  const renderHeight = Math.max(1, height * fit * zoom);

  return (
    <Dialog open={!!current} onOpenChange={(open) => !open && close()}>
      {current && selected !== null && (
        <DialogContent
          className="max-w-5xl overflow-hidden border-white/10 bg-zinc-950 p-0 text-zinc-100"
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (!composerFocus.current) returnFocus?.focus();
            composerFocus.current = false;
          }}
          onKeyDown={(event) => {
            if (event.target instanceof HTMLTextAreaElement) return;
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
              event.preventDefault();
              move(event.key === "ArrowLeft" ? -1 : 1);
            }
          }}
        >
          <div className="flex items-center gap-2 border-b border-white/10 px-3 py-2 pe-12">
            <DialogTitle className="min-w-0 flex-1 truncate text-sm font-medium">
              {current.filename}
            </DialogTitle>
            <button
              type="button"
              className={controlStyle}
              title={t("chat.image.similar")}
              aria-label={t("chat.image.similar")}
              disabled={!!action}
              onClick={() => void run("similar")}
            >
              {action === "similar" ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Search className="size-4" />
              )}
            </button>
            <button
              type="button"
              className={controlStyle}
              title={t("chat.image.ask")}
              aria-label={t("chat.image.ask")}
              disabled={!!action}
              onClick={() => void run("ask")}
            >
              {action === "ask" ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <MessageSquare className="size-4" />
              )}
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className={controlStyle}
                  title={t("chat.image.more")}
                  aria-label={t("chat.image.more")}
                >
                  <MoreHorizontal className="size-4" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  disabled={!!action}
                  onSelect={() => {
                    if (ocrText !== null) setShowText(!showText);
                    else void run("text");
                  }}
                >
                  <ScanText />
                  {t("chat.image.extractText")}
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={() => setRotation((value) => (value + 90) % 360)}
                >
                  <RotateCw />
                  {t("chat.image.rotate")}
                </DropdownMenuItem>
                <DropdownMenuItem asChild>
                  <a
                    href={current.downloadSrc ?? current.src}
                    download={current.filename}
                  >
                    <Download />
                    {t("chat.image.download")}
                  </a>
                </DropdownMenuItem>
                {sourceUrl && (
                  <DropdownMenuItem
                    onSelect={() => {
                      close();
                      navigate(sourceUrl);
                    }}
                  >
                    <ExternalLink />
                    {t("chat.image.source")}
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          {(error || action === "text") && (
            <p role={error ? "alert" : "status"} className="px-4 py-2 text-sm">
              {error || t("chat.image.extracting")}
            </p>
          )}
          <div className="flex max-h-[75dvh] flex-col">
            <div
              ref={viewport}
              key={current.src}
              className="h-[min(60dvh,42rem)] min-h-32 shrink-0 overflow-auto overscroll-contain bg-black/30"
            >
              <div
                className="flex min-h-full min-w-full items-center justify-center"
                style={{ width: renderWidth, height: renderHeight }}
              >
                <div
                  className="relative shrink-0"
                  style={{ width: renderWidth, height: renderHeight }}
                >
                  <img
                    src={current.src}
                    alt={current.filename}
                    draggable={false}
                    onLoad={(event) =>
                      setDimensions({
                        width: event.currentTarget.naturalWidth,
                        height: event.currentTarget.naturalHeight,
                      })
                    }
                    onError={() => setError(t("lens.imageUnavailable"))}
                    className="absolute left-1/2 top-1/2 max-w-none"
                    style={{
                      width: dimensions.width * fit * zoom,
                      height: dimensions.height * fit * zoom,
                      transform: `translate(-50%, -50%) rotate(${rotation}deg)`,
                    }}
                  />
                </div>
              </div>
            </div>
            {showText && (
              <div className="min-h-24 overflow-y-auto border-t border-white/10 p-4">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-sm">{t("chat.image.extractText")}</span>
                  <button
                    type="button"
                    className={controlStyle}
                    disabled={!ocrText}
                    title={t("chat.image.copyText")}
                    aria-label={t("chat.image.copyText")}
                    onClick={() =>
                      void navigator.clipboard
                        .writeText(ocrText ?? "")
                        .then(() => setCopied(true))
                        .catch(() => setError(t("chat.image.actionError")))
                    }
                  >
                    <Copy className="size-4" />
                  </button>
                </div>
                {copied && (
                  <p role="status" className="mb-2 text-xs">
                    {t("chat.image.copied")}
                  </p>
                )}
                <p
                  dir="auto"
                  className="whitespace-pre-wrap break-words text-sm leading-relaxed"
                >
                  {ocrText || t("chat.image.noText")}
                </p>
              </div>
            )}
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-white/10 px-3 py-1">
            <div className="flex items-center">
              <button
                type="button"
                className={controlStyle}
                disabled={selected === 0}
                title={t("chat.image.previous")}
                aria-label={t("chat.image.previous")}
                onClick={() => move(-1)}
              >
                <ChevronLeft className="size-4" />
              </button>
              <span className="text-xs tabular-nums">
                {selected + 1} / {images.length}
              </span>
              <button
                type="button"
                className={controlStyle}
                disabled={selected === images.length - 1}
                title={t("chat.image.next")}
                aria-label={t("chat.image.next")}
                onClick={() => move(1)}
              >
                <ChevronRight className="size-4" />
              </button>
            </div>
            <div className="flex items-center">
              <button
                type="button"
                className={controlStyle}
                disabled={zoom <= 1}
                title={t("chat.image.zoomOut")}
                aria-label={t("chat.image.zoomOut")}
                onClick={() => setZoom(Math.max(1, zoom - 0.5))}
              >
                <ZoomOut className="size-4" />
              </button>
              <button
                type="button"
                className={controlStyle}
                title={t("chat.image.resetZoom")}
                aria-label={t("chat.image.resetZoom")}
                onClick={() => {
                  setZoom(1);
                  setRotation(0);
                }}
              >
                <Maximize2 className="size-4" />
              </button>
              <button
                type="button"
                className={controlStyle}
                disabled={zoom >= 3}
                title={t("chat.image.zoomIn")}
                aria-label={t("chat.image.zoomIn")}
                onClick={() => setZoom(Math.min(3, zoom + 0.5))}
              >
                <ZoomIn className="size-4" />
              </button>
            </div>
          </div>
        </DialogContent>
      )}
    </Dialog>
  );
}
