import type { LocalImportOptions } from "../api/modelManagerClient.ts";

export interface LocalFileLike {
  name: string;
  size: number;
  webkitRelativePath?: string;
}
export function localModelFiles<T extends LocalFileLike>(
  files: T[],
  options: LocalImportOptions,
): { files: { file: T; path: string }[]; ignored: number } {
  const result: { file: T; path: string }[] = [];
  const seen = new Set<string>();
  const allDirectory =
    files.length > 0 &&
    files.every((file) => file.webkitRelativePath?.includes("/"));
  for (const file of files) {
    const original = file.webkitRelativePath || file.name;
    const path = allDirectory
      ? original.split("/").slice(1).join("/")
      : original;
    if (
      path.startsWith("/") ||
      path.split("/").includes("..") ||
      /[\\\x00-\x1f:]/.test(path)
    )
      throw new Error("unsafeFiles");
    const supported =
      options.extensions.some((extension) =>
        path.toLowerCase().endsWith(extension),
      ) || options.companion_files.includes(path);
    if (!supported) continue;
    if (seen.has(path.toLowerCase())) throw new Error("duplicateFiles");
    seen.add(path.toLowerCase());
    result.push({ file, path });
  }
  if (
    !result.some(({ path }) =>
      options.extensions.some((extension) =>
        path.toLowerCase().endsWith(extension),
      ),
    )
  )
    throw new Error("noWeights");
  if (result.reduce((sum, item) => sum + item.file.size, 0) > options.max_bytes)
    throw new Error("tooLarge");
  // Companion metadata first, then weights; only selected known files leave the browser.
  result.sort(
    (a, b) =>
      Number(
        options.extensions.some((extension) => a.path.endsWith(extension)),
      ) -
      Number(
        options.extensions.some((extension) => b.path.endsWith(extension)),
      ),
  );
  return { files: result, ignored: files.length - result.length };
}
