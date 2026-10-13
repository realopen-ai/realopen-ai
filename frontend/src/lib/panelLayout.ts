/** Ignore mount-time measurements and keep notebook Studio in control of visibility. */
export function rightPanelVisibilityAfterResize(
  percentage: number,
  previousPercentage: number | undefined,
  notebookOpen: boolean,
): boolean | null {
  if (previousPercentage === undefined) return null;
  if (percentage === 0) return false;
  if (previousPercentage === 0 && !notebookOpen) return true;
  return null;
}
