import { Bot, Lightbulb, Code2, Search, Terminal } from "lucide-react";
import { useChatStore } from "@/store/chatStore";
import { useT } from "@/store/settingsStore";

export function WelcomeScreen({
  onSend,
}: {
  onSend: (message: string) => void;
}) {
  const { profileName, models } = useChatStore();
  const t = useT();

  const suggestions = [
    {
      icon: Lightbulb,
      title: t("welcome.explain"),
      subtitle: t("welcome.explainSub"),
      prompt: "Explain how transformers work in simple terms",
      color: "text-amber-400",
      bg: "bg-amber-500/10",
    },
    {
      icon: Code2,
      title: t("welcome.writeCode"),
      subtitle: t("welcome.writeCodeSub"),
      prompt:
        "Write Python code to analyze a CSV dataset with pandas and generate a summary report",
      color: "text-emerald-400",
      bg: "bg-emerald-500/10",
    },
    {
      icon: Search,
      title: t("welcome.searchWeb"),
      subtitle: t("welcome.searchWebSub"),
      prompt: "Search the web for the latest breakthroughs in AI research 2025",
      color: "text-blue-400",
      bg: "bg-blue-500/10",
    },
    {
      icon: Terminal,
      title: t("welcome.runCode"),
      subtitle: t("welcome.runCodeSub"),
      prompt:
        "Write and run Python code to calculate fibonacci numbers efficiently",
      color: "text-purple-400",
      bg: "bg-purple-500/10",
    },
  ];

  const modelTags = models.slice(0, 6).map((m) => ({
    name: m.description || m.id,
    type: m.type,
  }));

  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <div className="text-center space-y-6 max-w-md w-full">
        <div className="space-y-3">
          <div className="w-14 h-14 rounded-2xl bg-linear-to-br from-indigo-500 to-blue-600 flex items-center justify-center mx-auto shadow-lg shadow-indigo-500/20">
            <Bot className="w-7 h-7 text-white" />
          </div>
          <h2 className="text-[28px] font-semibold text-foreground tracking-tight">
            {t("welcome.title")}
          </h2>
          <p className="text-[14px] text-muted-foreground leading-relaxed max-w-sm mx-auto">
            {t("welcome.subtitle")}
          </p>
        </div>

        {profileName && (
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-emerald-500/10 text-[13px] text-emerald-400 font-medium">
            <div className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
            {profileName} {t("welcome.profile")}
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 pt-2">
          {suggestions.map((s) => (
            <button
              key={s.title}
              onClick={() => onSend(s.prompt)}
              className="group flex items-start gap-3 rounded-xl border border-border bg-card p-3.5 text-left hover:bg-accent transition-colors"
            >
              <div
                className={`shrink-0 w-8 h-8 rounded-lg ${s.bg} flex items-center justify-center`}
              >
                <s.icon className={`w-4 h-4 ${s.color}`} />
              </div>
              <div className="min-w-0">
                <p className="text-[13px] font-medium text-foreground group-hover:text-primary transition-colors">
                  {s.title}
                </p>
                <p className="text-[12px] text-muted-foreground">
                  {s.subtitle}
                </p>
              </div>
            </button>
          ))}
        </div>

        {modelTags.length > 0 && (
          <div className="pt-2">
            <p className="text-[11px] text-muted-foreground/60 uppercase tracking-wider mb-2">
              {t("welcome.availableModels")}
            </p>
            <div className="flex flex-wrap justify-center gap-1.5">
              {modelTags.map((m, i) => (
                <span
                  key={i}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-secondary text-[11px] text-muted-foreground"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-500" />
                  {m.name}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
