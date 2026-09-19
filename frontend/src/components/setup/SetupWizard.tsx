import { useEffect, useCallback, useState } from "react";
import {
  Monitor,
  Cpu,
  HardDrive,
  Cpu as GpuIcon,
  Check,
  ChevronRight,
  ChevronLeft,
  ChevronDown,
  Loader2,
  AlertTriangle,
  Download,
  Bot,
  Image,
  Puzzle,
  Zap,
  ArrowRight,
  Volume2,
  AudioLines,
  Package,
} from "lucide-react";
import {
  useSetupStore,
  type SetupStep,
  type PullRow,
} from "@/store/setupStore";
import type { SetupModule } from "@/api/setupClient";
import { t } from "@/store/settingsStore";
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
  const [showAllProfiles, setShowAllProfiles] = useState(false);

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

  const recommendedProfile = hardwareInfo.recommended_profile;

  // Split profiles: recommended first, then others
  const recommendedEntry = [
    recommendedProfile,
    profiles[recommendedProfile],
  ] as const;
  const otherEntries = Object.entries(profiles).filter(
    ([name]) => name !== recommendedProfile,
  );

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
      <div className="space-y-2">
        {/* Recommended profile — always shown with green border */}
        {recommendedEntry[1] && (
          <ProfileCard
            name={recommendedEntry[0]}
            profile={recommendedEntry[1]}
            isSelected={selectedProfile === recommendedEntry[0]}
            isRecommended={true}
            onSelect={() => setSelectedProfile(recommendedEntry[0])}
          />
        )}

        {/* Expandable "More profiles" toggle */}
        {otherEntries.length > 0 && (
          <>
            <button
              onClick={() => setShowAllProfiles(!showAllProfiles)}
              className="flex items-center gap-1.5 w-full py-2 px-1 text-[12px] text-muted-foreground hover:text-foreground transition-colors"
            >
              <ChevronDown
                className={cn(
                  "w-3.5 h-3.5 transition-transform",
                  showAllProfiles && "rotate-180",
                )}
              />
              <span>
                {showAllProfiles
                  ? "Hide other profiles"
                  : `${otherEntries.length} more profile${otherEntries.length > 1 ? "s" : ""} available`}
              </span>
            </button>

            {/* Other profiles — only shown when expanded */}
            {showAllProfiles && (
              <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                {otherEntries.map(([name, profile]) => (
                  <ProfileCard
                    key={name}
                    name={name}
                    profile={profile}
                    isSelected={selectedProfile === name}
                    isRecommended={false}
                    onSelect={() => setSelectedProfile(name)}
                  />
                ))}
              </div>
            )}
          </>
        )}
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

function ProfileCard({
  name: _profileName,
  profile,
  isSelected,
  isRecommended,
  onSelect,
}: {
  name: string;
  profile: {
    label: string;
    description: string;
    models: { id: string; description: string; size: string }[];
  };
  isSelected: boolean;
  isRecommended: boolean;
  onSelect: () => void;
}) {
  // _profileName is used as key by the parent but not needed in render
  void _profileName;
  return (
    <button
      onClick={onSelect}
      className={cn(
        "w-full text-left p-3 rounded-xl border transition-all",
        isRecommended && isSelected
          ? "border-emerald-500/40 bg-emerald-500/5 ring-1 ring-emerald-500/20"
          : isSelected
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
                ? isRecommended
                  ? "border-emerald-500"
                  : "border-primary"
                : "border-muted-foreground/30",
            )}
          >
            {isSelected && (
              <div
                className={cn(
                  "w-1.5 h-1.5 rounded-full",
                  isRecommended ? "bg-emerald-500" : "bg-primary",
                )}
              />
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
              selectedProfile={selectedProfile}
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
  selectedProfile,
}: {
  module: SetupModule;
  enabled: boolean;
  available: boolean;
  canToggle: boolean;
  onToggle: () => void;
  selectedProfile: string;
}) {
  // Compute the exact model size for the selected profile
  // For optional modules: use profile_models[selectedProfile] to get model sizes
  // For required modules: models come from profiles.yml (shown in profile card)
  const profileModels = module.profile_models?.[selectedProfile] ?? [];
  const exactSize =
    profileModels.length > 0
      ? profileModels.map((m) => m.size).join(" + ")
      : null;

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
            {available && exactSize && (
              <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground/60">
                <Download className="w-3 h-3" />
                Size: {exactSize}
              </span>
            )}
            {available && !exactSize && module.estimated_size && (
              <span className="text-[11px] text-muted-foreground/60">
                Est. size: {module.estimated_size}
              </span>
            )}
          </div>
          {/* Show model details for optional modules */}
          {available && !module.required && profileModels.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {profileModels.map((m) => (
                <span
                  key={m.id}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-secondary text-[10px] text-muted-foreground"
                >
                  {m.description || m.id}
                  <span className="text-muted-foreground/50">{m.size}</span>
                </span>
              ))}
            </div>
          )}
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
            .flatMap((mod) => mod.profile_models?.[selectedProfile] ?? [])
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

/** Provider icon for a setup row — voice artifacts get distinct icons
 *  (ASR = speaker, TTS = waveform, runtime = package); ollama models keep
 *  the previous neutral look. */
function PullRowIcon({ row }: { row: PullRow }) {
  if (row.kind === "voice_model" || row.provider === "qwen3-asr") {
    return <Volume2 className="w-3.5 h-3.5 text-primary shrink-0" />;
  }
  if (row.provider === "pocket-tts") {
    return <AudioLines className="w-3.5 h-3.5 text-primary shrink-0" />;
  }
  if (row.kind === "voice_runtime") {
    return <Package className="w-3.5 h-3.5 text-primary shrink-0" />;
  }
  return <Bot className="w-3.5 h-3.5 text-muted-foreground/60 shrink-0" />;
}

/** Provider label for voice rows (ASR model / TTS model / Voice runtime). */
function pullRowProviderLabel(row: PullRow): string | null {
  if (row.provider === "qwen3-asr" || row.kind === "voice_model") {
    // Distinguish ASR vs TTS by provider when possible.
    if (row.provider === "pocket-tts") return t("setup.voice.provider.tts");
    return t("setup.voice.provider.asr");
  }
  if (row.provider === "pocket-tts") return t("setup.voice.provider.tts");
  if (row.kind === "voice_runtime") return t("setup.voice.provider.runtime");
  return null;
}

/** One install row of the unified list (ollama models + voice models +
 *  voice runtimes). Real percent bar when pull_progress provides one;
 *  indeterminate animated bar + streamed status/output text when only
 *  pull_status events arrive (NO fake percentages); done rows with a
 *  check mark (incl. "already installed"); error rows with retry hint. */
function InstallRow({ row }: { row: PullRow }) {
  const providerLabel = pullRowProviderLabel(row);
  const isVoice = row.kind === "voice_model" || row.kind === "voice_runtime";

  return (
    <div
      className={cn(
        "rounded-lg border px-2.5 py-2",
        row.status === "error"
          ? "border-red-500/30 bg-red-500/5"
          : row.status === "done"
            ? "border-emerald-500/20 bg-emerald-500/5"
            : "border-border/60 bg-secondary/30",
      )}
    >
      <div className="flex items-center gap-2">
        <PullRowIcon row={row} />
        <span className="text-[11.5px] font-medium text-foreground truncate flex-1">
          {row.model}
        </span>
        {/* Provider chip for voice artifacts */}
        {providerLabel && (
          <span
            className={cn(
              "text-[9px] px-1.5 py-0.5 rounded-full shrink-0 uppercase tracking-wide",
              isVoice
                ? "bg-primary/10 text-primary"
                : "bg-secondary text-muted-foreground",
            )}
          >
            {providerLabel}
          </span>
        )}
        {/* Terminal state indicator */}
        {row.status === "done" && (
          <span className="flex items-center gap-1 shrink-0">
            <Check className="w-3.5 h-3.5 text-emerald-500" />
            {row.alreadyInstalled && (
              <span className="text-[9.5px] text-muted-foreground/70">
                {t("setup.installing.alreadyInstalled")}
              </span>
            )}
          </span>
        )}
        {row.status === "error" && (
          <AlertTriangle className="w-3.5 h-3.5 text-red-500 shrink-0" />
        )}
        {/* Live percentage when real numbers arrive */}
        {row.status === "running" && row.hasPercent && (
          <span className="text-[10px] font-medium text-foreground shrink-0">
            {row.percent ?? 0}%
          </span>
        )}
      </div>

      {/* Progress bar — real percent when available, indeterminate
          animated bar otherwise (never a fake percentage). */}
      {row.status === "running" && (
        <div className="mt-1.5 w-full h-1.5 bg-secondary rounded-full overflow-hidden">
          {row.hasPercent ? (
            <div
              className="h-full bg-primary/70 rounded-full transition-all duration-200"
              style={{ width: `${row.percent ?? 0}%` }}
            />
          ) : (
            <div className="h-full w-full rounded-full voice-indeterminate-bar text-primary/50" />
          )}
        </div>
      )}

      {/* Streamed status / output text (pull_status events) */}
      {row.status === "running" && (row.statusText || row.outputText) && (
        <p className="mt-1.5 text-[10px] text-muted-foreground/70 truncate">
          {row.outputText || row.statusText}
        </p>
      )}

      {/* Error details + retry hint */}
      {row.status === "error" && (
        <div className="mt-1">
          {row.error && (
            <p className="text-[10px] text-red-500 truncate">{row.error}</p>
          )}
          <p className="text-[10px] text-muted-foreground/60">
            {t("setup.installing.retryHint")}
          </p>
        </div>
      )}
    </div>
  );
}

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

  // Completion gate: the backend's `pull_all_done` (status === "done")
  // fires only after every artifact — including voice models and voice
  // runtimes — reached a terminal state (pull_done or pull_error). We
  // additionally verify on the frontend that every known row is terminal
  // before enabling the finish transition (mirrors existing error
  // handling: errors stay visible but do not block completion).
  const allRowsTerminal =
    pullProgress.rows.length === 0 ||
    pullProgress.rows.every((r) => r.status === "done" || r.status === "error");

  useEffect(() => {
    if (pullProgress.status === "done" && !isPulling && allRowsTerminal) {
      const timer = setTimeout(() => {
        completeSetup();
        setStep("done");
      }, 1000);
      return () => clearTimeout(timer);
    }
  }, [pullProgress.status, isPulling, allRowsTerminal]);

  const totalPercent =
    pullProgress.totalModels > 0
      ? Math.round(
          (pullProgress.completed.length / pullProgress.totalModels) * 100,
        )
      : 0;

  const showRows = pullProgress.rows.length > 0;

  return (
    <div className="py-8">
      <div className="flex flex-col items-center text-center">
        <div className="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mb-6">
          <Download className="w-8 h-8 text-primary animate-bounce" />
        </div>
        <h3 className="text-[18px] font-semibold text-foreground mb-2">
          {t("setup.installing.title")}
        </h3>
        <p className="text-[13px] text-muted-foreground mb-6 max-w-sm">
          {t("setup.installing.subtitle")}
        </p>

        {/* Overall progress — SAME progress for models AND voice deps */}
        <div className="w-full max-w-sm mb-4">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[12px] text-muted-foreground">
              {t("setup.installing.overallProgress")}
            </span>
            <span className="text-[12px] font-medium text-foreground">
              {pullProgress.completed.length}/{pullProgress.totalModels}{" "}
              {t("setup.installing.completed").toLowerCase()}
            </span>
          </div>
          <div className="w-full h-2 bg-secondary rounded-full overflow-hidden">
            <div
              className="h-full bg-primary rounded-full transition-all duration-300"
              style={{ width: `${totalPercent}%` }}
            />
          </div>
        </div>

        {/* Unified install rows — ollama models + voice models/runtimes */}
        {showRows && (
          <div className="w-full max-w-sm mb-4 space-y-1.5 max-h-64 overflow-y-auto text-left">
            {pullProgress.rows.map((row) => (
              <InstallRow key={row.model} row={row} />
            ))}
          </div>
        )}

        {/* Current model progress (legacy view before any rows arrive,
            and for backends that only emit aggregate progress) */}
        {!showRows && pullProgress.currentModel && isPulling && (
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

        {/* Completed models (compact list, kept for quick scanning) */}
        {!showRows && pullProgress.completed.length > 0 && (
          <div className="w-full max-w-sm mt-2">
            <p className="text-[11px] text-muted-foreground/50 uppercase tracking-wider mb-1">
              {t("setup.installing.completed")}
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
              {t("setup.installing.errors")}
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
