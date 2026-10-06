export interface NoteSelection {
  start: number;
  end: number;
  text: string;
}

export function selectedNoteSection(
  content: string,
  start: number,
  end: number,
): NoteSelection | null {
  if (start < 0 || end > content.length || end <= start) return null;
  return { start, end, text: content.slice(start, end) };
}

export function applyNoteProposal(
  content: string,
  proposal: string,
  selection: NoteSelection | null,
): string {
  if (!selection) return proposal;
  if (content.slice(selection.start, selection.end) !== selection.text)
    throw new Error("Selected section changed. Generate a new proposal.");
  return (
    content.slice(0, selection.start) + proposal + content.slice(selection.end)
  );
}
