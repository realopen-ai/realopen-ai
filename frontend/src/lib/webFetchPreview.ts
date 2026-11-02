/** Parse the extraction envelope without mistaking legacy page text for metadata. */
export function safeWebUrl(value?: string): string | undefined {
  try {
    const url = new URL(value || "");
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

export function webFetchPreview(output = "", requestedUrl?: string) {
  const envelope = /^# ([^\n]+)\n\n((?:[^\n]+\n)+)\n/.exec(output);
  if (envelope) {
    const fields = Object.fromEntries(
      envelope[2]
        .trimEnd()
        .split("\n")
        .map((line) => {
          const separator = line.indexOf(": ");
          return [line.slice(0, separator), line.slice(separator + 2)];
        }),
    );
    const url = safeWebUrl(fields.Source);
    if (
      url &&
      ["trafilatura", "markdownify", "text"].includes(fields.Extraction)
    ) {
      return {
        title: envelope[1],
        url,
        requestedUrl: safeWebUrl(fields["Requested URL"] || requestedUrl),
        author: fields.Author,
        date: fields["Page date (metadata)"],
        content: output.slice(envelope[0].length),
      };
    }
  }
  return {
    title: undefined,
    url: safeWebUrl(requestedUrl),
    requestedUrl: undefined,
    author: undefined,
    date: undefined,
    content: output,
  };
}
