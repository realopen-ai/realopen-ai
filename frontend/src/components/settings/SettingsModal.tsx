import { useState, useCallback } from "react";
import {
  X,
  Sun,
  Moon,
  Monitor,
  Palette,
  Languages,
  Bell,
  BellOff,
  Eye,
  Check,
  Type,
  Bot,
  Image,
  Download,
  Loader2,
  AlertTriangle,
  Puzzle,
  SlidersHorizontal,
  Sparkles,
  Package,
  Mic2,
} from "lucide-react";
import {
  useSettingsStore,
  accentColorMap,
  fontSizeOptions,
  type Appearance,
  type Contrast,
  type AccentColor,
  type Language,
  type FontSize,
} from "@/store/settingsStore";
import { useChatStore } from "@/store/chatStore";
import { installModuleModels } from "@/api/client";
import type { ModuleInfo } from "@/api/client";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import { DependenciesTab } from "@/components/settings/DependenciesTab";
import { AiTab } from "@/components/settings/AiTab";
import { VoiceTab } from "@/components/settings/VoiceTab";

type TabKey = "general" | "ai" | "voice" | "modules" | "dependencies" | "notifications";

// ─── Radio Option ────────────────────────────────────────────────

function RadioOption<T extends string>({
  value,
  current,
  onChange,
  label,
  icon,
}: {
  value: T;
  current: T;
  onChange: (v: T) => void;
  label: string;
  icon?: React.ReactNode;
}) {
  const isActive = value === current;
  return (
    <button
      onClick={() => onChange(value)}
      className={cn(
        "flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-[13px] transition-all w-full",
        isActive
          ? "bg-primary/10 text-primary font-medium ring-1 ring-primary/20"
          : "text-muted-foreground hover:bg-accent hover:text-foreground",
      )}
    >
      <div
        className={cn(
          "w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0 transition-colors",
          isActive ? "border-primary" : "border-muted-foreground/30",
        )}
      >
        {isActive && <div className="w-1.5 h-1.5 rounded-full bg-primary" />}
      </div>
      {icon && <span className="shrink-0">{icon}</span>}
      <span>{label}</span>
    </button>
  );
}

// ─── Accent Color Swatch ─────────────────────────────────────────

function AccentSwatch({
  color,
  current,
  onChange,
}: {
  color: AccentColor;
  current: AccentColor;
  onChange: (v: AccentColor) => void;
}) {
  const isActive = color === current;
  const css = accentColorMap[color];
  return (
    <button
      onClick={() => onChange(color)}
      className={cn(
        "group relative w-9 h-9 rounded-full flex items-center justify-center transition-all",
        isActive && "ring-2 ring-offset-2 ring-offset-background",
      )}
      style={{
        backgroundColor: css.primary,
        ["--tw-ring-color" as string]: css.primary,
      }}
      title={color.charAt(0).toUpperCase() + color.slice(1)}
    >
      {isActive && (
        <Check className="w-4 h-4" style={{ color: css.primaryForeground }} />
      )}
    </button>
  );
}

// ─── Font Size Option ────────────────────────────────────────────

function FontSizeOption({
  size,
  current,
  onChange,
  label,
}: {
  size: FontSize;
  current: FontSize;
  onChange: (v: FontSize) => void;
  label: string;
}) {
  const isActive = size === current;
  return (
    <button
      onClick={() => onChange(size)}
      className={cn(
        "flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-[13px] transition-all flex-1",
        isActive
          ? "bg-primary/10 text-primary font-medium ring-1 ring-primary/20"
          : "text-muted-foreground hover:bg-accent hover:text-foreground border border-border/50",
      )}
    >
      <span className="leading-none" style={{ fontSize: size }}>
        Aa
      </span>
      <span>{label}</span>
    </button>
  );
}

// ─── Module Icon Mapper ───────────────────────────────────────────

function ModuleIcon({ icon, className }: { icon: string; className?: string }) {
  switch (icon) {
    case "bot":
      return <Bot className={className} />;
    case "image":
      return <Image className={className} />;
    default:
      return <Puzzle className={className} />;
  }
}

// ─── Module Card ──────────────────────────────────────────────────

function ModuleCard({
  module,
  onToggle,
  onInstall,
  installing,
  installProgress,
}: {
  module: ModuleInfo;
  onToggle: (name: string, enabled: boolean) => void;
  onInstall: (name: string) => void;
  installing: boolean;
  installProgress: Record<string, { percent: number; status: string }>;
}) {
  const t = useT();
  const isRequired = module.required;
  const isAvailable = module.available;
  const requirementsMet = module.requirements_met;
  const isDownloading = installing;
  const progress = installProgress[module.name];

  // Can't toggle if: required, not available for profile, or requirements not met
  const canToggle = module.can_toggle && isAvailable && requirementsMet;

  return (
    <div
      className={cn(
        "rounded-xl border p-4 transition-all",
        !isAvailable
          ? "border-border/30 bg-card/50 opacity-50"
          : module.enabled
            ? "border-primary/20 bg-primary/5"
            : "border-border bg-card",
      )}
    >
      <div className="flex items-start gap-3">
        {/* Icon */}
        <div
          className={cn(
            "shrink-0 w-10 h-10 rounded-lg flex items-center justify-center",
            module.enabled
              ? "bg-primary/10 text-primary"
              : "bg-secondary text-muted-foreground",
          )}
        >
          <ModuleIcon icon={module.icon} className="w-5 h-5" />
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-[14px] font-medium text-foreground">
                {module.label}
              </span>
              {isRequired && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-primary/10 text-primary font-medium">
                  {t("modules.required")}
                </span>
              )}
            </div>

            {/* Toggle */}
            {canToggle && (
              <button
                onClick={() => onToggle(module.name, !module.enabled)}
                disabled={isDownloading}
                className={cn(
                  "relative w-10 h-5.5 rounded-full transition-colors shrink-0",
                  module.enabled ? "bg-primary" : "bg-secondary",
                  isDownloading && "opacity-50 cursor-not-allowed",
                )}
              >
                <div
                  className={cn(
                    "absolute top-0.5 w-4.5 h-4.5 rounded-full bg-white transition-transform shadow-sm",
                    module.enabled ? "translate-x-5" : "translate-x-0.5",
                  )}
                />
              </button>
            )}
            {isRequired && (
              <div className="flex items-center gap-1 text-emerald-500">
                <Check className="w-4 h-4" />
              </div>
            )}
          </div>

          <p className="text-[12px] text-muted-foreground mt-1 leading-relaxed">
            {module.description}
          </p>

          {/* Status indicators */}
          <div className="flex items-center gap-3 mt-2.5 flex-wrap">
            {!isAvailable && (
              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground/60">
                <AlertTriangle className="w-3 h-3" />
                {t("modules.notAvailableForProfile")}
              </div>
            )}
            {isAvailable && !requirementsMet && (
              <div className="flex items-center gap-1.5 text-[11px] text-amber-500">
                <AlertTriangle className="w-3 h-3" />
                {t("modules.requirementsNotMet")}
                {module.minimum_requirements && (
                  <span>
                    ({module.minimum_requirements.ram} GB RAM,{" "}
                    {module.minimum_requirements.vram} GB VRAM)
                  </span>
                )}
              </div>
            )}
            {module.estimated_size && isAvailable && (
              <span className="text-[11px] text-muted-foreground/60">
                {t("modules.estimatedSize")}: {module.estimated_size}
              </span>
            )}
            {module.enabled && !module.models_downloaded && isAvailable && (
              <span className="text-[11px] text-amber-500">
                {t("modules.modelsNotDownloaded")}
              </span>
            )}
            {module.enabled && module.models_downloaded && (
              <span className="text-[11px] text-emerald-500">
                {t("modules.ready")}
              </span>
            )}
          </div>

          {/* Install button + progress */}
          {module.enabled && !module.models_downloaded && isAvailable && (
            <div className="mt-3">
              {isDownloading && progress ? (
                <div className="space-y-1.5">
                  <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    {progress.status}
                  </div>
                  <div className="w-full h-1.5 bg-secondary rounded-full overflow-hidden">
                    <div
                      className="h-full bg-primary rounded-full transition-all duration-300"
                      style={{ width: `${progress.percent}%` }}
                    />
                  </div>
                </div>
              ) : (
                <button
                  onClick={() => onInstall(module.name)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
                >
                  <Download className="w-3.5 h-3.5" />
                  {t("modules.downloadModels")}
                </button>
              )}
            </div>
          )}

          {/* Models list for optional modules */}
          {module.models && module.models.length > 0 && isAvailable && (
            <div className="mt-2.5">
              <p className="text-[10px] text-muted-foreground/50 uppercase tracking-wider mb-1">
                {t("modules.models")}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {module.models.map((m, i) => (
                  <span
                    key={i}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-secondary text-[10px] text-muted-foreground"
                  >
                    <span
                      className={cn(
                        "w-1.5 h-1.5 rounded-full",
                        module.models_downloaded
                          ? "bg-emerald-500"
                          : "bg-amber-500",
                      )}
                    />
                    {m.description || m.id}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Settings Modal ──────────────────────────────────────────────

export function SettingsModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<TabKey>("general");
  const t = useT();

  const appearance = useSettingsStore((s) => s.appearance);
  const contrast = useSettingsStore((s) => s.contrast);
  const accentColor = useSettingsStore((s) => s.accentColor);
  const language = useSettingsStore((s) => s.language);
  const fontSize = useSettingsStore((s) => s.fontSize);
  const notifyDeepSearch = useSettingsStore((s) => s.notifyDeepSearch);

  const setAppearance = useSettingsStore((s) => s.setAppearance);
  const setContrast = useSettingsStore((s) => s.setContrast);
  const setAccentColor = useSettingsStore((s) => s.setAccentColor);
  const setLanguage = useSettingsStore((s) => s.setLanguage);
  const setFontSize = useSettingsStore((s) => s.setFontSize);
  const setNotifyDeepSearch = useSettingsStore((s) => s.setNotifyDeepSearch);

  const modules = useChatStore((s) => s.modules);
  const toggleModule = useChatStore((s) => s.toggleModule);
  const loadModules = useChatStore((s) => s.loadModules);

  // Module install state
  const [installingModule, setInstallingModule] = useState<string | null>(null);
  const [installProgress, setInstallProgress] = useState<
    Record<string, { percent: number; status: string }>
  >({});

  const handleToggleModule = useCallback(
    async (name: string, enabled: boolean) => {
      await toggleModule(name, enabled);
    },
    [toggleModule],
  );

  const handleInstallModule = useCallback(
    async (moduleName: string) => {
      setInstallingModule(moduleName);
      setInstallProgress((prev) => ({
        ...prev,
        [moduleName]: { percent: 0, status: t("modules.startingDownload") },
      }));

      await installModuleModels(moduleName, (event) => {
        const evt = event.event as string;

        if (evt === "pull_start") {
          setInstallProgress((prev) => ({
            ...prev,
            [moduleName]: {
              percent: 0,
              status: `${t("modules.pullingModel")}: ${event.model}`,
            },
          }));
        } else if (evt === "pull_progress") {
          setInstallProgress((prev) => ({
            ...prev,
            [moduleName]: {
              percent: event.percent as number,
              status: `${t("modules.pullingModel")}: ${event.model}`,
            },
          }));
        } else if (evt === "pull_done") {
          setInstallProgress((prev) => ({
            ...prev,
            [moduleName]: {
              percent: 100,
              status: `${event.model} ✓`,
            },
          }));
        } else if (evt === "pull_error") {
          setInstallProgress((prev) => ({
            ...prev,
            [moduleName]: {
              percent: prev[moduleName]?.percent ?? 0,
              status: `${t("modules.error")}: ${event.error}`,
            },
          }));
        } else if (evt === "install_complete") {
          setInstallingModule(null);
          // Reload modules to update download status
          loadModules();
        }
      });

      setInstallingModule(null);
      // Reload modules after install completes
      await loadModules();
    },
    [t, loadModules],
  );

  if (!open) return null;

  const tabs: { key: TabKey; label: string; icon: React.ReactNode }[] = [
    {
      key: "general",
      label: t("settings.general"),
      icon: <SlidersHorizontal className="w-4 h-4" />,
    },
    {
      key: "ai",
      label: t("settings.ai"),
      icon: <Sparkles className="w-4 h-4" />,
    },
    {
      key: "voice",
      label: "Voice",
      icon: <Mic2 className="w-4 h-4" />,
    },
    {
      key: "modules",
      label: t("settings.modules"),
      icon: <Puzzle className="w-4 h-4" />,
    },
    {
      key: "dependencies",
      label: "Dependencies",
      icon: <Package className="w-4 h-4" />,
    },
    {
      key: "notifications",
      label: t("settings.notifications"),
      icon: <Bell className="w-4 h-4" />,
    },
  ];

  const activeTab = tabs.find((tb) => tb.key === tab);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal — Claude-style two-pane layout: tab rail on the left
          (desktop) / top tab pills (mobile), content pane on the right. */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("settings.title")}
        className="relative w-full max-w-3xl bg-card border border-border rounded-2xl shadow-2xl overflow-hidden animate-fade-in flex flex-col max-h-[88vh]"
      >
        {/* ── Mobile: header + horizontal tab pills ── */}
        <div className="md:hidden">
          <div className="flex items-center justify-between px-4 pt-4 pb-2">
            <h2 className="text-[15px] font-semibold text-foreground">
              {t("settings.title")}
            </h2>
            <button
              onClick={onClose}
              aria-label={t("common.close")}
              className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="flex gap-1.5 px-3 pb-3 overflow-x-auto scrollbar-none">
            {tabs.map((tabItem) => (
              <button
                key={tabItem.key}
                onClick={() => setTab(tabItem.key)}
                className={cn(
                  "shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium transition-colors",
                  tab === tabItem.key
                    ? "bg-primary/10 text-primary ring-1 ring-primary/20"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                {tabItem.icon}
                {tabItem.label}
              </button>
            ))}
          </div>
          <div className="h-px bg-border/50" />
        </div>

        {/* ── Body: sidebar (desktop) + content ── */}
        <div className="flex flex-1 min-h-0">
          {/* Sidebar tab rail */}
          <nav className="hidden md:flex flex-col w-56 shrink-0 border-r border-border/50 p-3 gap-0.5 overflow-y-auto">
            <div className="flex items-center justify-between px-2 pt-1.5 pb-3">
              <h2 className="text-[14px] font-semibold text-foreground">
                {t("settings.title")}
              </h2>
            </div>
            {tabs.map((tabItem) => (
              <button
                key={tabItem.key}
                onClick={() => setTab(tabItem.key)}
                className={cn(
                  "flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[13px] text-left transition-colors",
                  tab === tabItem.key
                    ? "bg-accent text-foreground font-medium"
                    : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
                )}
              >
                <span
                  className={cn(
                    "shrink-0 transition-colors",
                    tab === tabItem.key
                      ? "text-primary"
                      : "text-muted-foreground/70",
                  )}
                >
                  {tabItem.icon}
                </span>
                {tabItem.label}
              </button>
            ))}
            <div className="mt-auto pt-3 px-2">
              <p className="text-[10px] text-muted-foreground/40 leading-relaxed">
                RealOpen-AI
              </p>
            </div>
          </nav>

          {/* Content pane */}
          <div className="flex-1 min-w-0 flex flex-col">
            {/* Desktop pane header */}
            <div className="hidden md:flex items-center justify-between px-6 pt-5 pb-3 shrink-0">
              <h3 className="text-[15px] font-semibold text-foreground">
                {activeTab?.label}
              </h3>
              <button
                onClick={onClose}
                aria-label={t("common.close")}
                className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Scrollable content */}
            <div className="flex-1 min-h-0 overflow-y-auto px-5 md:px-6 pb-6">
              {tab === "general" && (
                <div className="space-y-6">
                  {/* Appearance */}
                  <div className="space-y-2.5">
                    <div className="flex items-center gap-2">
                      <Palette className="w-4 h-4 text-muted-foreground" />
                      <span className="text-[13px] font-medium text-foreground">
                        {t("settings.appearance")}
                      </span>
                    </div>
                    <div className="space-y-1">
                      <RadioOption<Appearance>
                        value="system"
                        current={appearance}
                        onChange={setAppearance}
                        label={t("settings.appearance.system")}
                        icon={<Monitor className="w-4 h-4" />}
                      />
                      <RadioOption<Appearance>
                        value="dark"
                        current={appearance}
                        onChange={setAppearance}
                        label={t("settings.appearance.dark")}
                        icon={<Moon className="w-4 h-4" />}
                      />
                      <RadioOption<Appearance>
                        value="light"
                        current={appearance}
                        onChange={setAppearance}
                        label={t("settings.appearance.light")}
                        icon={<Sun className="w-4 h-4" />}
                      />
                    </div>
                  </div>

                  {/* Contrast */}
                  <div className="space-y-2.5">
                    <div className="flex items-center gap-2">
                      <Eye className="w-4 h-4 text-muted-foreground" />
                      <span className="text-[13px] font-medium text-foreground">
                        {t("settings.contrast")}
                      </span>
                    </div>
                    <div className="space-y-1">
                      <RadioOption<Contrast>
                        value="medium"
                        current={contrast}
                        onChange={setContrast}
                        label={t("settings.contrast.medium")}
                      />
                      <RadioOption<Contrast>
                        value="increased"
                        current={contrast}
                        onChange={setContrast}
                        label={t("settings.contrast.increased")}
                      />
                    </div>
                  </div>

                  {/* Accent Color */}
                  <div className="space-y-2.5">
                    <div className="flex items-center gap-2">
                      <div className="w-4 h-4 rounded-full bg-primary" />
                      <span className="text-[13px] font-medium text-foreground">
                        {t("settings.accentColor")}
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-2.5 px-1">
                      {(Object.keys(accentColorMap) as AccentColor[]).map(
                        (color) => (
                          <AccentSwatch
                            key={color}
                            color={color}
                            current={accentColor}
                            onChange={setAccentColor}
                          />
                        ),
                      )}
                    </div>
                  </div>

                  {/* Font Size */}
                  <div className="space-y-2.5">
                    <div className="flex items-center gap-2">
                      <Type className="w-4 h-4 text-muted-foreground" />
                      <span className="text-[13px] font-medium text-foreground">
                        {t("settings.fontSize")}
                      </span>
                    </div>
                    <div className="flex gap-2">
                      {fontSizeOptions.map((size) => (
                        <FontSizeOption
                          key={size}
                          size={size}
                          current={fontSize}
                          onChange={setFontSize}
                          label={t(`settings.fontSize.${size}`)}
                        />
                      ))}
                    </div>
                  </div>

                  {/* Language */}
                  <div className="space-y-2.5">
                    <div className="flex items-center gap-2">
                      <Languages className="w-4 h-4 text-muted-foreground" />
                      <span className="text-[13px] font-medium text-foreground">
                        {t("settings.language")}
                      </span>
                    </div>
                    <div className="space-y-1">
                      <RadioOption<Language>
                        value="en"
                        current={language}
                        onChange={setLanguage}
                        label={t("settings.language.en")}
                      />
                      <RadioOption<Language>
                        value="fr"
                        current={language}
                        onChange={setLanguage}
                        label={t("settings.language.fr")}
                      />
                    </div>
                  </div>
                </div>
              )}

              {tab === "ai" && <AiTab />}

              {tab === "voice" && <VoiceTab />}

              {tab === "modules" && (
                <div className="space-y-3">
                  <p className="text-[12px] text-muted-foreground leading-relaxed">
                    {t("modules.description")}
                  </p>
                  {modules.map((module) => (
                    <ModuleCard
                      key={module.name}
                      module={module}
                      onToggle={handleToggleModule}
                      onInstall={handleInstallModule}
                      installing={installingModule === module.name}
                      installProgress={installProgress}
                    />
                  ))}
                  {modules.length === 0 && (
                    <div className="text-center py-8">
                      <Puzzle className="w-8 h-8 text-muted-foreground/30 mx-auto mb-2" />
                      <p className="text-[13px] text-muted-foreground/60">
                        {t("modules.noModules")}
                      </p>
                    </div>
                  )}
                </div>
              )}

              {tab === "dependencies" && <DependenciesTab />}

              {tab === "notifications" && (
                <div className="space-y-4">
                  {/* DeepSearch notifications */}
                  <div className="flex items-center justify-between p-4 rounded-xl border border-border bg-card">
                    <div className="flex items-start gap-3">
                      {notifyDeepSearch ? (
                        <Bell className="w-5 h-5 text-primary shrink-0 mt-0.5" />
                      ) : (
                        <BellOff className="w-5 h-5 text-muted-foreground shrink-0 mt-0.5" />
                      )}
                      <div>
                        <p className="text-[13px] font-medium text-foreground">
                          {t("settings.notifyDeepSearch")}
                        </p>
                        <p className="text-[12px] text-muted-foreground mt-0.5">
                          {language === "fr"
                            ? "Recevez une notification lorsqu'une recherche approfondie est terminée"
                            : "Get a notification when a deep search task completes"}
                        </p>
                      </div>
                    </div>
                    {/* Toggle Switch */}
                    <button
                      onClick={() => setNotifyDeepSearch(!notifyDeepSearch)}
                      className={cn(
                        "relative w-10 h-5.5 rounded-full transition-colors shrink-0",
                        notifyDeepSearch ? "bg-primary" : "bg-secondary",
                      )}
                    >
                      <div
                        className={cn(
                          "absolute top-0.5 w-4.5 h-4.5 rounded-full bg-white transition-transform shadow-sm",
                          notifyDeepSearch
                            ? "translate-x-5"
                            : "translate-x-0.5",
                        )}
                      />
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
