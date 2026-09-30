import { useEffect } from "react";
import { useSettingsStore, accentColorMap } from "@/store/settingsStore";

/**
 * This component doesn't render anything visible.
 * It listens to settings changes and applies CSS variables to <html>.
 *
 * The palette is intentionally layered so depth comes from surface
 * differences rather than borders:
 *   background → sidebar-bg → card → secondary → accent(hover)
 */
export function ThemeManager() {
  const appearance = useSettingsStore((s) => s.appearance);
  const contrast = useSettingsStore((s) => s.contrast);
  const accentColor = useSettingsStore((s) => s.accentColor);
  const fontSize = useSettingsStore((s) => s.fontSize);
  const language = useSettingsStore((s) => s.language);

  useEffect(() => {
    const root = document.documentElement;
    const css = accentColorMap[accentColor];

    // ── Accent color ──
    root.style.setProperty("--color-primary", css.primary);
    root.style.setProperty("--color-primary-foreground", css.primaryForeground);
    root.style.setProperty("--color-ring", css.ring);

    // ── Font size ──
    root.style.setProperty("--app-font-size", fontSize);

    // ── Appearance ──
    const isDark = (() => {
      if (appearance === "dark") return true;
      if (appearance === "light") return false;
      // system
      return window.matchMedia("(prefers-color-scheme: dark)").matches;
    })();

    if (isDark) {
      root.classList.add("dark");
      root.classList.remove("light");
      // Layered dark: deep near-black base, progressively lighter surfaces.
      root.style.setProperty("--color-background", "#09090b");
      root.style.setProperty("--color-foreground", "#ececef");
      root.style.setProperty("--color-card", "#101014");
      root.style.setProperty("--color-card-foreground", "#ececef");
      root.style.setProperty("--color-popover", "#141419");
      root.style.setProperty("--color-popover-foreground", "#ececef");
      root.style.setProperty("--color-secondary", "#17171c");
      root.style.setProperty("--color-secondary-foreground", "#e4e4e8");
      root.style.setProperty("--color-muted", "#17171c");
      root.style.setProperty("--color-muted-foreground", "#8f8f9a");
      root.style.setProperty("--color-accent", "#1d1d24");
      root.style.setProperty("--color-accent-foreground", "#ececef");
      root.style.setProperty("--color-border", "#222228");
      root.style.setProperty("--color-input", "#26262d");
      root.style.setProperty("--color-sidebar-bg", "#0c0c0e");
      root.style.setProperty("--color-sidebar-border", "#1b1b20");
      root.style.setProperty("--color-sidebar-accent", "#17171c");
      root.style.setProperty("--color-terminal-bg", "#0a0a0c");
      root.style.setProperty("--color-sandbox-bg", "#0e0e12");
      root.style.setProperty("--color-sandbox-border", "#1e1e24");
      // Extended surface / semantic tokens
      root.style.setProperty("--color-surface-hover", "#1d1d24");
      root.style.setProperty("--color-surface-selected", "#202028");
      root.style.setProperty("--color-success", "#34d399");
      root.style.setProperty("--color-warning", "#fbbf24");
      root.style.setProperty("--color-danger", "#f87171");
      root.style.setProperty("--color-shadow-strong", "rgba(0,0,0,0.55)");
      root.style.setProperty("--color-shadow-soft", "rgba(0,0,0,0.35)");
    } else {
      root.classList.add("light");
      root.classList.remove("dark");
      // Layered light: soft off-white base, white elevated surfaces.
      root.style.setProperty("--color-background", "#fafafa");
      root.style.setProperty("--color-foreground", "#1a1a20");
      root.style.setProperty("--color-card", "#ffffff");
      root.style.setProperty("--color-card-foreground", "#1a1a20");
      root.style.setProperty("--color-popover", "#ffffff");
      root.style.setProperty("--color-popover-foreground", "#1a1a20");
      root.style.setProperty("--color-secondary", "#f1f1f3");
      root.style.setProperty("--color-secondary-foreground", "#3a3a42");
      root.style.setProperty("--color-muted", "#f1f1f3");
      root.style.setProperty("--color-muted-foreground", "#74747e");
      root.style.setProperty("--color-accent", "#ececf0");
      root.style.setProperty("--color-accent-foreground", "#1a1a20");
      root.style.setProperty("--color-border", "#e6e6ea");
      root.style.setProperty("--color-input", "#dcdce2");
      root.style.setProperty("--color-sidebar-bg", "#f4f4f6");
      root.style.setProperty("--color-sidebar-border", "#e6e6ea");
      root.style.setProperty("--color-sidebar-accent", "#ececf0");
      root.style.setProperty("--color-terminal-bg", "#121216");
      root.style.setProperty("--color-sandbox-bg", "#fbfbfc");
      root.style.setProperty("--color-sandbox-border", "#e8e8ec");
      // Extended surface / semantic tokens
      root.style.setProperty("--color-surface-hover", "#f0f0f3");
      root.style.setProperty("--color-surface-selected", "#eaeaf0");
      root.style.setProperty("--color-success", "#16a34a");
      root.style.setProperty("--color-warning", "#d97706");
      root.style.setProperty("--color-danger", "#dc2626");
      root.style.setProperty("--color-shadow-strong", "rgba(24,24,32,0.16)");
      root.style.setProperty("--color-shadow-soft", "rgba(24,24,32,0.08)");
    }

    // ── Contrast ──
    if (contrast === "increased") {
      if (isDark) {
        root.style.setProperty("--color-foreground", "#ffffff");
        root.style.setProperty("--color-muted-foreground", "#b4b4bf");
        root.style.setProperty("--color-border", "#3a3a44");
        root.style.setProperty("--color-card-foreground", "#ffffff");
      } else {
        root.style.setProperty("--color-foreground", "#000000");
        root.style.setProperty("--color-muted-foreground", "#52525a");
        root.style.setProperty("--color-border", "#cfcfd6");
      }
    }
  }, [appearance, contrast, accentColor, fontSize]);

  useEffect(() => {
    const root = document.documentElement;
    root.lang = language;
    root.dir = language === "ar" ? "rtl" : "ltr";
  }, [language]);

  // Listen for system theme changes when in "system" mode
  useEffect(() => {
    if (appearance !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => {
      // Trigger re-render by touching the store
      useSettingsStore.setState({ appearance: "system" });
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [appearance]);

  return null;
}
