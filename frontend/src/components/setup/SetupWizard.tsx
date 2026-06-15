import { useEffect, useCallback } from "react";
import {
  Monitor,
  Cpu,
  HardDrive,
  Cpu as GpuIcon,
  Check,
  ChevronRight,
  ChevronLeft,
  Loader2,
  AlertTriangle,
  Download,
  Bot,
  Image,
  Puzzle,
  Zap,
  ArrowRight,
} from "lucide-react";
import { useSetupStore, type SetupStep } from "@/store/setupStore";
import type { SetupModule } from "@/api/setupClient";
import { cn } from "@/lib/utils";

// ─── Step indicator ──────────────────────────────────────────────

const STEPS: { key: SetupStep; label: string; num: number }[] = [
  { key: "welcome", label: "Welcome", num: 1 },
  { key: "prerequisites", label: "Prerequisites", num: 2 },
  { key: "hardware", label: "Hardware", num: 3 },
  { key: "modules", label: "Modules", num: 4 },
  { key: "review", label: "Review", num: 5 },
  { key: "installing", label: "Installing", num: 6 },
  { key: "done", label: "Done", num: 7 },
];

function StepIndicator({ current }: { current: SetupStep }) {
  const currentIdx = STEPS.findIndex((s) => s.key === current);
  return (
    <div className="flex items-center gap-1 px-2">
      {STEPS.map((step, idx) => (
        <div key={step.key} className="flex items-center gap-1">
          <div
            className={cn(
              "w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-medium transition-all",
              idx < currentIdx
                ? "bg-emerald-500 text-white"
                : idx === currentIdx
                  ? "bg-primary text-primary-foreground"
                  : "bg-secondary text-muted-foreground",
            )}
          >
            {idx < currentIdx ? <Check className="w-3.5 h-3.5" /> : step.num}
          </div>
          {idx < STEPS.length - 1 && (
            <div
              className={cn(
                "w-4 h-0.5 rounded-full transition-colors",
                idx < currentIdx ? "bg-emerald-500" : "bg-secondary",
              )}
            />
          )}
        </div>
      ))}
    </div>
  );
}

// ─── Module Icon ──────────────────────────────────────────────────

function ModuleIcon({ icon, className }: { icon: string; className?: string }) {
  switch (icon) {
    case "bot":
      return <Bot className={className} />;
    case "image":
      return <Image className={className} />;
    default:
      return <Puzzle className={className} />;
  }
}

// ─── Welcome Step ─────────────────────────────────────────────────

function WelcomeStep() {
  const setStep = useSetupStore((s) => s.setStep);
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center">
      <div className="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mb-6">
        <Zap className="w-8 h-8 text-primary" />
      </div>
      <h2 className="text-2xl font-bold text-foreground mb-3">
        Welcome to RealOpen-AI
      </h2>
      <p className="text-muted-foreground text-[14px] max-w-md leading-relaxed mb-2">
        Your fully offline, private AI assistant. This setup wizard will help
        you configure your hardware profile, choose which features to enable,
        and download the required AI models.
      </p>
      <p className="text-muted-foreground/60 text-[12px] max-w-sm leading-relaxed mb-8">
        Everything runs locally on your machine — no data is sent to the cloud.
        All models are downloaded from Ollama and run entirely offline.
      </p>
      <button
        onClick={() => setStep("prerequisites")}
        className="flex items-center gap-2 px-6 py-3 rounded-xl bg-primary text-primary-foreground font-medium text-[14px] hover:bg-primary/90 transition-colors"
      >
        Get Started
        <ArrowRight className="w-4 h-4" />
      </button>
    </div>
  );
}

// ─── Prerequisites Step ───────────────────────────────────────────

function PrerequisitesStep() {
  const setStep = useSetupStore((s) => s.setStep);
  const hardwareInfo = useSetupStore((s) => s.hardwareInfo);

  const checks = [
    {
      label: "Docker",
      ok: hardwareInfo?.docker_available ?? false,
      detail: hardwareInfo?.docker_available
        ? "Docker is installed and running"
        : "Docker is not running — please start Docker Desktop",
    },
    {
      label: "Ollama",
      ok: hardwareInfo?.ollama_installed ?? false,
      detail: hardwareInfo?.ollama_installed
        ? "Ollama is installed"
        : "Ollama is not installed — setup will help you install it",
    },
    {
      label: "Ollama Server",
      ok: hardwareInfo?.ollama_running ?? false,
      detail: hardwareInfo?.ollama_running
        ? "Ollama server is running"
        : "Ollama server is not running — models can't be pulled yet",
    },
  ];

  return (
    <div className="py-4">
      <h3 className="text-[16px] font-semibold text-foreground mb-1">
        Prerequisites Check
      </h3>
      <p className="text-[13px] text-muted-foreground mb-5">
        Let&apos;s make sure everything you need is ready.
      </p>
      <div className="space-y-3">
        {checks.map((check) => (
          <div
            key={check.label}
            className={cn(
              "flex items-start gap-3 p-3 rounded-xl border",
              check.ok
                ? "border-emerald-500/20 bg-emerald-500/5"
                : "border-amber-500/20 bg-amber-500/5",
            )}
          >
            <div
              className={cn(
                "w-6 h-6 rounded-full flex items-center justify-center shrink-0 mt-0.5",
                check.ok
                  ? "bg-emerald-500 text-white"
                  : "bg-amber-500 text-white",
              )}
            >
              {check.ok ? (
                <Check className="w-3.5 h-3.5" />
              ) : (
                <AlertTriangle className="w-3.5 h-3.5" />
              )}
            </div>
            <div>
              <p className="text-[13px] font-medium text-foreground">
                {check.label}
              </p>
              <p className="text-[12px] text-muted-foreground">
                {check.detail}
              </p>
            </div>
          </div>
        ))}
      </div>
      <div className="flex items-center justify-between mt-6">
        <button
          onClick={() => setStep("welcome")}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        >
          <ChevronLeft className="w-4 h-4" />
          Back
        </button>
        <button
          onClick={() => setStep("hardware")}
          className="flex items-center gap-1.5 px-5 py-2.5 rounded-xl bg-primary text-primary-foreground font-medium text-[13px] hover:bg-primary/90 transition-colors"
        >
          Continue
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

// ─── Hardware Step ────────────────────────────────────────────────

function HardwareStep() {
  const setStep = useSetupStore((s) => s.setStep);
  const hardwareInfo = useSetupStore((s) => s.hardwareInfo);
  const profiles = useSetupStore((s) => s.profiles);
  const selectedProfile = useSetupStore((s) => s.selectedProfile);
  const setSelectedProfile = useSetupStore((s) => s.setSelectedProfile);

  if (!hardwareInfo) return null;

  const gpuTypeDisplay =
    hardwareInfo.gpu_type === "nvidia"
      ? "NVIDIA (CUDA)"
      : hardwareInfo.gpu_type === "apple"
        ? "Apple Silicon"
        : "None (CPU-only)";

  const vramDisplay =
    hardwareInfo.gpu_type === "nvidia"
      ? `${hardwareInfo.gpu_vram_gb} GB`
      : hardwareInfo.gpu_type === "apple"
        ? `Unified (${hardwareInfo.ram_gb} GB)`
        : "N/A";

  return (
    <div className="py-4">
      <h3 className="text-[16px] font-semibold text-foreground mb-1">
        Hardware Detection
      </h3>
      <p className="text-[13px] text-muted-foreground mb-5">
        We detected the following hardware on your system.
      </p>

      {/* Hardware info card */}
      <div className="rounded-xl border border-border bg-card p-4 mb-5">
        <div className="grid grid-cols-2 gap-3">
          <InfoRow
            icon={<Monitor className="w-4 h-4" />}
            label="Platform"
            value={hardwareInfo.platform_display}
          />
          <InfoRow
            icon={<Cpu className="w-4 h-4" />}
            label="CPU"
            value={hardwareInfo.cpu_name}
          />
          <InfoRow
            icon={<HardDrive className="w-4 h-4" />}
            label="RAM"
            value={`${hardwareInfo.ram_gb} GB`}
          />
          <InfoRow
            icon={<GpuIcon className="w-4 h-4" />}
            label="GPU"
            value={gpuTypeDisplay}
          />
          <InfoRow
            icon={<HardDrive className="w-4 h-4" />}
            label="VRAM"
            value={vramDisplay}
          />
          <InfoRow
            icon={<Cpu className="w-4 h-4" />}
            label="Cores"
            value={`${hardwareInfo.cpu_cores}`}
          />
        </div>
      </div>

      {/* Profile selection */}
      <h4 className="text-[14px] font-medium text-foreground mb-3">
        Recommended Profile
      </h4>
      <div className="space-y-2 max-h-60 overflow-y-auto">
        {Object.entries(profiles).map(([name, profile]) => {
          const isRecommended = name === hardwareInfo.recommended_profile;
          const isSelected = name === selectedProfile;
          return (
            <button
              key={name}
              onClick={() => setSelectedProfile(name)}
              className={cn(
                "w-full text-left p-3 rounded-xl border transition-all",
                isSelected
                  ? "border-primary/30 bg-primary/5 ring-1 ring-primary/20"
                  : "border-border bg-card hover:bg-accent",
              )}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div
                    className={cn(
                      "w-4 h-4 rounded-full border-2 flex items-center justify-center",
                      isSelected
                        ? "border-primary"
                        : "border-muted-foreground/30",
                    )}
                  >
                    {isSelected && (
                      <div className="w-1.5 h-1.5 rounded-full bg-primary" />
                    )}
                  </div>
                  <span className="text-[13px] font-medium text-foreground">
                    {profile.label}
                  </span>
                </div>
                {isRecommended && (
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-500 font-medium">
                    Recommended
                  </span>
                )}
              </div>
              <p className="text-[12px] text-muted-foreground mt-1 ml-6">
                {profile.description}
              </p>
              <div className="flex flex-wrap gap-1 mt-2 ml-6">
                {profile.models.slice(0, 3).map((m) => (
                  <span
                    key={m.id}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-secondary text-[10px] text-muted-foreground"
                  >
                    {m.description || m.id}
                  </span>
                ))}
                {profile.models.length > 3 && (
                  <span className="text-[10px] text-muted-foreground/50">
                    +{profile.models.length - 3} more
                  </span>
                )}
              </div>
            </button>
          );
        })}
      </div>

      <div className="flex items-center justify-between mt-6">
        <button
          onClick={() => setStep("prerequisites")}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        >
          <ChevronLeft className="w-4 h-4" />
          Back
        </button>
        <button
          onClick={() => setStep("modules")}
          className="flex items-center gap-1.5 px-5 py-2.5 rounded-xl bg-primary text-primary-foreground font-medium text-[13px] hover:bg-primary/90 transition-colors"
        >
          Continue
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

function InfoRow({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-start gap-2">
      <span className="text-muted-foreground mt-0.5 shrink-0">{icon}</span>
      <div>
        <p className="text-[11px] text-muted-foreground/60 uppercase tracking-wider">
          {label}
        </p>
        <p className="text-[13px] text-foreground font-medium truncate">
          {value}
        </p>
      </div>
    </div>
  );
}

// ─── Modules Step ─────────────────────────────────────────────────

function ModulesStep() {
  const setStep = useSetupStore((s) => s.setStep);
  const modules = useSetupStore((s) => s.modules);
  const enabledModules = useSetupStore((s) => s.enabledModules);
  const selectedProfile = useSetupStore((s) => s.selectedProfile);
  const toggleModule = useSetupStore((s) => s.toggleModule);

  return (
    <div className="py-4">
      <h3 className="text-[16px] font-semibold text-foreground mb-1">
        Choose Modules
      </h3>
      <p className="text-[13px] text-muted-foreground mb-5">
        Enable the features you want. Required modules are always active.
        Optional modules can be toggled later in Settings.
      </p>
      <div className="space-y-3">
        {modules.map((mod) => {
          const isEnabled = enabledModules.includes(mod.name);
          const isAvailable = mod.availability[selectedProfile] ?? false;
          const canToggle = !mod.required && isAvailable;

          return (
            <ModuleSetupCard
              key={mod.name}
              module={mod}
              enabled={isEnabled}
              available={isAvailable}
              canToggle={canToggle}
              onToggle={() => toggleModule(mod.name)}
            />
          );
        })}
      </div>

      <div className="flex items-center justify-between mt-6">
        <button
          onClick={() => setStep("hardware")}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        >
          <ChevronLeft className="w-4 h-4" />
          Back
        </button>
        <button
          onClick={() => setStep("review")}
          className="flex items-center gap-1.5 px-5 py-2.5 rounded-xl bg-primary text-primary-foreground font-medium text-[13px] hover:bg-primary/90 transition-colors"
        >
          Continue
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

function ModuleSetupCard({
  module,
  enabled,
  available,
  canToggle,
  onToggle,
}: {
  module: SetupModule;
  enabled: boolean;
  available: boolean;
  canToggle: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border p-4 transition-all",
        !available
          ? "border-border/30 bg-card/50 opacity-50"
          : enabled
            ? "border-primary/20 bg-primary/5"
            : "border-border bg-card",
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "shrink-0 w-10 h-10 rounded-lg flex items-center justify-center",
            enabled
              ? "bg-primary/10 text-primary"
              : "bg-secondary text-muted-foreground",
          )}
        >
          <ModuleIcon icon={module.icon} className="w-5 h-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-[14px] font-medium text-foreground">
                {module.label}
              </span>
              {module.required && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-primary/10 text-primary font-medium">
                  Required
                </span>
              )}
            </div>
            {canToggle && (
              <button
                onClick={onToggle}
                className={cn(
                  "relative w-10 h-5.5 rounded-full transition-colors shrink-0",
                  enabled ? "bg-primary" : "bg-secondary",
                )}
              >
                <div
                  className={cn(
                    "absolute top-0.5 w-4.5 h-4.5 rounded-full bg-white transition-transform shadow-sm",
                    enabled ? "translate-x-5" : "translate-x-0.5",
                  )}
                />
              </button>
            )}
            {module.required && (
              <div className="flex items-center gap-1 text-emerald-500">
                <Check className="w-4 h-4" />
              </div>
            )}
          </div>
          <p className="text-[12px] text-muted-foreground mt-1 leading-relaxed">
            {module.description}
          </p>
          <div className="flex items-center gap-3 mt-2">
            {!available && (
              <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground/60">
                <AlertTriangle className="w-3 h-3" />
                Not available for this profile
              </span>
            )}
            {module.estimated_size && available && (
              <span className="text-[11px] text-muted-foreground/60">
                Est. size: {module.estimated_size}
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Review Step ──────────────────────────────────────────────────

function ReviewStep() {
  const setStep = useSetupStore((s) => s.setStep);
  const selectedProfile = useSetupStore((s) => s.selectedProfile);
  const profiles = useSetupStore((s) => s.profiles);
  const modules = useSetupStore((s) => s.modules);
  const enabledModules = useSetupStore((s) => s.enabledModules);
  const applySetup = useSetupStore((s) => s.applySetup);
  const isApplying = useSetupStore((s) => s.isApplying);
  const error = useSetupStore((s) => s.error);

  const profile = profiles[selectedProfile];
  const enabledModuleObjs = modules.filter((m) =>
    enabledModules.includes(m.name),
  );

  const handleApply = useCallback(async () => {
    const success = await applySetup();
    if (success) {
      setStep("installing");
    }
  }, [applySetup, setStep]);

  return (
    <div className="py-4">
      <h3 className="text-[16px] font-semibold text-foreground mb-1">
        Review Configuration
      </h3>
      <p className="text-[13px] text-muted-foreground mb-5">
        Confirm your setup before we download the models.
      </p>

      {/* Profile review */}
      <div className="rounded-xl border border-border bg-card p-4 mb-3">
        <p className="text-[11px] text-muted-foreground/50 uppercase tracking-wider mb-1">
          Hardware Profile
        </p>
        <p className="text-[14px] font-medium text-foreground">
          {profile?.label ?? selectedProfile}
        </p>
        <p className="text-[12px] text-muted-foreground">
          {profile?.description ?? ""}
        </p>
      </div>

      {/* Modules review */}
      <div className="rounded-xl border border-border bg-card p-4 mb-3">
        <p className="text-[11px] text-muted-foreground/50 uppercase tracking-wider mb-2">
          Enabled Modules
        </p>
        <div className="space-y-2">
          {enabledModuleObjs.map((mod) => (
            <div key={mod.name} className="flex items-center gap-2">
              <ModuleIcon
                icon={mod.icon}
                className="w-4 h-4 text-primary shrink-0"
              />
              <span className="text-[13px] text-foreground">{mod.label}</span>
              {mod.required && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-primary/10 text-primary font-medium">
                  Required
                </span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Models to download */}
      <div className="rounded-xl border border-border bg-card p-4 mb-3">
        <p className="text-[11px] text-muted-foreground/50 uppercase tracking-wider mb-2">
          Models to Download
        </p>
        <div className="space-y-1.5">
          {profile?.models.map((m) => (
            <div key={m.id} className="flex items-center gap-2">
              <Download className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
              <span className="text-[12px] text-foreground">
                {m.description || m.id}
              </span>
              <span className="text-[10px] text-muted-foreground/50 ml-auto">
                {m.size}
              </span>
            </div>
          ))}
          {enabledModuleObjs
            .filter((m) => !m.required)
            .flatMap((mod) => mod.models)
            .map((m) => (
              <div key={m.id} className="flex items-center gap-2">
                <Download className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                <span className="text-[12px] text-foreground">
                  {m.description || m.id}
                </span>
                <span className="text-[10px] text-muted-foreground/50 ml-auto">
                  {m.size}
                </span>
              </div>
            ))}
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-3 mb-3">
          <p className="text-[12px] text-red-500">{error}</p>
        </div>
      )}

      <div className="flex items-center justify-between mt-6">
        <button
          onClick={() => setStep("modules")}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        >
          <ChevronLeft className="w-4 h-4" />
          Back
        </button>
        <button
          onClick={handleApply}
          disabled={isApplying}
          className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-primary text-primary-foreground font-medium text-[13px] hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {isApplying ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Applying...
            </>
          ) : (
            <>
              <Download className="w-4 h-4" />
              Apply & Download Models
            </>
          )}
        </button>
      </div>
    </div>
  );
}

// ─── Installing Step ──────────────────────────────────────────────

function InstallingStep() {
  const pullModels = useSetupStore((s) => s.pullModels);
  const isPulling = useSetupStore((s) => s.isPulling);
  const pullProgress = useSetupStore((s) => s.pullProgress);
  const completeSetup = useSetupStore((s) => s.completeSetup);
  const setStep = useSetupStore((s) => s.setStep);

  useEffect(() => {
    if (!isPulling && pullProgress.status !== "done") {
      pullModels();
    }
  }, []);

  useEffect(() => {
    if (pullProgress.status === "done" && !isPulling) {
      const timer = setTimeout(() => {
        completeSetup();
        setStep("done");
      }, 1000);
      return () => clearTimeout(timer);
    }
  }, [pullProgress.status, isPulling]);

  const totalPercent =
    pullProgress.totalModels > 0
      ? Math.round(
          (pullProgress.completed.length / pullProgress.totalModels) * 100,
        )
      : 0;

  return (
    <div className="py-8">
      <div className="flex flex-col items-center text-center">
        <div className="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mb-6">
          <Download className="w-8 h-8 text-primary animate-bounce" />
        </div>
        <h3 className="text-[18px] font-semibold text-foreground mb-2">
          Downloading Models
        </h3>
        <p className="text-[13px] text-muted-foreground mb-6 max-w-sm">
          We&apos;re downloading the AI models needed for your configuration.
          This may take a while depending on your internet speed.
        </p>

        {/* Overall progress */}
        <div className="w-full max-w-sm mb-4">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[12px] text-muted-foreground">
              Overall Progress
            </span>
            <span className="text-[12px] font-medium text-foreground">
              {pullProgress.completed.length}/{pullProgress.totalModels} models
            </span>
          </div>
          <div className="w-full h-2 bg-secondary rounded-full overflow-hidden">
            <div
              className="h-full bg-primary rounded-full transition-all duration-300"
              style={{ width: `${totalPercent}%` }}
            />
          </div>
        </div>

        {/* Current model progress */}
        {pullProgress.currentModel && isPulling && (
          <div className="w-full max-w-sm mb-4">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[12px] text-muted-foreground truncate max-w-50">
                {pullProgress.currentModel}
              </span>
              <span className="text-[12px] font-medium text-foreground">
                {pullProgress.percent}%
              </span>
            </div>
            <div className="w-full h-1.5 bg-secondary rounded-full overflow-hidden">
              <div
                className="h-full bg-primary/60 rounded-full transition-all duration-200"
                style={{ width: `${pullProgress.percent}%` }}
              />
            </div>
          </div>
        )}

        {/* Completed models */}
        {pullProgress.completed.length > 0 && (
          <div className="w-full max-w-sm mt-2">
            <p className="text-[11px] text-muted-foreground/50 uppercase tracking-wider mb-1">
              Completed
            </p>
            <div className="space-y-1 max-h-30 overflow-y-auto">
              {pullProgress.completed.map((m) => (
                <div key={m} className="flex items-center gap-1.5">
                  <Check className="w-3 h-3 text-emerald-500 shrink-0" />
                  <span className="text-[11px] text-muted-foreground truncate">
                    {m}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Errors */}
        {pullProgress.errors.length > 0 && (
          <div className="w-full max-w-sm mt-3">
            <p className="text-[11px] text-red-500/70 uppercase tracking-wider mb-1">
              Errors
            </p>
            {pullProgress.errors.map((e, i) => (
              <p key={i} className="text-[11px] text-red-500">
                {e}
              </p>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Done Step ────────────────────────────────────────────────────

function DoneStep() {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center">
      <div className="w-16 h-16 rounded-2xl bg-emerald-500/10 flex items-center justify-center mb-6">
        <Check className="w-8 h-8 text-emerald-500" />
      </div>
      <h3 className="text-[20px] font-bold text-foreground mb-2">
        Setup Complete!
      </h3>
      <p className="text-[13px] text-muted-foreground mb-6 max-w-sm">
        Your RealOpen-AI is ready. All models have been downloaded and your
        configuration has been applied. You can start chatting now!
      </p>
      <p className="text-[12px] text-muted-foreground/50 mb-4">
        This page will reload automatically...
      </p>
      <Loader2 className="w-5 h-5 text-primary animate-spin" />
    </div>
  );
}

// ─── Main SetupWizard ─────────────────────────────────────────────

export function SetupWizard() {
  const step = useSetupStore((s) => s.step);
  const isChecking = useSetupStore((s) => s.isChecking);
  const checkSetupStatus = useSetupStore((s) => s.checkSetupStatus);
  const loadHardwareInfo = useSetupStore((s) => s.loadHardwareInfo);
  const loadProfiles = useSetupStore((s) => s.loadProfiles);
  const loadModules = useSetupStore((s) => s.loadModules);
  const setupComplete = useSetupStore((s) => s.setupComplete);

  // Load data on mount
  useEffect(() => {
    const init = async () => {
      const complete = await checkSetupStatus();
      if (!complete) {
        await Promise.all([loadHardwareInfo(), loadProfiles(), loadModules()]);
      }
    };
    init();
  }, []);

  // Reload page when setup is complete
  useEffect(() => {
    if (setupComplete) {
      const timer = setTimeout(() => {
        window.location.reload();
      }, 3000);
      return () => clearTimeout(timer);
    }
  }, [setupComplete]);

  if (isChecking) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="w-8 h-8 text-primary animate-spin" />
          <p className="text-[14px] text-muted-foreground">
            Checking setup status...
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-lg">
        {/* Header */}
        <div className="flex items-center justify-center mb-6">
          <StepIndicator current={step} />
        </div>

        {/* Content card */}
        <div className="bg-card border border-border rounded-2xl shadow-2xl p-6">
          {step === "welcome" && <WelcomeStep />}
          {step === "prerequisites" && <PrerequisitesStep />}
          {step === "hardware" && <HardwareStep />}
          {step === "modules" && <ModulesStep />}
          {step === "review" && <ReviewStep />}
          {step === "installing" && <InstallingStep />}
          {step === "done" && <DoneStep />}
        </div>
      </div>
    </div>
  );
}
