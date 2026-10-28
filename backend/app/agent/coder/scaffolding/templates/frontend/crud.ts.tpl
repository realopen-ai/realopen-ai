import type { AppContext, Mutation } from "./extensions";
import type { AppConfig, Entity, Row, Page, Values, Query } from "./types";
import { createCrudContext, label } from "./data";
export { createCrudContext } from "./data";
export type { AppConfig, Entity } from "./types";

function node<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  return element;
}

export function mountCrud(
  root: HTMLElement,
  config: AppConfig,
  context = createCrudContext(config),
) {
  let entity = config.entities[0]!,
    editing: Row | null = null;
  let query: Query = {
    search: "",
    sort: "id",
    descending: false,
    limit: 25,
    offset: 0,
  };
  let page: Page = { items: [], total: 0, limit: 25, offset: 0 };
  let busy = false,
    disposed = false,
    invalidated = false;
  let status: HTMLElement;
  const path = () => "/api/" + entity.name;
  const headers = { "Content-Type": "application/json" };

  function pending(value: boolean) {
    busy = value;
    root.setAttribute("aria-busy", String(value));
    root
      .querySelectorAll<
        HTMLInputElement | HTMLButtonElement | HTMLSelectElement
      >("button,input,select")
      .forEach((control) => {
        control.disabled = value || control.dataset.unavailable === "true";
      });
  }

  async function reload(): Promise<void> {
    const params = new URLSearchParams(
      Object.entries(query).map(([key, value]) => [key, String(value)]),
    );
    page = await context.request<Page>(path() + "?" + params);
    if (page.items.length === 0 && page.total > 0 && query.offset > 0) {
      query.offset = Math.max(0, query.offset - query.limit);
      return reload();
    }
    if (!disposed) render();
  }

  async function action(work: () => Promise<void>, message = "") {
    if (busy || disposed) return;
    pending(true);
    status.textContent = "Loading…";
    status.className = "";
    try {
      await work();
      if (!disposed) status.textContent = message;
    } catch (error) {
      if (!disposed) {
        status.textContent =
          error instanceof Error ? error.message : "Request failed";
        status.className = "error";
      }
    } finally {
      if (!disposed) {
        pending(false);
        if (invalidated) {
          invalidated = false;
          void action(reload);
        }
      }
    }
  }

  async function changed(event: Mutation) {
    for (const listener of context.mutations) {
      try {
        await listener(event);
      } catch (error) {
        console.warn("Mutation listener failed", error);
      }
    }
  }

  function button(text: string, work: () => void) {
    const result = node("button", text);
    result.type = "button";
    result.addEventListener("click", work);
    return result;
  }

  function render() {
    root.replaceChildren();
    const title = node("h1", config.name);
    const storage = node(
      "small",
      config.local
        ? "Saved in this browser"
        : "Saved to the application database",
    );
    const navigation = node("nav");
    navigation.setAttribute("aria-label", "Resources");
    for (const candidate of config.entities) {
      const tab = button(label(candidate), () => {
        if (busy) return;
        entity = candidate;
        editing = null;
        query = { ...query, search: "", sort: "id", offset: 0 };
        page = { items: [], total: 0, limit: 25, offset: 0 };
        render();
        void action(reload);
      });
      tab.className = candidate === entity ? "" : "outline";
      tab.setAttribute("aria-current", String(candidate === entity));
      navigation.append(tab);
    }
    status = node("p");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const tools = node("form");
    tools.className = "toolbar";
    const search = node("input");
    search.type = "search";
    search.value = query.search;
    search.placeholder = "Search…";
    search.setAttribute("aria-label", "Search records");
    const sort = node("select");
    sort.setAttribute("aria-label", "Sort by");
    for (const field of [{ name: "id", label: "ID" }, ...entity.fields]) {
      const option = node("option", label(field));
      option.value = field.name;
      sort.append(option);
    }
    sort.value = query.sort;
    tools.append(search, sort);
    const searchButton = node("button", "Search");
    searchButton.type = "submit";
    tools.append(searchButton);
    tools.addEventListener("submit", (event) => {
      event.preventDefault();
      query = { ...query, search: search.value, sort: sort.value, offset: 0 };
      void action(reload);
    });
    const direction = button(
      query.descending ? "Descending" : "Ascending",
      () => {
        query.descending = !query.descending;
        void action(reload);
      },
    );
    tools.append(direction);
    const editor = node("form");
    editor.setAttribute(
      "aria-label",
      editing ? "Edit record" : "Create record",
    );
    editor.append(node("h2", editing ? "Edit record" : "New record"));
    const inputs = new Map<string, HTMLInputElement>();
    for (const field of entity.fields) {
      const wrapper = node("label");
      wrapper.textContent = label(field);
      const input = node("input");
      input.name = field.name;
      input.type =
        field.type === "boolean"
          ? "checkbox"
          : field.type === "string"
            ? "text"
            : "number";
      input.step = field.type === "integer" ? "1" : "any";
      input.required = field.required && field.type !== "boolean";
      if (field.type === "string") input.maxLength = 1000;
      const value = editing ? editing[field.name] : field.default;
      if (field.type === "boolean") {
        input.checked = value === true;
        input.indeterminate = value == null && !field.required;
      } else input.value = value == null ? "" : String(value);
      wrapper.append(input);
      editor.append(wrapper);
      inputs.set(field.name, input);
    }
    const save = node("button", editing ? "Save changes" : "Add record");
    save.type = "submit";
    editor.append(save);
    if (editing)
      editor.append(
        button("Cancel edit", () => {
          editing = null;
          render();
        }),
      );
    editor.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!editor.reportValidity()) return;
      const values: Values = {};
      for (const field of entity.fields) {
        const input = inputs.get(field.name)!;
        values[field.name] =
          field.type === "boolean"
            ? input.indeterminate
              ? null
              : input.checked
            : !input.value.trim() && !field.required
              ? null
              : field.type === "string"
                ? input.value.trim()
                : Number(input.value);
      }
      const id = editing?.id;
      void action(async () => {
        const record = await context.request<Row>(
          path() + (id === undefined ? "" : `/${id}`),
          {
            method: id === undefined ? "POST" : "PATCH",
            headers,
            body: JSON.stringify(values),
          },
        );
        editing = null;
        await changed({
          entity: entity.name,
          action: id === undefined ? "created" : "updated",
          id: record.id,
        });
        await reload();
      }, "Saved");
    });
    const list = node("section");
    list.setAttribute("aria-label", "Records");
    if (!page.items.length) list.append(node("p", "No records yet."));
    for (const row of page.items) {
      const card = node("article");
      card.dataset.id = String(row.id);
      card.append(node("h3", `#${row.id}`));
      const details = node("dl");
      for (const field of entity.fields) {
        details.append(node("dt", label(field)));
        const value = node("dd");
        if (field.type === "boolean") {
          const toggle = node("input");
          toggle.type = "checkbox";
          toggle.checked = row[field.name] === true;
          toggle.indeterminate = row[field.name] === null;
          toggle.setAttribute(
            "aria-label",
            `${label(field)} for record ${row.id}`,
          );
          toggle.addEventListener("change", () => {
            void action(async () => {
              await context.request(path() + `/${row.id}`, {
                method: "PATCH",
                headers,
                body: JSON.stringify({ [field.name]: toggle.checked }),
              });
              await changed({
                entity: entity.name,
                action: "updated",
                id: row.id,
              });
              await reload();
            }, "Saved").then(() => {
              if (toggle.isConnected) {
                toggle.checked = row[field.name] === true;
                toggle.indeterminate = row[field.name] === null;
              }
            });
          });
          value.append(toggle);
        } else
          value.textContent =
            row[field.name] == null ? "—" : String(row[field.name]);
        details.append(value);
      }
      card.append(
        details,
        button("Edit", () => {
          editing = row;
          render();
        }),
      );
      const remove = button("Delete", () => {
        void action(async () => {
          await context.request<void>(path() + `/${row.id}`, {
            method: "DELETE",
          });
          if (editing?.id === row.id) editing = null;
          await changed({ entity: entity.name, action: "deleted", id: row.id });
          await reload();
        }, "Deleted");
      });
      remove.className = "outline secondary";
      card.append(remove);
      list.append(card);
    }
    const pagination = node("nav");
    pagination.setAttribute("aria-label", "Pagination");
    const previous = button("Previous", () => {
      query.offset = Math.max(0, query.offset - query.limit);
      void action(reload);
    });
    const next = button("Next", () => {
      query.offset += query.limit;
      void action(reload);
    });
    previous.dataset.unavailable = String(query.offset === 0);
    next.dataset.unavailable = String(query.offset + query.limit >= page.total);
    previous.disabled = query.offset === 0;
    next.disabled = query.offset + query.limit >= page.total;
    pagination.append(
      previous,
      node(
        "span",
        `${page.total ? query.offset + 1 : 0}–${Math.min(query.offset + query.limit, page.total)} / ${page.total}`,
      ),
      next,
    );
    root.append(
      title,
      storage,
      navigation,
      status,
      tools,
      editor,
      list,
      pagination,
    );
    pending(busy);
  }
  // WS-style plugins can invalidate the current page without owning CRUD UI.
  context.invalidate = async () => {
    if (busy) {
      invalidated = true;
      return;
    }
    await action(reload);
  };
  render();
  void action(reload);
  return () => {
    disposed = true;
    context.invalidate = async () => {};
    root.replaceChildren();
  };
}
