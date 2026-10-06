import type { Flashcard, Rating } from "../api/flashcardsClient.ts";

export function studyQueue(
  cards: Flashcard[],
  all: boolean,
  now = Date.now(),
): Flashcard[] {
  return cards
    .filter((card) => all || Date.parse(card.due_at) <= now)
    .sort(
      (a, b) =>
        Date.parse(a.due_at) - Date.parse(b.due_at) || a.position - b.position,
    );
}
export function shuffled<T>(items: T[], random = Math.random): T[] {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}
export function ratingForKey(key: string, code = ""): Rating | null {
  // Physical number-row keys also work without Shift on layouts such as AZERTY.
  const number = /^Digit[1-4]$/.test(code) ? code.slice(-1) : key;
  return (
    (
      { "1": "again", "2": "hard", "3": "good", "4": "easy" } as Record<
        string,
        Rating
      >
    )[number] ?? null
  );
}
