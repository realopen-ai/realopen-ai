import { create } from "zustand";
import { persist } from "zustand/middleware";

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
export type Language = "en" | "fr";
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

// ─── i18n strings ───────────────────────────────────────────────

export const translations: Record<Language, Record<string, string>> = {
  en: {
    "sidebar.chats": "Chats",
    "sidebar.newChat": "New chat",
    "sidebar.noConversations": "No conversations yet",
    "sidebar.startNew": "Start a new chat to begin",
    "sidebar.hardwareProfile": "Hardware Profile",
    "sidebar.offlinePrivate": "100% Offline · Fully Private",
    "sidebar.collapse": "Collapse",
    "sidebar.settings": "Settings",
    "welcome.title": "RealOpen-AI",
    "welcome.subtitle":
      "Your fully offline, private AI assistant. Powered by local models running on your hardware.",
    "welcome.profile": "Profile",
    "welcome.explain": "Explain a concept",
    "welcome.explainSub": "in simple terms",
    "welcome.writeCode": "Write code",
    "welcome.writeCodeSub": "to solve a problem",
    "welcome.searchWeb": "Search the web",
    "welcome.searchWebSub": "for latest information",
    "welcome.runCode": "Run code",
    "welcome.runCodeSub": "in a sandbox",
    "welcome.availableModels": "Available Models",
    "input.placeholder": "Message RealOpen-AI...",
    "input.runningLocally": "Running locally via Ollama",
    "settings.title": "Settings",
    "settings.general": "General",
    "settings.notifications": "Notifications",
    "settings.appearance": "Appearance",
    "settings.appearance.system": "System",
    "settings.appearance.dark": "Dark",
    "settings.appearance.light": "Light",
    "settings.contrast": "Contrast",
    "settings.contrast.medium": "Medium",
    "settings.contrast.increased": "Increased",
    "settings.accentColor": "Accent color",
    "settings.language": "Language",
    "settings.language.en": "English",
    "settings.language.fr": "French",
    "settings.fontSize": "Font size",
    "settings.fontSize.14px": "Small (14px)",
    "settings.fontSize.16px": "Medium (16px)",
    "settings.fontSize.18px": "Large (18px)",
    "settings.notifyDeepSearch": "Get notified about DeepSearch tasks",
    "badge.searched": "Searched the web",
    "badge.deepSearch": "Deep research",
    "badge.ranCode": "Ran code",
    "badge.readFile": "Read file",
    "badge.writeFile": "Wrote file",
    "sandbox.title": "Sandbox",
    "sandbox.toolCalls": "tool calls",
    "sandbox.toolCall": "tool call",
    "panel.files": "Files",
    "panel.terminal": "Terminal",
    "panel.fileExplorer": "File Explorer",
    "mobile.chat": "Chat",
    "mobile.files": "Files",
    "mobile.terminal": "Terminal",
  },
  fr: {
    "sidebar.chats": "Conversations",
    "sidebar.newChat": "Nouvelle conversation",
    "sidebar.noConversations": "Aucune conversation",
    "sidebar.startNew": "Commencez une nouvelle conversation",
    "sidebar.hardwareProfile": "Profil matériel",
    "sidebar.offlinePrivate": "100% Hors ligne · Entièrement privé",
    "sidebar.collapse": "Réduire",
    "sidebar.settings": "Paramètres",
    "welcome.title": "RealOpen-AI",
    "welcome.subtitle":
      "Votre assistant IA privé et hors ligne. Propulsé par des modèles locaux sur votre matériel.",
    "welcome.profile": "Profil",
    "welcome.explain": "Expliquer un concept",
    "welcome.explainSub": "en termes simples",
    "welcome.writeCode": "Écrire du code",
    "welcome.writeCodeSub": "pour résoudre un problème",
    "welcome.searchWeb": "Rechercher sur le web",
    "welcome.searchWebSub": "pour les dernières infos",
    "welcome.runCode": "Exécuter du code",
    "welcome.runCodeSub": "dans un bac à sable",
    "welcome.availableModels": "Modèles disponibles",
    "input.placeholder": "Envoyer un message à RealOpen-AI...",
    "input.runningLocally": "Fonctionne localement via Ollama",
    "settings.title": "Paramètres",
    "settings.general": "Général",
    "settings.notifications": "Notifications",
    "settings.appearance": "Apparence",
    "settings.appearance.system": "Système",
    "settings.appearance.dark": "Sombre",
    "settings.appearance.light": "Clair",
    "settings.contrast": "Contraste",
    "settings.contrast.medium": "Moyen",
    "settings.contrast.increased": "Augmenté",
    "settings.accentColor": "Couleur d'accent",
    "settings.language": "Langue",
    "settings.language.en": "Anglais",
    "settings.language.fr": "Français",
    "settings.fontSize": "Taille de police",
    "settings.fontSize.14px": "Petite (14px)",
    "settings.fontSize.16px": "Moyenne (16px)",
    "settings.fontSize.18px": "Grande (18px)",
    "settings.notifyDeepSearch": "Être notifié des tâches DeepSearch",
    "badge.searched": "Recherche web",
    "badge.deepSearch": "Recherche approfondie",
    "badge.ranCode": "Code exécuté",
    "badge.readFile": "Fichier lu",
    "badge.writeFile": "Fichier écrit",
    "sandbox.title": "Bac à sable",
    "sandbox.toolCalls": "appels d'outil",
    "sandbox.toolCall": "appel d'outil",
    "panel.files": "Fichiers",
    "panel.terminal": "Terminal",
    "panel.fileExplorer": "Explorateur de fichiers",
    "mobile.chat": "Chat",
    "mobile.files": "Fichiers",
    "mobile.terminal": "Terminal",
  },
};

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

export function t(key: string): string {
  const lang = useSettingsStore.getState().language;
  return translations[lang]?.[key] ?? translations.en[key] ?? key;
}

export function useT() {
  const language = useSettingsStore((s) => s.language);
  return (key: string) =>
    translations[language]?.[key] ?? translations.en[key] ?? key;
}
