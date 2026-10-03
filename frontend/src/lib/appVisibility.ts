let focused = typeof document !== "undefined" ? document.hasFocus() : true;
let visible =
  typeof document !== "undefined"
    ? document.visibilityState === "visible"
    : true;

if (typeof window !== "undefined" && typeof document !== "undefined") {
  window.addEventListener("focus", () => {
    focused = true;
  });
  window.addEventListener("blur", () => {
    focused = false;
  });
  document.addEventListener("visibilitychange", () => {
    visible = document.visibilityState === "visible";
  });
}

export function isAppActive(): boolean {
  return focused && visible;
}
