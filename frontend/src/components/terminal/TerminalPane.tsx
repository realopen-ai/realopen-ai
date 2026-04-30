import { useEffect, useRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { useSandboxStore } from "@/store/sandboxStore";
import { useSettingsStore, accentColorMap } from "@/store/settingsStore";

/**
 * Convert a hex color like #6366f1 to an xterm ANSI 24-bit escape sequence prefix.
 * Returns something like "\x1b[38;2;99;102;241m"
 */
function hexToAnsi(hex: string): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `\x1b[38;2;${r};${g};${b}m`;
}

export function TerminalPane() {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const lastHistoryLenRef = useRef(0);
  const addTerminalLine = useSandboxStore((s) => s.addTerminalLine);

  // Create the terminal once
  useEffect(() => {
    if (!terminalRef.current || xtermRef.current) return;

    const accentColor = useSettingsStore.getState().accentColor;
    const css = accentColorMap[accentColor];
    const accent = hexToAnsi(css.primary);

    const xterm = new XTerm({
      theme: {
        background: "#0a0a0a",
        foreground: "#d4d4d4",
        cursor: css.primary,
        cursorAccent: "#0a0a0a",
        selectionBackground: css.primary + "33",
        black: "#1a1a1a",
        red: "#ef4444",
        green: "#4ade80",
        yellow: "#fbbf24",
        blue: "#60a5fa",
        magenta: "#c084fc",
        cyan: "#22d3ee",
        white: "#d4d4d4",
        brightBlack: "#525252",
        brightRed: "#f87171",
        brightGreen: "#86efac",
        brightYellow: "#fde047",
        brightBlue: "#93c5fd",
        brightMagenta: "#d8b4fe",
        brightCyan: "#67e8f9",
        brightWhite: "#f5f5f5",
      },
      fontFamily: "'JetBrains Mono', 'Fira Code', 'SF Mono', monospace",
      fontSize: 12,
      lineHeight: 1.6,
      cursorBlink: true,
      cursorStyle: "bar",
      scrollback: 1000,
      allowTransparency: true,
    });

    const fitAddon = new FitAddon();
    xterm.loadAddon(fitAddon);
    xterm.open(terminalRef.current);
    fitAddon.fit();

    xtermRef.current = xterm;
    fitAddonRef.current = fitAddon;

    // Welcome
    xterm.writeln(`${accent}  RealOpen-AI Sandbox Terminal\x1b[0m`);
    xterm.writeln("\x1b[2;37m  Awaiting commands from AI agent...\x1b[0m");
    xterm.writeln("");
    xterm.write(`${accent}❯\x1b[0m `);

    let currentLine = "";
    xterm.onData((data) => {
      // Re-read accent color at time of input
      const currentAccent = hexToAnsi(
        accentColorMap[useSettingsStore.getState().accentColor].primary,
      );

      if (data === "\r") {
        xterm.writeln("");
        if (currentLine.trim()) {
          addTerminalLine(`$ ${currentLine}`);
          xterm.writeln("\x1b[2;37m  [Command sent to sandbox]\x1b[0m");
        }
        xterm.write(`${currentAccent}❯\x1b[0m `);
        currentLine = "";
      } else if (data === "\u007F") {
        if (currentLine.length > 0) {
          currentLine = currentLine.slice(0, -1);
          xterm.write("\b \b");
        }
      } else if (data === "\u0003") {
        xterm.writeln("^C");
        xterm.write(`${currentAccent}❯\x1b[0m `);
        currentLine = "";
      } else if (data >= " ") {
        currentLine += data;
        xterm.write(data);
      }
    });

    const resizeObserver = new ResizeObserver(() => {
      try {
        fitAddon.fit();
      } catch {
        /* ignore */
      }
    });
    resizeObserver.observe(terminalRef.current);

    return () => {
      resizeObserver.disconnect();
      xterm.dispose();
      xtermRef.current = null;
      fitAddonRef.current = null;
    };
  }, [addTerminalLine]);

  // Write new terminal history entries
  useEffect(() => {
    const xterm = xtermRef.current;
    if (!xterm) return;
    const history = useSandboxStore.getState().terminalHistory;
    const newEntries = history.slice(lastHistoryLenRef.current);
    const accent = hexToAnsi(
      accentColorMap[useSettingsStore.getState().accentColor].primary,
    );

    for (const entry of newEntries) {
      // Each entry is a single line (we split them before addTerminalLine)
      // But we still split on \n as a safety net
      const lines = entry.split("\n");
      for (const line of lines) {
        if (line.startsWith("$")) {
          // Command lines - accent color
          xterm.writeln(`${accent}${line}\x1b[0m`);
        } else if (line.startsWith("  ")) {
          // Indented code lines - slightly dimmer accent
          xterm.writeln(`${accent}${line}\x1b[0m`);
        } else if (line.startsWith("Error") || line.startsWith("error")) {
          xterm.writeln(`\x1b[31m${line}\x1b[0m`);
        } else {
          // Output lines - dim gray
          xterm.writeln(`\x1b[2;37m${line}\x1b[0m`);
        }
      }
    }
    lastHistoryLenRef.current = history.length;
  }, [useSandboxStore((s) => s.terminalHistory.length)]);

  // Update xterm theme when accent color changes
  useEffect(() => {
    const xterm = xtermRef.current;
    if (!xterm) return;
    const css = accentColorMap[useSettingsStore.getState().accentColor];
    xterm.options.theme = {
      ...xterm.options.theme,
      cursor: css.primary,
      selectionBackground: css.primary + "33",
    };
  }, [useSettingsStore((s) => s.accentColor)]);

  return (
    <div className="h-full w-full bg-terminal-bg">
      <div ref={terminalRef} className="h-full w-full" />
    </div>
  );
}
