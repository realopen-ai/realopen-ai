import { useEffect, useState } from "react";
import { Info } from "lucide-react";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";

interface ServiceState {
  status: string;
  enabled: boolean;
  managed: boolean;
  error?: string;
}

export function SearchServiceStatus() {
  const t = useT();
  const [states, setStates] = useState<Record<string, ServiceState>>({});
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const response = await fetch("/api/tools/use_websearch/runtime", {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Runtime status unavailable");
        const result = await response.json();
        if (active) {
          setStates(result);
          setUnavailable(false);
        }
      } catch {
        if (active) setUnavailable(true);
      } finally {
        if (active) timer = setTimeout(refresh, 2000);
      }
    };
    void refresh();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, []);

  return (
    <div
      className="mb-4 rounded-xl border border-border/60 bg-card/50 p-4 text-xs"
      aria-live="polite"
    >
      <div className="flex items-start gap-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Info className="h-4 w-4" aria-hidden="true" />
        </span>
        <p className="pt-0.5 text-[12.5px] leading-relaxed text-muted-foreground">
          {t("search.runtime.description")}
        </p>
      </div>
      {unavailable && (
        <p className="mt-3 text-destructive">
          {t("search.runtime.unavailable")}
        </p>
      )}
      {Object.keys(states).length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2 border-t border-border/40 pt-3">
          {Object.entries(states).map(([name, state]) => (
            <div key={name} className="min-w-0">
              <div className="inline-flex items-center gap-2 rounded-full border border-border/50 bg-secondary/50 px-2.5 py-1.5">
                <span
                  aria-hidden="true"
                  className={cn(
                    "h-1.5 w-1.5 shrink-0 rounded-full",
                    state.status === "ready"
                      ? "bg-emerald-500"
                      : state.status === "failed"
                        ? "bg-destructive"
                        : "bg-muted-foreground/50",
                  )}
                />
                <span className="font-medium text-foreground">
                  {name === "searxng" ? "SearXNG" : "Degoog"}
                </span>
                <span
                  className={cn(
                    "text-muted-foreground",
                    state.status === "ready" && "text-emerald-500",
                    state.status === "failed" && "text-destructive",
                  )}
                >
                  {t(`search.runtime.${state.status}`)}
                </span>
              </div>
              {state.status === "failed" && state.error && (
                <p className="mt-2 break-words leading-relaxed text-destructive">
                  {state.error}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
