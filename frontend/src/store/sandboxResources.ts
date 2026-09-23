export const SANDBOX_LIMITS = {
  cpu: { min: 0.25, max: 8 },
  memoryGb: { min: 0.25, max: 16 },
  storageGb: { min: 0.0625, max: 50 },
} as const;

export function sandboxResourcesAreValid(cpu: number, memoryGb: number, storageGb: number) {
  return (
    Number.isFinite(cpu) && cpu >= SANDBOX_LIMITS.cpu.min && cpu <= SANDBOX_LIMITS.cpu.max &&
    Number.isFinite(memoryGb) && memoryGb >= SANDBOX_LIMITS.memoryGb.min && memoryGb <= SANDBOX_LIMITS.memoryGb.max &&
    Number.isFinite(storageGb) && storageGb >= SANDBOX_LIMITS.storageGb.min && storageGb <= SANDBOX_LIMITS.storageGb.max
  );
}

export function sandboxResourcePayload(memoryGb: number, storageGb: number) {
  return {
    memoryLimitMb: Math.round(memoryGb * 1024),
    workspaceQuotaBytes: Math.round(storageGb * 1024 ** 3),
  };
}
