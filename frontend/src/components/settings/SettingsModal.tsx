import { useState, useCallback } from "react";
import {
  X,
  Sun,
  Moon,
  Monitor,
  Check,
  Bot,
  Image,
  Download,
  Loader2,
  Puzzle,
  SlidersHorizontal,
  Sparkles,
  Package,
  Mic2,
  Bell,
  BellOff,
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
import {
  SectionHeader,
  EmptyState,
  StatusDot,
  FilterChip,
} from "@/components/ui/primitives";
import { SettingRow, SettingToggle } from "@/components/brain/ToolsTab";
import { DependenciesTab } from "@/components/settings/DependenciesTab";
import { AiTab } from "@/components/settings/AiTab";
import { VoiceTab } from "@/components/settings/VoiceTab";

type TabKey =
  | "general"
  | "ai"
  | "voice"
  | "modules"
  | "dependencies"
  | "notifications";

// ─── Segmented control (compact option switch) ───────────────────

type SegmentedOption<T extends string> = {
  value: T;
  label: string;
  icon?: React.ReactNode;
};

function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
}: {
  value: T;
  options: SegmentedOption<T>[];
  onChange: (v: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div
      className="inline-flex h-8 items-center rounded-lg bg-secondary p-0.5"
      role="group"
      aria-label={ariaLabel}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={cn(
            "inline-flex h-7 items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-2.5 text-[12.5px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
            value === option.value
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {option.icon}
          {option.label}
        </button>
      ))}
    </div>
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
        "group relative flex h-8 w-8 items-center justify-center rounded-full transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
        isActive && "ring-2 ring-offset-2 ring-offset-background",
      )}
      style={{
        backgroundColor: css.primary,
        ["--tw-ring-color" as string]: css.primary,
      }}
      title={color.charAt(0).toUpperCase() + color.slice(1)}
    >
      {isActive && (
        <Check
          className="h-3.5 w-3.5"
          style={{ color: css.primaryForeground }}
        />
      )}
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

// ─── Module Row (one row in the Modules list) ─────────────────────

function ModuleRow({
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
    <div className="flex items-start gap-3.5 py-4">
      {/* Icon */}
      <div
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
          module.enabled && isAvailable
            ? "bg-primary/10 text-primary"
            : "bg-secondary text-muted-foreground/80",
        )}
      >
        <ModuleIcon icon={module.icon} className="h-4.5 w-4.5" />
      </div>

      {/* Content */}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13.5px] font-medium text-foreground">
            {module.label}
          </span>
          {isRequired && (
            <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary">
              {t("modules.required")}
            </span>
          )}
          {module.estimated_size && isAvailable && (
            <span className="text-[11px] text-muted-foreground">
              {t("modules.estimatedSize")}: {module.estimated_size}
            </span>
          )}
        </div>

        <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
          {module.description}
        </p>

        {/* Status indicators */}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          {!isAvailable && (
            <StatusDot
              tone="neutral"
              label={t("modules.notAvailableForProfile")}
            />
          )}
          {isAvailable && !requirementsMet && (
            <StatusDot
              tone="warning"
              label={
                <>
                  {t("modules.requirementsNotMet")}
                  {module.minimum_requirements && (
                    <span>
                      {" "}
                      ({module.minimum_requirements.ram} GB RAM,{" "}
                      {module.minimum_requirements.vram} GB VRAM)
                    </span>
                  )}
                </>
              }
            />
          )}
          {module.enabled && !module.models_downloaded && isAvailable && (
            <StatusDot
              tone="warning"
              label={t("modules.modelsNotDownloaded")}
            />
          )}
          {module.enabled && module.models_downloaded && (
            <StatusDot tone="success" label={t("modules.ready")} />
          )}
        </div>

        {/* Install button + progress */}
        {module.enabled && !module.models_downloaded && isAvailable && (
          <div className="mt-2.5">
            {isDownloading && progress ? (
              <div className="max-w-70 space-y-1.5">
                <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {progress.status}
                </div>
                <div className="h-1 w-full overflow-hidden rounded-full bg-secondary">
                  <div
                    className="h-full rounded-full bg-primary transition-all duration-300"
                    style={{ width: `${progress.percent}%` }}
                  />
                </div>
              </div>
            ) : (
              <button
                onClick={() => onInstall(module.name)}
                className="inline-flex h-7 items-center gap-1.5 rounded-md bg-primary/10 px-2.5 text-[12.5px] font-medium text-primary transition-colors hover:bg-primary/20"
              >
                <Download className="h-3.5 w-3.5" />
                {t("modules.downloadModels")}
              </button>
            )}
          </div>
        )}

        {/* Models list for optional modules */}
        {module.models && module.models.length > 0 && isAvailable && (
          <div className="mt-2.5 flex flex-wrap gap-1.5">
            {module.models.map((m, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-1.5 rounded-full bg-secondary px-2 py-0.5 text-[10.5px] text-muted-foreground"
              >
                <span
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    module.models_downloaded ? "bg-success" : "bg-warning",
                  )}
                />
                {m.description || m.id}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Right: toggle (optional) or locked indicator (required) */}
      {canToggle ? (
        <SettingToggle
          checked={module.enabled}
          onChange={(v) => onToggle(module.name, v)}
          disabled={isDownloading}
          label={module.label}
        />
      ) : (
        isRequired && <Check className="mt-1.5 h-4 w-4 shrink-0 text-success" />
      )}
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
      label: t("settings.voice"),
      icon: <Mic2 className="w-4 h-4" />,
    },
    {
      key: "modules",
      label: t("settings.modules"),
      icon: <Puzzle className="w-4 h-4" />,
    },
    {
      key: "dependencies",
      label: t("settings.dependencies"),
      icon: <Package className="w-4 h-4" />,
    },
    {
      key: "notifications",
      label: t("settings.notifications"),
      icon: <Bell className="w-4 h-4" />,
    },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/65 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal — desktop-application settings layout: section nav on the
          left, settings rows on the right. Mobile: header + chip nav. */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("settings.title")}
        className="relative flex h-[min(620px,92vh)] w-full max-w-210 animate-fade-in flex-col overflow-hidden rounded-xl border border-border/70 bg-card shadow-[0_24px_70px_-12px_var(--color-shadow-strong)]"
      >
        {/* ── Header ── */}
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-border/60 px-5">
          <h2 className="text-[16.5px] font-semibold tracking-[-0.01em] text-foreground">
            {t("settings.title")}
          </h2>
          <button
            onClick={onClose}
            aria-label={t("common.close")}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* ── Mobile: horizontal chip nav ── */}
        <div className="scrollbar-none flex gap-1.5 overflow-x-auto border-b border-border/60 px-3 py-2.5 md:hidden">
          {tabs.map((tabItem) => (
            <FilterChip
              key={tabItem.key}
              active={tab === tabItem.key}
              onClick={() => setTab(tabItem.key)}
            >
              {tabItem.icon}
              {tabItem.label}
            </FilterChip>
          ))}
        </div>

        {/* ── Body: section nav (desktop) + content ── */}
        <div className="flex min-h-0 flex-1">
          {/* Section nav */}
          <nav className="hidden w-52 shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border/60 p-2.5 md:flex">
            {tabs.map((tabItem) => (
              <button
                key={tabItem.key}
                onClick={() => setTab(tabItem.key)}
                className={cn(
                  "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-left text-[13px] transition-colors",
                  tab === tabItem.key
                    ? "bg-primary/10 font-medium text-primary"
                    : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
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
            <div className="mt-auto px-2.5 pt-3">
              <p className="text-[10.5px] leading-relaxed text-muted-foreground/70">
                RealOpen-AI
              </p>
            </div>
          </nav>

          {/* Scrollable settings content */}
          <div className="min-w-0 flex-1 overflow-y-auto">
            <div className="px-5 py-6 md:px-7 md:py-7">
              {tab === "general" && (
                <div className="divide-y divide-border/50">
                  <SettingRow
                    label={t("settings.appearance")}
                    className="flex-wrap"
                  >
                    <SegmentedControl<Appearance>
                      value={appearance}
                      onChange={setAppearance}
                      ariaLabel={t("settings.appearance")}
                      options={[
                        {
                          value: "system",
                          label: t("settings.appearance.system"),
                          icon: <Monitor className="w-3.5 h-3.5" />,
                        },
                        {
                          value: "dark",
                          label: t("settings.appearance.dark"),
                          icon: <Moon className="w-3.5 h-3.5" />,
                        },
                        {
                          value: "light",
                          label: t("settings.appearance.light"),
                          icon: <Sun className="w-3.5 h-3.5" />,
                        },
                      ]}
                    />
                  </SettingRow>

                  <SettingRow
                    label={t("settings.accentColor")}
                    className="flex-wrap"
                  >
                    <div className="flex flex-wrap items-center gap-2">
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
                  </SettingRow>

                  <SettingRow
                    label={t("settings.fontSize")}
                    className="flex-wrap"
                  >
                    <SegmentedControl<FontSize>
                      value={fontSize}
                      onChange={setFontSize}
                      ariaLabel={t("settings.fontSize")}
                      options={fontSizeOptions.map((size) => ({
                        value: size,
                        label: t(`settings.fontSize.${size}`),
                        icon: (
                          <span
                            className="font-semibold leading-none"
                            style={{ fontSize: size }}
                          >
                            Aa
                          </span>
                        ),
                      }))}
                    />
                  </SettingRow>

                  <SettingRow
                    label={t("settings.contrast")}
                    className="flex-wrap"
                  >
                    <SegmentedControl<Contrast>
                      value={contrast}
                      onChange={setContrast}
                      ariaLabel={t("settings.contrast")}
                      options={[
                        {
                          value: "medium",
                          label: t("settings.contrast.medium"),
                        },
                        {
                          value: "increased",
                          label: t("settings.contrast.increased"),
                        },
                      ]}
                    />
                  </SettingRow>

                  <SettingRow
                    label={t("settings.language")}
                    className="flex-wrap"
                  >
                    <SegmentedControl<Language>
                      value={language}
                      onChange={setLanguage}
                      ariaLabel={t("settings.language")}
                      options={[
                        { value: "en", label: t("settings.language.en") },
                        { value: "fr", label: t("settings.language.fr") },
                        { value: "ar", label: t("settings.language.ar") },
                      ]}
                    />
                  </SettingRow>
                </div>
              )}

              {tab === "ai" && <AiTab />}

              {tab === "voice" && <VoiceTab />}

              {tab === "modules" && (
                <div>
                  <SectionHeader
                    title={t("settings.modules")}
                    description={t("modules.description")}
                  />
                  {modules.length > 0 ? (
                    <div className="divide-y divide-border/50">
                      {modules.map((module) => (
                        <ModuleRow
                          key={module.name}
                          module={module}
                          onToggle={handleToggleModule}
                          onInstall={handleInstallModule}
                          installing={installingModule === module.name}
                          installProgress={installProgress}
                        />
                      ))}
                    </div>
                  ) : (
                    <EmptyState
                      icon={<Puzzle />}
                      title={t("modules.noModules")}
                      className="py-10"
                    />
                  )}
                </div>
              )}

              {tab === "dependencies" && <DependenciesTab />}

              {tab === "notifications" && (
                <div className="divide-y divide-border/50">
                  <div className="flex items-center gap-4 py-3.5">
                    <div
                      className={cn(
                        "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
                        notifyDeepSearch
                          ? "bg-primary/10 text-primary"
                          : "bg-secondary text-muted-foreground/80",
                      )}
                    >
                      {notifyDeepSearch ? (
                        <Bell className="h-4.5 w-4.5" />
                      ) : (
                        <BellOff className="h-4.5 w-4.5" />
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-[13.5px] text-foreground">
                        {t("settings.notifyDeepSearch")}
                      </div>
                      <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                        {t("settings.notifyDeepSearchDescription")}
                      </div>
                    </div>
                    <SettingToggle
                      checked={notifyDeepSearch}
                      onChange={(v) => setNotifyDeepSearch(v)}
                      label={t("settings.notifyDeepSearch")}
                    />
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
