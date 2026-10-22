import type { AppContext } from "../extensions";

export function mount(
  root: HTMLElement,
  context: AppContext,
  mountApp: (target: HTMLElement) => () => void,
) {
  const session = context.session!;
  let closeApp: (() => void) | undefined;
  let identity: number | undefined,
    closed = false,
    pending = false;
  function element(tag: string, text = "") {
    const item = document.createElement(tag);
    item.textContent = text;
    return item;
  }
  function render() {
    if (closed) return;
    const state = session.getSnapshot();
    if (
      state.status === "authenticated" &&
      identity === state.user!.id &&
      closeApp
    )
      return;
    closeApp?.();
    closeApp = undefined;
    identity = undefined;
    root.replaceChildren();
    if (state.status === "authenticated") {
      identity = state.user!.id;
      const header = element("header", "Signed in as " + state.user!.username);
      const button = element("button", "Sign out") as HTMLButtonElement;
      button.onclick = async () => {
        button.disabled = true;
        try {
          await session.logout();
        } catch (error) {
          button.disabled = false;
          const message = element(
            "p",
            String(error instanceof Error ? error.message : error),
          );
          message.setAttribute("role", "alert");
          header.append(message);
        }
      };
      const content = element("div");
      header.append(button);
      root.append(header, content);
      closeApp = mountApp(content);
      return;
    }
    if (state.status === "loading") {
      const status = element("p", "Checking session…");
      status.setAttribute("role", "status");
      root.append(status);
      return;
    }
    if (state.status === "error") {
      const message = element("p", state.error);
      message.setAttribute("role", "alert");
      const retry = element("button", "Retry");
      retry.onclick = () => void session.refresh();
      root.append(message, retry);
      return;
    }
    let register = false;
    const title = element("h1", "Sign in"),
      form = document.createElement("form");
    const username = document.createElement("input"),
      password = document.createElement("input");
    for (const [input, name] of [
      [username, "Username"],
      [password, "Password"],
    ] as const) {
      input.name = name.toLowerCase();
      input.required = true;
      input.minLength = name === "Username" ? 3 : 12;
      input.maxLength = name === "Username" ? 48 : 128;
      input.autocomplete =
        name === "Username" ? "username" : "current-password";
      const label = element("label", name);
      label.append(input);
      form.append(label);
    }
    password.type = "password";
    const message = element("p", state.error);
    message.setAttribute("role", "alert");
    const submit = element("button", "Sign in") as HTMLButtonElement;
    submit.type = "submit";
    const toggle = element("button", "Create an account") as HTMLButtonElement;
    toggle.type = "button";
    toggle.onclick = () => {
      register = !register;
      title.textContent = submit.textContent = register
        ? "Create account"
        : "Sign in";
      password.autocomplete = register ? "new-password" : "current-password";
      toggle.textContent = register
        ? "Already have an account?"
        : "Create an account";
      message.textContent = "";
    };
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (pending || !form.reportValidity()) return;
      pending = true;
      submit.disabled = toggle.disabled = true;
      message.textContent = "";
      try {
        await session.authenticate(
          register ? "register" : "login",
          username.value,
          password.value,
        );
      } catch (error) {
        message.textContent =
          error instanceof Error ? error.message : "Authentication failed";
      } finally {
        pending = false;
        submit.disabled = toggle.disabled = false;
      }
    };
    form.append(message, submit, toggle);
    root.append(title, form);
  }
  const unsubscribe = session.subscribe(render);
  render();
  return () => {
    closed = true;
    unsubscribe();
    closeApp?.();
    root.replaceChildren();
  };
}
