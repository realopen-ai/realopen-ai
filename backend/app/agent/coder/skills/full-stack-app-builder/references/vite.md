# Vite vanilla TypeScript / Pico CSS reference

The starter uses /workspace/frontend. Dependencies and styles are local. Commands from /workspace: `bun run --cwd frontend build`; start_preview uses `bun run --cwd frontend dev -- --host 0.0.0.0 --port 6767`.

Pico styles semantic HTML (button, input, label, article, table, dialog). It is CSS, not a JavaScript component framework: wire behavior with TypeScript. Import `@picocss/pico/css/pico.min.css` locally and use custom style.css for layout. No CDN scripts/styles, React or Tailwind are required.

The starter request helper is in src/api.ts. Use relative /api paths; vite.config.ts proxies them to backend port 6969. Match the actual backend response shape:

```typescript
import { request } from "./api";
type Item = { id: number; title: string };
const items = await request<Item[]>("/api/items");
const list = document.querySelector<HTMLUListElement>("#items")!;
for (const item of items) {
  const row = document.createElement("li");
  row.textContent = item.title;
  list.append(row);
}
```

Render user text with textContent, not innerHTML. Use labels, semantic buttons, loading and error status messages. Catch request failures. When sending JSON, include Content-Type: application/json. Do not paste answer prose into source or keep rewriting entire files for one correction. Read exact text and use edit_file.

The starter includes Vitest with happy-dom (no Chromium). Run `bun run --cwd frontend test`, `bun run --cwd frontend typecheck`, and `bun run --cwd frontend build`, preferably batched with run_checks. Add application-specific DOM tests; the starter's helper/status tests are only examples. Exercise the real rendered controls and handlers, assert the request method/body and resulting UI, and test success/error/finally cleanup. DOM tests mock fetch; backend tests separately verify real persistence. Neither is a real-browser test.

Start both services and use check_preview at port 6767 with the application's /api route to verify the proxy/API connection. Load references/testing.md for focused testing examples or when optional browser verification is requested.
