import { common, createStarryNight } from "@wooorm/starry-night";

export const MAX_HIGHLIGHT_BYTES = 200_000;

export type HighlightNode =
  | { type: "text"; value: string }
  | {
      type: "element";
      tagName: string;
      properties?: Record<string, unknown>;
      children: HighlightNode[];
    };

export interface HighlightResult {
  scope: string;
  children: HighlightNode[];
}

let starryNightPromise: ReturnType<typeof createStarryNight> | undefined;
const cache = new Map<string, HighlightResult | null>();
const MAX_CACHE_ENTRIES = 100;

function highlighter() {
  starryNightPromise ??= createStarryNight(common);
  return starryNightPromise;
}

function remember(key: string, result: HighlightResult | null) {
  if (cache.size >= MAX_CACHE_ENTRIES) cache.delete(cache.keys().next().value!);
  cache.set(key, result);
  return result;
}

export async function highlightCode({
  code,
  language,
  filePath,
}: {
  code: string;
  language?: string;
  filePath?: string;
}): Promise<HighlightResult | null> {
  const flag = (language || filePath || "").replace(/^language-/, "").trim();
  if (!flag || new TextEncoder().encode(code).byteLength > MAX_HIGHLIGHT_BYTES)
    return null;
  const key = `${flag}\0${code}`;
  if (cache.has(key)) return cache.get(key) ?? null;
  const instance = await highlighter();
  const scope = instance.flagToScope(flag);
  if (!scope) return remember(key, null);
  const tree = instance.highlight(code, scope);
  return remember(key, {
    scope,
    children: tree.children as HighlightNode[],
  });
}
