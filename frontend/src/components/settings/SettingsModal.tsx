import { useState } from "react";
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
} from "lucide-react";
import {
  useSettingsStore,
  accentColorMap,
  type Appearance,
  type Contrast,
  type AccentColor,
  type Language,
} from "@/store/settingsStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";

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

// ─── Settings Modal ──────────────────────────────────────────────

export function SettingsModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"general" | "notifications">("general");
  const t = useT();

  const appearance = useSettingsStore((s) => s.appearance);
  const contrast = useSettingsStore((s) => s.contrast);
  const accentColor = useSettingsStore((s) => s.accentColor);
  const language = useSettingsStore((s) => s.language);
  const notifyDeepSearch = useSettingsStore((s) => s.notifyDeepSearch);

  const setAppearance = useSettingsStore((s) => s.setAppearance);
  const setContrast = useSettingsStore((s) => s.setContrast);
  const setAccentColor = useSettingsStore((s) => s.setAccentColor);
  const setLanguage = useSettingsStore((s) => s.setLanguage);
  const setNotifyDeepSearch = useSettingsStore((s) => s.setNotifyDeepSearch);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal */}
      <div className="relative w-full max-w-lg mx-4 bg-card border border-border rounded-2xl shadow-2xl overflow-hidden animate-fade-in">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-border/50">
          <h2 className="text-[16px] font-semibold text-foreground">
            {t("settings.title")}
          </h2>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Tab Bar */}
        <div className="flex border-b border-border/50">
          <button
            onClick={() => setTab("general")}
            className={cn(
              "flex-1 py-2.5 text-[13px] font-medium transition-colors relative",
              tab === "general"
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t("settings.general")}
            {tab === "general" && (
              <div className="absolute bottom-0 left-1/2 -translate-x-1/2 w-12 h-0.5 bg-primary rounded-full" />
            )}
          </button>
          <button
            onClick={() => setTab("notifications")}
            className={cn(
              "flex-1 py-2.5 text-[13px] font-medium transition-colors relative",
              tab === "notifications"
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t("settings.notifications")}
            {tab === "notifications" && (
              <div className="absolute bottom-0 left-1/2 -translate-x-1/2 w-12 h-0.5 bg-primary rounded-full" />
            )}
          </button>
        </div>

        {/* Content */}
        <div className="p-5 max-h-[60vh] overflow-y-auto">
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
                      notifyDeepSearch ? "translate-x-5" : "translate-x-0.5",
                    )}
                  />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
