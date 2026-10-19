export async function showStatus(
  element: HTMLElement,
  load: () => Promise<string>,
) {
  element.setAttribute("aria-busy", "true");
  element.textContent = "Loading…";
  try {
    element.textContent = await load();
  } catch (error) {
    element.textContent =
      error instanceof Error ? error.message : "Request failed";
  } finally {
    element.removeAttribute("aria-busy");
  }
}
