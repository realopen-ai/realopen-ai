const SANDBOX_PORTS = new Set([6767, 6969]);

export function previewHostUrl(port: number | null, fallback: string): string {
  return port && SANDBOX_PORTS.has(port)
    ? `http://localhost:${port}`
    : fallback;
}

export function normalizePreviewAddress(
  value: string,
  currentBase: string,
): string | null {
  const input = value.trim();
  if (!input) return null;
  if (input.startsWith("/") && currentBase.startsWith("http")) {
    return new URL(input, currentBase)
      .toString()
      .replace(/\/$/, input === "/" ? "/" : "");
  }
  const candidate = /^localhost:|^127\.0\.0\.1:/.test(input)
    ? `http://${input}`
    : input;
  try {
    const url = new URL(candidate);
    if (
      !["localhost", "127.0.0.1"].includes(url.hostname) ||
      !SANDBOX_PORTS.has(Number(url.port))
    ) {
      return null;
    }
    return url.toString().replace(/\/$/, url.pathname === "/" ? "" : "/");
  } catch {
    return null;
  }
}
