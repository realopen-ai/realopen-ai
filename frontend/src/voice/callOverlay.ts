export type CallOverlayCorner =
  | "top-left"
  | "top-right"
  | "bottom-left"
  | "bottom-right";

export function nearestCallOverlayCorner(
  point: { x: number; y: number },
  bounds: { left: number; top: number; width: number; height: number },
): CallOverlayCorner {
  const horizontal =
    point.x < bounds.left + bounds.width / 2 ? "left" : "right";
  const vertical = point.y < bounds.top + bounds.height / 2 ? "top" : "bottom";
  return `${vertical}-${horizontal}`;
}
