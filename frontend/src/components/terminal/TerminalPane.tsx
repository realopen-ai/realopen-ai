import { useEffect, useRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { useSandboxStore } from "@/store/sandboxStore";

export function TerminalPane() {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const lastHistoryLenRef = useRef(0);
  const addTerminalLine = useSandboxStore((s) => s.addTerminalLine);

  useEffect(() => {
    if (!terminalRef.current || xtermRef.current) return;

    const xterm = new XTerm({
      theme: {
        background: "#0a0a0a",
        foreground: "#d4d4d4",
        cursor: "#6366f1",
        cursorAccent: "#0a0a0a",
        selectionBackground: "#6366f133",
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
    xterm.writeln("\x1b[38;2;99;102;241m  RealOpen-AI Sandbox Terminal\x1b[0m");
    xterm.writeln("\x1b[2;37m  Awaiting commands from AI agent...\x1b[0m");
    xterm.writeln("");
    xterm.write("\x1b[38;2;99;102;241m❯\x1b[0m ");

    let currentLine = "";
    xterm.onData((data) => {
      if (data === "\r") {
        xterm.writeln("");
        if (currentLine.trim()) {
          addTerminalLine(`$ ${currentLine}`);
          xterm.writeln("\x1b[2;37m  [Command sent to sandbox]\x1b[0m");
        }
        xterm.write("\x1b[38;2;99;102;241m❯\x1b[0m ");
        currentLine = "";
      } else if (data === "\u007F") {
        if (currentLine.length > 0) {
          currentLine = currentLine.slice(0, -1);
          xterm.write("\b \b");
        }
      } else if (data === "\u0003") {
        xterm.writeln("^C");
        xterm.write("\x1b[38;2;99;102;241m❯\x1b[0m ");
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

  useEffect(() => {
    const xterm = xtermRef.current;
    if (!xterm) return;
    const newEntries = useSandboxStore
      .getState()
      .terminalHistory.slice(lastHistoryLenRef.current);
    for (const entry of newEntries) {
      if (entry.startsWith("$")) {
        xterm.writeln(`\x1b[38;2;99;102;241m${entry}\x1b[0m`);
      } else if (entry.startsWith("Error") || entry.startsWith("error")) {
        xterm.writeln(`\x1b[31m${entry}\x1b[0m`);
      } else {
        xterm.writeln(`\x1b[2;37m${entry}\x1b[0m`);
      }
    }
    lastHistoryLenRef.current =
      useSandboxStore.getState().terminalHistory.length;
  }, [useSandboxStore((s) => s.terminalHistory.length)]);

  return (
    <div className="h-full w-full bg-terminal-bg">
      <div ref={terminalRef} className="h-full w-full" />
    </div>
  );
}
