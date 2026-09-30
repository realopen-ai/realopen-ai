import { Lightbulb, Code2, Search, Terminal } from "lucide-react";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";

export function WelcomeScreen({
  onSend,
}: {
  onSend: (message: string) => void;
}) {
  const profileLabel = useChatStore((s) => s.profileLabel);
  const t = useT();

  const suggestions = [
    {
      icon: Lightbulb,
      title: t("welcome.explain"),
      subtitle: t("welcome.explainSub"),
      prompt: "Explain how transformers work in simple terms",
    },
    {
      icon: Code2,
      title: t("welcome.writeCode"),
      subtitle: t("welcome.writeCodeSub"),
      prompt:
        "Write Python code to analyze a CSV dataset with pandas and generate a summary report",
    },
    {
      icon: Search,
      title: t("welcome.searchWeb"),
      subtitle: t("welcome.searchWebSub"),
      prompt: "Search the web for the latest breakthroughs in AI research 2026",
    },
    {
      icon: Terminal,
      title: t("welcome.runCode"),
      subtitle: t("welcome.runCodeSub"),
      prompt:
        "Write and run Python code to calculate fibonacci numbers efficiently",
    },
  ];

  return (
    <div className="flex-1 flex items-center justify-center px-6 py-8 overflow-y-auto">
      {/* Bias the block slightly above true center so it sits in the optical
          middle of the space between the top edge and the composer. */}
      <div className="w-full max-w-xl -translate-y-8 space-y-9">
        <div className="text-center space-y-3">
          <h2 className="text-[28px] font-semibold text-foreground tracking-[-0.01em] leading-tight">
            {t("welcome.title")}
          </h2>
          <p className="text-[16px] text-muted-foreground leading-relaxed">
            {t("welcome.heading")}
          </p>
          {profileLabel && (
            <p className="flex items-center justify-center gap-1.5 text-xs text-muted-foreground/80">
              <span
                className="h-1.5 w-1.5 rounded-full bg-success"
                aria-hidden="true"
              />
              {profileLabel}
            </p>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {suggestions.map((s) => (
            <button
              key={s.title}
              onClick={() => onSend(s.prompt)}
              className="group flex items-center gap-3 rounded-xl bg-secondary/60 px-4 py-3.5 text-left transition-colors hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            >
              <s.icon className="h-4.5 w-4.5 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground" />
              <span className="min-w-0">
                <span className="block text-[13.5px] font-medium text-foreground leading-snug">
                  {s.title}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {s.subtitle}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
