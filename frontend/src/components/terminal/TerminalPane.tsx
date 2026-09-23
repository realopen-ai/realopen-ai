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

/** Write a single terminal history entry to an xterm instance */
function writeHistoryEntry(xterm: XTerm, entry: string, accent: string) {
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

export type TerminalMode = "coder" | "shell";

export function TerminalPane({
  mode = "shell",
  active = true,
}: {
  mode?: TerminalMode;
  active?: boolean;
}) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const lastHistoryLenRef = useRef(0);
  const sandboxId = useSandboxStore((s) => s.sandboxId);

  // Create the terminal once + immediately replay any pending history
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
      cursorBlink: mode === "shell",
      cursorStyle: "bar",
      scrollback: 1000,
      allowTransparency: true,
    });

    const fitAddon = new FitAddon();
    xterm.loadAddon(fitAddon);
    xterm.open(terminalRef.current);

    // Try to fit; if container is 0-width (panel collapsed), that's OK;
    // the ResizeObserver below will re-fit when the container becomes visible.
    try {
      fitAddon.fit();
    } catch {
      /* container might be 0-width */
    }

    xtermRef.current = xterm;
    fitAddonRef.current = fitAddon;

    if (mode === "coder") {
      xterm.writeln(`${accent}  RealOpen-AI Sandbox Terminal\x1b[0m`);
    } else if (!sandboxId) {
      xterm.writeln("\x1b[2;37m  Link a running workspace to open a shell.\x1b[0m");
    } else {
      xterm.writeln("\x1b[2;37m  Connecting to workspace shell...\x1b[0m");
    }
    xterm.writeln("");

    // ── Replay any pending history that was added while terminal was unmounted ──
    const history = mode === "coder" ? useSandboxStore.getState().terminalHistory : [];
    if (mode === "coder" && history.length > 0) {
      xterm.writeln(""); // visual separator
      for (const entry of history) {
        writeHistoryEntry(xterm, entry, accent);
      }
    }
    lastHistoryLenRef.current = history.length;

    let socket: WebSocket | null = null;
    let disposed = false;
    if (mode === "shell" && sandboxId) {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/api/sandboxes/${sandboxId}/terminal`);
      socket.binaryType = "arraybuffer";
      socket.onopen = () => {
        if (disposed) {
          socket?.close();
          return;
        }
        socket?.send(JSON.stringify({ cols: xterm.cols, rows: xterm.rows }));
        xterm.focus();
      };
      socket.onmessage = (event) => {
        if (!disposed) xterm.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
      };
      socket.onerror = () => {
        if (!disposed) xterm.writeln("\r\n\x1b[31mTerminal connection failed. Is the workspace running?\x1b[0m");
      };
      xterm.onData((data) => { if (socket?.readyState === WebSocket.OPEN) socket.send(new TextEncoder().encode(data)); });
    }

    const resizeObserver = new ResizeObserver(() => {
      try {
        fitAddon.fit();
        if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols: xterm.cols, rows: xterm.rows }));
      } catch {
        /* ignore — container might be 0-width */
      }
    });
    resizeObserver.observe(terminalRef.current);

    return () => {
      disposed = true;
      resizeObserver.disconnect();
      if (socket?.readyState === WebSocket.OPEN) socket.close();
      else if (socket?.readyState === WebSocket.CONNECTING) {
        socket.onopen = () => socket?.close();
      }
      xterm.dispose();
      xtermRef.current = null;
      fitAddonRef.current = null;
    };
  }, [mode, sandboxId]);

  useEffect(() => {
    if (!active) return;
    try {
      fitAddonRef.current?.fit();
      xtermRef.current?.focus();
    } catch {
      /* the panel can still be transitioning from display:none */
    }
  }, [active]);

  // Write new terminal history entries (after initial replay)
  useEffect(() => {
    if (mode !== "coder") return;
    const xterm = xtermRef.current;
    if (!xterm) return;
    const history = useSandboxStore.getState().terminalHistory;
    const newEntries = history.slice(lastHistoryLenRef.current);
    if (newEntries.length === 0) return;

    const accent = hexToAnsi(
      accentColorMap[useSettingsStore.getState().accentColor].primary,
    );

    for (const entry of newEntries) {
      writeHistoryEntry(xterm, entry, accent);
    }
    lastHistoryLenRef.current = history.length;
  }, [mode, useSandboxStore((s) => s.terminalHistory.length)]);

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
