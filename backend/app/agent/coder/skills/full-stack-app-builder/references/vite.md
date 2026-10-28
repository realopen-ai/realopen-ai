# React / Vite / TypeScript and local styling

The starter uses /workspace/frontend. Dependencies and styles are local. Commands from /workspace: `bun run --cwd frontend build`; start_preview uses `bun run --cwd frontend dev -- --host 0.0.0.0 --port 6767`.

Choose frontend="react-vite" by default. The entrypoint is src/main.tsx; edit src/App.tsx or focused components/hooks. Keep JSX in .tsx files, use useState for UI state and useEffect with cleanup for loading, and use React handlers rather than document.createElement/innerHTML. The React Vite plugin, TSX configuration, React types and React Testing Library are installed. The optional frontend="vite" selection is vanilla TypeScript; preserve it when explicitly requested or already present.

Both scaffolds use local Tailwind CSS. src/theme.css supplies the theme and src/style.css handles app layout. Preserve existing projects' styling rather than automatically migrating them. No CDN scripts/styles or extra component library is required.

Tailwind v4 uses locally installed `tailwindcss` and `@tailwindcss/vite`, the `tailwindcss()` Vite plugin and `@import "tailwindcss"` in theme.css. It includes semantic base styles for usable forms; add complete utility classes such as `grid gap-4 md:grid-cols-2` in source for custom design. Do not assemble partial class names dynamically or add obsolete v3 configuration/directives. Preserve the generated Vite proxy and plugin when editing config. Behavior still needs TypeScript.

The starter request helper is in src/api.ts. Use relative /api paths; vite.config.ts proxies them to backend port 6969. Match the actual backend response shape. A focused React list component can render fetched data declaratively:

```tsx
type Item = { id: number; title: string };
export function ItemList({ items }: { items: Item[] }) {
  return <ul>{items.map((item) => <li key={item.id}>{item.title}</li>)}</ul>;
}
```

React escapes user text in JSX; never use dangerouslySetInnerHTML for user data. Vanilla apps use textContent, not innerHTML. Use labels, semantic buttons, loading and error status messages. Catch request failures. When sending JSON, include Content-Type: application/json. Do not paste answer prose into source or keep rewriting entire files for one correction. Read exact text and use edit_file.

The starter includes Vitest with happy-dom (no Chromium). Run `bun run --cwd frontend test`, `bun run --cwd frontend typecheck`, and `bun run --cwd frontend build`, preferably batched with run_checks. Add application-specific DOM tests; the starter's helper/status tests are only examples. Exercise the real rendered controls and handlers, assert the request method/body and resulting UI, and test success/error/finally cleanup. DOM tests mock fetch; backend tests separately verify real persistence. Neither is a real-browser test.

Start both services and use check_preview at port 6767 with the application's /api route to verify the proxy/API connection. Load references/testing.md for focused testing examples or when optional browser verification is requested.
