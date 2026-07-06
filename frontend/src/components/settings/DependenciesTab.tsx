import { useState, useCallback, useEffect, useRef } from "react";
import {
  Check,
  Download,
  Loader2,
  AlertCircle,
  Package,
  Terminal,
} from "lucide-react";
import {
  fetchDependencies,
  installDependency,
  type DependencyInfo,
  type InstallEvent,
} from "@/api/depsClient";
import { cn } from "@/lib/utils";

export function DependenciesTab() {
  const [deps, setDeps] = useState<DependencyInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [installing, setInstalling] = useState<string | null>(null);
  const [installOutput, setInstallOutput] = useState<string>("");
  const [installError, setInstallError] = useState<string>("");
  const logsEndRef = useRef<HTMLDivElement>(null);

  const loadDeps = useCallback(async () => {
    setIsLoading(true);
    const result = await fetchDependencies();
    setDeps(result);
    setIsLoading(false);
  }, []);

  useEffect(() => {
    loadDeps();
  }, [loadDeps]);

  const handleInstall = useCallback(
    async (name: string) => {
      setInstalling(name);
      setInstallOutput("");
      setInstallError("");

      await installDependency(name, (event: InstallEvent) => {
        if (event.output) {
          setInstallOutput((prev) => prev + event.output + "\n");
          logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
        }
        if (event.stage === "error") {
          setInstallError(event.error || "Installation failed");
          if (event.manual_command) {
            setInstallError(
              event.error + "\n\nManual command:\n" + event.manual_command,
            );
          }
        }
      });

      setInstalling(null);
      // Refresh status after install
      await loadDeps();
    },
    [loadDeps],
  );

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-5 h-5 text-primary animate-spin" />
      </div>
    );
  }

  if (deps.length === 0) {
    return (
      <div className="text-center py-8">
        <Package className="w-8 h-8 text-muted-foreground/30 mx-auto mb-2" />
        <p className="text-[13px] text-muted-foreground/60">
          No optional dependencies available.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-[12px] text-muted-foreground leading-relaxed">
        Optional dependencies extend the capabilities of your AI assistant.
        Install them on demand — they are not included by default to keep the
        installation lightweight.
      </p>

      {deps.map((dep) => (
        <DependencyCard
          key={dep.name}
          dep={dep}
          isInstalling={installing === dep.name}
          installOutput={installing === dep.name ? installOutput : ""}
          installError={installing === dep.name ? installError : ""}
          logsEndRef={logsEndRef}
          onInstall={() => handleInstall(dep.name)}
        />
      ))}
    </div>
  );
}

function DependencyCard({
  dep,
  isInstalling,
  installOutput,
  installError,
  logsEndRef,
  onInstall,
}: {
  dep: DependencyInfo;
  isInstalling: boolean;
  installOutput: string;
  installError: string;
  logsEndRef: React.RefObject<HTMLDivElement | null>;
  onInstall: () => void;
}) {
  const [showOutput, setShowOutput] = useState(false);

  return (
    <div
      className={cn(
        "rounded-xl border p-4 transition-all",
        dep.installed
          ? "border-emerald-500/20 bg-emerald-500/5"
          : isInstalling
            ? "border-amber-500/30 bg-amber-500/5"
            : "border-border bg-card",
      )}
    >
      {/* Header row */}
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "w-10 h-10 rounded-lg flex items-center justify-center shrink-0",
            dep.installed ? "bg-emerald-500/10" : "bg-secondary",
          )}
        >
          {dep.installed ? (
            <Check className="w-5 h-5 text-emerald-400" />
          ) : isInstalling ? (
            <Loader2 className="w-5 h-5 text-amber-400 animate-spin" />
          ) : (
            <Package className="w-5 h-5 text-muted-foreground" />
          )}
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <p className="text-[13px] font-medium text-foreground">
              {dep.display_name}
            </p>
            {dep.install_size && (
              <span className="text-[10px] text-muted-foreground/50">
                {dep.install_size}
              </span>
            )}
          </div>
          <p className="text-[12px] text-muted-foreground/70 mt-0.5 leading-relaxed">
            {dep.description}
          </p>

          {/* Version / status badge */}
          <div className="flex items-center gap-2 mt-2">
            {dep.installed ? (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/10 text-emerald-400">
                <Check className="w-3 h-3" />
                Installed{dep.version ? ` · ${dep.version}` : ""}
              </span>
            ) : dep.available ? (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/10 text-amber-400">
                Not installed
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-secondary text-muted-foreground">
                Not available on {dep.distro}
              </span>
            )}
          </div>

          {/* Features enabled */}
          {dep.enables.length > 0 && (
            <div className="mt-2 space-y-0.5">
              {dep.enables.map((feature) => (
                <div key={feature} className="flex items-center gap-1.5">
                  <div
                    className={cn(
                      "w-1.5 h-1.5 rounded-full",
                      dep.installed
                        ? "bg-emerald-400"
                        : "bg-muted-foreground/30",
                    )}
                  />
                  <span
                    className={cn(
                      "text-[11px]",
                      dep.installed
                        ? "text-foreground/70"
                        : "text-muted-foreground/50",
                    )}
                  >
                    {feature}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Install button */}
        {!dep.installed && dep.available && !isInstalling && (
          <button
            onClick={onInstall}
            className="shrink-0 flex items-center gap-1.5 px-3 py-2 rounded-lg text-[12px] font-medium bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
          >
            <Download className="w-3.5 h-3.5" />
            Install
          </button>
        )}
      </div>

      {/* Install output (collapsible) */}
      {(isInstalling || installError) && (
        <div className="mt-3">
          {installError && (
            <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 mb-2">
              <div className="flex items-start gap-2">
                <AlertCircle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
                <p className="text-[11px] text-red-400 whitespace-pre-wrap">
                  {installError}
                </p>
              </div>
            </div>
          )}
          {installOutput && (
            <div>
              <button
                onClick={() => setShowOutput(!showOutput)}
                className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
              >
                <Terminal className="w-3 h-3" />
                {showOutput ? "Hide" : "Show"} installation log
              </button>
              {showOutput && (
                <pre className="mt-1.5 max-h-48 overflow-y-auto rounded-lg bg-black/80 p-2.5 text-[10px] font-mono text-green-400/80 leading-relaxed">
                  {installOutput}
                  <div className="mt-1.5" ref={logsEndRef} />
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
