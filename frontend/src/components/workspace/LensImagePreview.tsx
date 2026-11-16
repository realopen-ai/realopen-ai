import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { lensClient, lensPageKind, type LensImage } from "@/api/lensClient";
import { sourceLocationUrl } from "@/lib/sourceNavigation";
import { useT } from "@/store/settingsStore";

export function LensImagePreview({ id }: { id: string }) {
  const t = useT();
  const [image, setImage] = useState<LensImage | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setImage(null);
    setError(false);
    void lensClient
      .detail(id, controller.signal)
      .then(setImage)
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [id]);

  if (error)
    return (
      <p role="alert" className="p-4 text-sm text-danger">
        {t("lens.imageUnavailable")}
      </p>
    );
  if (!image)
    return (
      <Loader2
        aria-label={t("lens.loading")}
        className="m-4 size-5 animate-spin"
      />
    );
  const documentUrl = sourceLocationUrl({
    documentId: image.document_id,
    page: image.page_number,
    chunk: image.document_chunk_id,
  });
  return (
    <section className="h-full space-y-4 overflow-y-auto p-4">
      <header className="space-y-1">
        <h2 className="break-words text-base font-medium">{image.filename}</h2>
        {image.conversation_id && (
          <Link
            to={`/${image.conversation_id}`}
            className="text-xs text-muted-foreground underline underline-offset-4"
          >
            {t("lens.sourceChat")}
          </Link>
        )}
        {documentUrl && (image.document_chunk_id || !image.conversation_id) && (
          <Link
            to={documentUrl}
            className="block text-xs text-muted-foreground underline underline-offset-4"
          >
            {image.page_number
              ? t(`lens.source.${lensPageKind(image.filename)}`, {
                  number: image.page_number,
                })
              : t("learn.viewSource")}
          </Link>
        )}
      </header>
      <img
        src={lensClient.preview(image.id)}
        onError={() => setError(true)}
        alt={image.filename}
        className="max-h-[65vh] w-full rounded-lg border border-border bg-background object-contain"
      />
      {image.ocr_text && (
        <div className="space-y-2">
          <h3 className="text-sm font-medium">{t("artifacts.ocr")}</h3>
          <p
            dir="auto"
            className="whitespace-pre-wrap break-words rounded-lg bg-secondary p-3 text-sm leading-relaxed text-muted-foreground"
          >
            {image.ocr_text}
          </p>
        </div>
      )}
    </section>
  );
}
