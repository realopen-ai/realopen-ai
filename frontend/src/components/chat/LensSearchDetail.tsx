import { useLocation } from "react-router-dom";
import { lensClient, lensPageKind } from "@/api/lensClient";
import type { ToolCallResult } from "@/store/chatStore";
import { useUIStore } from "@/store/uiStore";
import { sideTabRouteKey } from "@/lib/rightPanelTabs";
import { useT } from "@/store/settingsStore";

export function LensSearchDetail({ call }: { call: ToolCallResult }) {
  const routeKey = sideTabRouteKey(useLocation());
  const openSideTab = useUIStore((state) => state.openSideTab);
  const t = useT();

  if (!call.imageResults?.length) {
    return (
      <p className="text-xs text-muted-foreground">
        {call.output ||
          t(call.status === "running" ? "tool.lens.running" : "lens.noMatches")}
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {call.imageResults.map((image) => (
        <li key={image.id}>
          <button
            type="button"
            title={image.filename}
            onClick={() =>
              openSideTab(
                { kind: "lens-image", id: image.id, title: image.filename },
                routeKey,
              )
            }
            className="flex w-full items-center gap-3 rounded-lg border border-border bg-background/50 p-2 text-start transition-colors hover:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <img
              src={lensClient.thumbnail(image.id)}
              alt=""
              loading="lazy"
              className="h-14 w-20 shrink-0 rounded-md bg-secondary object-contain"
            />
            <span className="min-w-0">
              <span className="block truncate text-xs font-medium">
                {image.filename}
              </span>
              {image.page_number && (
                <span className="block text-xs text-muted-foreground">
                  {t(`lens.source.${lensPageKind(image.filename)}`, {
                    number: image.page_number,
                  })}
                </span>
              )}
              {image.excerpt && (
                <span
                  dir="auto"
                  className="mt-1 line-clamp-2 break-words text-xs text-muted-foreground"
                >
                  {image.excerpt}
                </span>
              )}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
