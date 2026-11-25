import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import type { MessageDTO } from "@/api/client";
import {
  useImageViewerStore,
  type ViewerImage,
} from "@/store/imageViewerStore";
import { useT } from "@/store/settingsStore";

function PreviewImage({
  image,
  thumbnail = false,
}: {
  image: ViewerImage;
  thumbnail?: boolean;
}) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  return failed ? (
    <span className="flex h-full w-full flex-col items-center justify-center gap-2 p-2 text-center text-xs opacity-70">
      <ImageOff className="size-5" aria-hidden />
      {t("lens.imageUnavailable")}
    </span>
  ) : (
    <img
      src={image.src}
      alt={image.filename}
      className="h-full w-full object-contain"
      loading={thumbnail ? "lazy" : undefined}
      draggable={false}
      onError={() => setFailed(true)}
    />
  );
}

export function MessageImages({
  attachments,
  files,
  conversationId,
}: {
  attachments?: MessageDTO["imageAttachments"];
  files?: File[];
  conversationId?: string;
}) {
  const t = useT();
  const [localImages, setLocalImages] = useState<ViewerImage[]>([]);
  const openViewer = useImageViewerStore((s) => s.open);

  useEffect(() => {
    if (!files?.length) return;
    const images = files.map((file) => ({
      src: URL.createObjectURL(file),
      filename: file.name,
      file,
      sourceUrl: conversationId ? `/${conversationId}` : undefined,
    }));
    setLocalImages(images);
    return () => images.forEach(({ src }) => URL.revokeObjectURL(src));
  }, [files, conversationId]);

  const images = attachments?.length
    ? attachments.map(({ id, filename }) => ({
        src: `/api/documents/${encodeURIComponent(id)}/download`,
        filename,
        sourceUrl: conversationId ? `/${conversationId}` : undefined,
      }))
    : localImages;
  if (!images.length) {
    return files?.length ? null : (
      <p className="mt-2 text-end text-xs text-muted-foreground">
        {t("lens.imageUnavailable")}
      </p>
    );
  }

  return (
    <div className="mt-2 flex flex-wrap justify-end gap-2">
      {images.map((image, index) => (
        <button
          key={image.src}
          type="button"
          aria-label={t("chat.image.open", { filename: image.filename })}
          title={image.filename}
          className="aspect-4/3 w-32 max-w-full cursor-zoom-in overflow-hidden rounded-xl border border-border bg-secondary/40 hover:border-foreground/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:w-40"
          onClick={(event) => {
            openViewer(images, index, event.currentTarget);
          }}
        >
          <PreviewImage image={image} thumbnail />
        </button>
      ))}
    </div>
  );
}
