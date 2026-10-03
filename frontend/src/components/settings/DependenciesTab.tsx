import { useState, useCallback, useEffect, useRef } from "react";
import {
  Download,
  Loader2,
  AlertCircle,
  Package,
  Terminal,
  Trash2,
} from "lucide-react";
import {
  fetchDependencies,
  installDependency,
  uninstallDependency,
  type DependencyInfo,
  type InstallEvent,
} from "@/api/depsClient";
import { cn } from "@/lib/utils";
import { EmptyState, StatusDot } from "@/components/ui/primitives";
import { t, useT } from "@/store/settingsStore";

export function DependenciesTab() {
  useT();
  const [deps, setDeps] = useState<DependencyInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null); // name of dep being installed/uninstalled
  const [busyAction, setBusyAction] = useState<"install" | "uninstall" | null>(
    null,
  );
  const [installOutput, setInstallOutput] = useState<string>("");
  const [installError, setInstallError] = useState<string>("");
  const [confirmUninstall, setConfirmUninstall] = useState<string | null>(null);
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
      setBusy(name);
      setBusyAction("install");
      setInstallOutput("");
      setInstallError("");

      await installDependency(name, (event: InstallEvent) => {
        if (event.output) {
          setInstallOutput((prev) => prev + event.output + "\n");
          logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
        }
        if (event.stage === "error") {
          setInstallError(event.error || t("settings.dependencies.installFailed"));
          if (event.manual_command) {
            setInstallError(
              event.error + `\n\n${t("settings.dependencies.manualCommand")}:\n` + event.manual_command,
            );
          }
        }
      });

      setBusy(null);
      setBusyAction(null);
      await loadDeps();
    },
    [loadDeps],
  );

  const handleUninstall = useCallback(
    async (name: string) => {
      setBusy(name);
      setBusyAction("uninstall");
      setConfirmUninstall(null);
      setInstallOutput("");
      setInstallError("");

      await uninstallDependency(name, (event: InstallEvent) => {
        if (event.output) {
          setInstallOutput((prev) => prev + event.output + "\n");
          logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
        }
        if (event.stage === "error") {
          setInstallError(event.error || t("settings.dependencies.uninstallFailed"));
        }
      });

      setBusy(null);
      setBusyAction(null);
      await loadDeps();
    },
    [loadDeps],
  );

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-5 h-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (deps.length === 0) {
    return (
      <EmptyState
        icon={<Package />}
        title={t("settings.dependencies.empty")}
        className="py-10"
      />
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs leading-relaxed text-muted-foreground">
        {t("settings.dependencies.description")}
      </p>

      <div className="divide-y divide-border/50">
        {deps.map((dep) => (
          <DependencyRow
            key={dep.name}
            dep={dep}
            isBusy={busy === dep.name}
            busyAction={busy === dep.name ? busyAction : null}
            installOutput={busy === dep.name ? installOutput : ""}
            installError={busy === dep.name ? installError : ""}
            confirmUninstall={confirmUninstall === dep.name}
            logsEndRef={logsEndRef}
            onInstall={() => handleInstall(dep.name)}
            onUninstall={() => setConfirmUninstall(dep.name)}
            onConfirmUninstall={() => handleUninstall(dep.name)}
            onCancelUninstall={() => setConfirmUninstall(null)}
          />
        ))}
      </div>
    </div>
  );
}

function DependencyRow({
  dep,
  isBusy,
  busyAction,
  installOutput,
  installError,
  confirmUninstall,
  logsEndRef,
  onInstall,
  onUninstall,
  onConfirmUninstall,
  onCancelUninstall,
}: {
  dep: DependencyInfo;
  isBusy: boolean;
  busyAction: "install" | "uninstall" | null;
  installOutput: string;
  installError: string;
  confirmUninstall: boolean;
  logsEndRef: React.RefObject<HTMLDivElement | null>;
  onInstall: () => void;
  onUninstall: () => void;
  onConfirmUninstall: () => void;
  onCancelUninstall: () => void;
}) {
  const [showOutput, setShowOutput] = useState(false);

  return (
    <div className="py-3.5">
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <p className="text-[13.5px] font-medium text-foreground">
              {dep.display_name}
            </p>
            {dep.install_size && (
              <span className="text-[11px] text-muted-foreground">
                {dep.install_size}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            {dep.description}
          </p>

          {/* Status */}
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {isBusy ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" />
                {busyAction === "install" ? t("settings.dependencies.installing") : t("settings.dependencies.uninstalling")}
              </span>
            ) : dep.installed ? (
              <StatusDot
                tone="success"
                label={`${t("settings.dependencies.installed")}${dep.version ? ` · ${dep.version}` : ""}`}
              />
            ) : dep.available ? (
              <StatusDot tone="warning" label={t("settings.dependencies.notInstalled")} />
            ) : (
              <StatusDot
                tone="neutral"
                label={t("settings.dependencies.notAvailable", { distro: dep.distro })}
              />
            )}
            {dep.in_overlay && (
              <span
                className="inline-flex items-center gap-1 text-[11px] text-muted-foreground"
                title={t("settings.dependencies.volumeHelp")}
              >
                <Package className="h-3 w-3" />
                {t("settings.dependencies.persistentVolume")}
              </span>
            )}
          </div>

          {/* Features enabled */}
          {dep.enables.length > 0 && (
            <div className="mt-1.5 space-y-0.5">
              {dep.enables.map((feature) => (
                <div key={feature} className="flex items-center gap-1.5">
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      dep.installed ? "bg-success" : "bg-muted-foreground/30",
                    )}
                  />
                  <span className="text-[11px] text-muted-foreground">
                    {feature}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex shrink-0 items-center gap-1.5">
          {!dep.installed && dep.available && !isBusy && (
            <button
              onClick={onInstall}
              className="inline-flex h-7 items-center gap-1.5 rounded-md bg-primary/10 px-2.5 text-[12.5px] font-medium text-primary transition-colors hover:bg-primary/20"
            >
              <Download className="h-3.5 w-3.5" />
              {t("settings.dependencies.install")}
            </button>
          )}

          {dep.installed && !isBusy && !confirmUninstall && (
            <button
              onClick={onUninstall}
              aria-label={t("settings.dependencies.uninstallNamed", { name: dep.display_name })}
              title={t("settings.dependencies.uninstall")}
              className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground/80 transition-colors hover:bg-danger/10 hover:text-danger"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
          {dep.installed && confirmUninstall && !isBusy && (
            <div className="flex items-center gap-1">
              <button
                onClick={onConfirmUninstall}
                className="rounded-md bg-danger/10 px-2 py-1 text-[11.5px] font-medium text-danger transition-colors hover:bg-danger/20"
              >
                {t("settings.dependencies.uninstall")}
              </button>
              <button
                onClick={onCancelUninstall}
                className="rounded-md px-2 py-1 text-[11.5px] text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
              >
                {t("workspace.cancel")}
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Install/uninstall output (collapsible) */}
      {(isBusy || installError) && (
        <div className="mt-2.5">
          {installError && (
            <div className="mb-2 flex items-start gap-2 rounded-lg bg-danger/10 px-3 py-2">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
              <p className="whitespace-pre-wrap text-[11.5px] leading-relaxed text-danger">
                {installError}
              </p>
            </div>
          )}
          {installOutput && (
            <div>
              <button
                onClick={() => setShowOutput(!showOutput)}
                className="inline-flex items-center gap-1.5 text-[11.5px] text-muted-foreground transition-colors hover:text-foreground"
              >
                <Terminal className="h-3 w-3" />
                {showOutput ? t("settings.dependencies.hideLog") : t("settings.dependencies.showLog")}
              </button>
              {showOutput && (
                <pre className="mt-1.5 max-h-48 overflow-y-auto rounded-lg bg-secondary p-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
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
