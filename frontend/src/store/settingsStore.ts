import { create } from "zustand";
import { persist } from "zustand/middleware";

import { translations, type Language, type TranslationKey } from "@/i18n";

export type { Language } from "@/i18n";
export type { TranslationKey } from "@/i18n";

export type Appearance = "system" | "dark" | "light";
export type Contrast = "medium" | "increased";
export type AccentColor =
  | "gray"
  | "blue"
  | "green"
  | "yellow"
  | "pink"
  | "orange"
  | "purple"
  | "black";
export type FontSize = "14px" | "16px" | "18px";

interface SettingsState {
  // General
  appearance: Appearance;
  contrast: Contrast;
  accentColor: AccentColor;
  language: Language;
  fontSize: FontSize;

  // Notifications
  notifyDeepSearch: boolean;

  // Actions
  setAppearance: (v: Appearance) => void;
  setContrast: (v: Contrast) => void;
  setAccentColor: (v: AccentColor) => void;
  setLanguage: (v: Language) => void;
  setFontSize: (v: FontSize) => void;
  setNotifyDeepSearch: (v: boolean) => void;
}

// ─── Accent color → CSS values ──────────────────────────────────

export const accentColorMap: Record<
  AccentColor,
  {
    primary: string;
    primaryForeground: string;
    primaryHover: string;
    ring: string;
    glow: string;
  }
> = {
  gray: {
    primary: "#a1a1aa",
    primaryForeground: "#000000",
    primaryHover: "#d4d4d8",
    ring: "#a1a1aa",
    glow: "rgba(161,161,170,0.15)",
  },
  blue: {
    primary: "#6366f1",
    primaryForeground: "#ffffff",
    primaryHover: "#5558e6",
    ring: "#6366f1",
    glow: "rgba(99,102,241,0.15)",
  },
  green: {
    primary: "#22c55e",
    primaryForeground: "#000000",
    primaryHover: "#16a34a",
    ring: "#22c55e",
    glow: "rgba(34,197,94,0.15)",
  },
  yellow: {
    primary: "#eab308",
    primaryForeground: "#000000",
    primaryHover: "#ca8a04",
    ring: "#eab308",
    glow: "rgba(234,179,8,0.15)",
  },
  pink: {
    primary: "#ec4899",
    primaryForeground: "#ffffff",
    primaryHover: "#db2777",
    ring: "#ec4899",
    glow: "rgba(236,72,153,0.15)",
  },
  orange: {
    primary: "#f97316",
    primaryForeground: "#000000",
    primaryHover: "#ea580c",
    ring: "#f97316",
    glow: "rgba(249,115,22,0.15)",
  },
  purple: {
    primary: "#a855f7",
    primaryForeground: "#ffffff",
    primaryHover: "#9333ea",
    ring: "#a855f7",
    glow: "rgba(168,85,247,0.15)",
  },
  black: {
    primary: "#e5e5e5",
    primaryForeground: "#000000",
    primaryHover: "#f5f5f5",
    ring: "#e5e5e5",
    glow: "rgba(229,229,229,0.1)",
  },
};

// ─── Font size options ──────────────────────────────────────────

export const fontSizeOptions: FontSize[] = ["14px", "16px", "18px"];

// ─── Store with persistence ─────────────────────────────────────

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      appearance: "system",
      contrast: "medium",
      accentColor: "gray",
      language: "en",
      fontSize: "14px" as FontSize,
      notifyDeepSearch: true,

      setAppearance: (v) => set({ appearance: v }),
      setContrast: (v) => set({ contrast: v }),
      setAccentColor: (v) => set({ accentColor: v }),
      setLanguage: (v) => set({ language: v }),
      setFontSize: (v) => set({ fontSize: v }),
      setNotifyDeepSearch: (v) => set({ notifyDeepSearch: v }),
    }),
    {
      name: "realopen-ai-settings",
    },
  ),
);

// ─── Helper hooks ───────────────────────────────────────────────

export function t(key: TranslationKey): string {
  const lang = useSettingsStore.getState().language as Language;
  return translations[lang]?.[key] ?? translations.en[key] ?? key;
}

export function useT() {
  const language = useSettingsStore((s) => s.language) as Language;
  return (key: TranslationKey) =>
    translations[language]?.[key] ?? translations.en[key] ?? key;
}
