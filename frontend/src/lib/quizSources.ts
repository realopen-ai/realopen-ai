/** Defensive rendering for saved/legacy quiz provenance, never HTML. */
export function quizSourceLinks(values: string[] = []) {
  return [...new Set(values)]
    .flatMap((value) => {
      try {
        const url = new URL(value);
        if (
          !["https:", "http:"].includes(url.protocol) ||
          !url.hostname ||
          url.username ||
          url.password ||
          /[\s\u0000-\u001f]/.test(value)
        )
          return [];
        return [{ href: url.href, label: url.hostname }];
      } catch {
        return [];
      }
    })
    .slice(0, 3);
}
