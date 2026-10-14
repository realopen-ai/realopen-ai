/** Read only validated paging metadata; never guess missing tool identities. */
export function toolOutputDetails(output?: string) {
  try {
    const data = JSON.parse(output || "null");
    if (
      !data ||
      typeof data !== "object" ||
      Array.isArray(data) ||
      typeof data.result_ref !== "string" ||
      typeof data.content !== "string"
    )
      return null;
    const count = Array.from(data.content).length; // Backend offsets count Unicode code points.
    const end = data.offset + count;
    if (
      !Number.isSafeInteger(data.offset) ||
      data.offset < 0 ||
      !Number.isSafeInteger(data.total_chars) ||
      data.total_chars < end ||
      (data.next_offset !== null &&
        (!Number.isSafeInteger(data.next_offset) ||
          data.next_offset !== end ||
          data.next_offset >= data.total_chars)) ||
      (data.next_offset === null && end !== data.total_chars)
    )
      return null;
    let formatted = data.content;
    let isJson = false;
    try {
      formatted = JSON.stringify(JSON.parse(data.content), null, 2);
      isJson = true;
    } catch {}
    return {
      resultRef: data.result_ref as string,
      sourceTool:
        typeof data.source_tool === "string" ? data.source_tool : undefined,
      sourceTitle:
        typeof data.source_title === "string" ? data.source_title : undefined,
      offset: data.offset as number,
      end,
      count,
      totalChars: data.total_chars as number,
      nextOffset: data.next_offset as number | null,
      formatted,
      isJson,
    };
  } catch {
    return null;
  }
}
