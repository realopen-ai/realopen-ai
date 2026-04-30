import { useEffect } from "react";
import { useSettingsStore, accentColorMap } from "@/store/settingsStore";

/**
 * This component doesn't render anything visible.
 * It listens to settings changes and applies CSS variables to <html>.
 */
export function ThemeManager() {
  const appearance = useSettingsStore((s) => s.appearance);
  const contrast = useSettingsStore((s) => s.contrast);
  const accentColor = useSettingsStore((s) => s.accentColor);
  const fontSize = useSettingsStore((s) => s.fontSize);

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
      root.style.setProperty("--color-background", "#000000");
      root.style.setProperty("--color-foreground", "#f5f5f5");
      root.style.setProperty("--color-card", "#111111");
      root.style.setProperty("--color-card-foreground", "#f5f5f5");
      root.style.setProperty("--color-popover", "#111111");
      root.style.setProperty("--color-popover-foreground", "#f5f5f5");
      root.style.setProperty("--color-secondary", "#1a1a1a");
      root.style.setProperty("--color-secondary-foreground", "#f5f5f5");
      root.style.setProperty("--color-muted", "#1a1a1a");
      root.style.setProperty("--color-muted-foreground", "#737373");
      root.style.setProperty("--color-accent", "#1f1f1f");
      root.style.setProperty("--color-accent-foreground", "#f5f5f5");
      root.style.setProperty("--color-border", "#262626");
      root.style.setProperty("--color-input", "#262626");
      root.style.setProperty("--color-sidebar-bg", "#000000");
      root.style.setProperty("--color-sidebar-border", "#1a1a1a");
      root.style.setProperty("--color-sidebar-accent", "#1a1a1a");
      root.style.setProperty("--color-terminal-bg", "#0a0a0a");
      root.style.setProperty("--color-sandbox-bg", "#0d0d0d");
      root.style.setProperty("--color-sandbox-border", "#1f1f1f");
    } else {
      root.classList.add("light");
      root.classList.remove("dark");
      root.style.setProperty("--color-background", "#ffffff");
      root.style.setProperty("--color-foreground", "#171717");
      root.style.setProperty("--color-card", "#f5f5f5");
      root.style.setProperty("--color-card-foreground", "#171717");
      root.style.setProperty("--color-popover", "#f5f5f5");
      root.style.setProperty("--color-popover-foreground", "#171717");
      root.style.setProperty("--color-secondary", "#e5e5e5");
      root.style.setProperty("--color-secondary-foreground", "#171717");
      root.style.setProperty("--color-muted", "#f0f0f0");
      root.style.setProperty("--color-muted-foreground", "#737373");
      root.style.setProperty("--color-accent", "#ebebeb");
      root.style.setProperty("--color-accent-foreground", "#171717");
      root.style.setProperty("--color-border", "#e5e5e5");
      root.style.setProperty("--color-input", "#e5e5e5");
      root.style.setProperty("--color-sidebar-bg", "#f8f8f8");
      root.style.setProperty("--color-sidebar-border", "#e5e5e5");
      root.style.setProperty("--color-sidebar-accent", "#ebebeb");
      root.style.setProperty("--color-terminal-bg", "#1e1e1e");
      root.style.setProperty("--color-sandbox-bg", "#f5f5f5");
      root.style.setProperty("--color-sandbox-border", "#e5e5e5");
    }

    // ── Contrast ──
    if (contrast === "increased") {
      if (isDark) {
        root.style.setProperty("--color-foreground", "#ffffff");
        root.style.setProperty("--color-muted-foreground", "#a3a3a3");
        root.style.setProperty("--color-border", "#404040");
        root.style.setProperty("--color-card-foreground", "#ffffff");
      } else {
        root.style.setProperty("--color-foreground", "#000000");
        root.style.setProperty("--color-muted-foreground", "#525252");
        root.style.setProperty("--color-border", "#d4d4d4");
      }
    }
  }, [appearance, contrast, accentColor, fontSize]);

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
