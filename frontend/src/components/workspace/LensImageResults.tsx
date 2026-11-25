import type { LensImage } from "@/api/lensClient";
import { lensClient, lensViewerImage } from "@/api/lensClient";
import { useImageViewerStore } from "@/store/imageViewerStore";
import { useT } from "@/store/settingsStore";

export function LensImageResults({ images }: { images: LensImage[] }) {
  const t = useT();
  const openViewer = useImageViewerStore((s) => s.open);
  if (!images.length)
    return (
      <p className="p-4 text-sm text-muted-foreground">{t("lens.noMatches")}</p>
    );
  return (
    <div className="grid h-full min-h-0 grid-cols-2 content-start gap-3 overflow-y-auto p-4">
      {images.map((image, index) => (
        <button
          key={image.id}
          type="button"
          title={image.filename}
          onClick={(event) =>
            openViewer(images.map(lensViewerImage), index, event.currentTarget)
          }
          className="overflow-hidden rounded-xl border border-border text-start hover:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <img
            src={lensClient.thumbnail(image.id)}
            alt=""
            loading="lazy"
            className="aspect-4/3 w-full bg-secondary object-contain"
          />
          <span className="block truncate p-2 text-xs">{image.filename}</span>
        </button>
      ))}
    </div>
  );
}
