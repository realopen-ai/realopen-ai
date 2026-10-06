import { useId, useState, type ReactNode } from "react";
import { ChevronDown, Layers } from "lucide-react";
import { useT } from "@/store/settingsStore";

/** Answers are not mounted until the user deliberately expands the deck. */
export function DeckCardStack({
  count,
  children,
}: {
  count: number;
  children: ReactNode;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  if (!count) return null;
  return (
    <section>
      <div
        className={`relative mx-3 mb-6 transition-all duration-300 motion-reduce:transition-none ${expanded ? "pt-0" : "pt-3 pb-3"}`}
      >
        {[3, 2, 1].map((layer) => (
          <div
            key={layer}
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-2 top-3 bottom-3 rounded-2xl border border-border bg-card transition-all duration-300 motion-reduce:transition-none"
            style={{
              transform: expanded
                ? "none"
                : `translateY(${layer * 5}px) rotate(${layer % 2 ? -layer : layer}deg)`,
              opacity: expanded ? 0 : 1,
            }}
          />
        ))}
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={() => setExpanded((value) => !value)}
          className={`relative z-10 w-full rounded-2xl border border-border/70 bg-card shadow-sm hover:border-primary/40 hover:bg-accent/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring transition-all duration-300 motion-reduce:transition-none ${expanded ? "px-5 py-4" : "px-6 py-10 sm:py-12"}`}
        >
          <span className="flex items-center justify-center gap-3 font-medium">
            <Layers className="size-5 text-primary" />
            {t(expanded ? "learn.collapseCards" : "learn.expandCards")}
            <ChevronDown
              className={`size-4 text-muted-foreground transition-transform duration-300 motion-reduce:transition-none ${expanded ? "rotate-180" : ""}`}
            />
          </span>
          {!expanded && (
            <span className="mt-2 block text-sm text-muted-foreground">
              {t("learn.hiddenAnswers", { count })}
            </span>
          )}
        </button>
      </div>
      <div id={contentId} hidden={!expanded}>
        {expanded && (
          <div className="space-y-4 flashcard-deck-expand">{children}</div>
        )}
      </div>
    </section>
  );
}
