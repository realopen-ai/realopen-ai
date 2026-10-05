# Frontend

React + Vite + TypeScript interface for RealOpen-AI, with Tailwind styling, Zustand stores, React Router, Markdown/syntax highlighting, and xterm terminals.

## Layout

| Path              | Responsibility                                                         |
| ----------------- | ---------------------------------------------------------------------- |
| `src/App.tsx`     | Route registration and application entry layout                        |
| `src/components/` | Chat, Brain, Workspace, settings, voice, and terminal UI               |
| `src/store/`      | Conversations, generation, workspace, settings, and other shared state |
| `src/api/`        | HTTP clients and streaming transport                                   |
| `src/lib/`        | Shared rendering and interaction utilities                             |
| `src/i18n/`       | English, French, and Arabic translations with RTL support              |
| `tests/`          | Node test-runner tests for stores, clients, and UI-related logic       |
| `vite.config.ts`  | Development proxy, source alias, and build plugins                     |

## Run

The standard development flow is `make` from the repository root; Docker serves Vite at **http://localhost:5173**.

To run Vite directly against a reachable local backend, use Node.js **24** (matching CI):

```sh
cd frontend
npm ci
VITE_PROXY_TARGET=http://127.0.0.1:8000 npm run dev
```

Without an override, the development proxy targets `http://backend:8000`, the Docker service hostname. It forwards `/api`, `/metrics`, and `/ws`; `/api` also supports sandbox terminal WebSocket upgrades.

Production assets are built with `npm run build` and served by [Nginx](../nginx/README.md). `npm run preview` is a local static-build preview, not a replacement for the configured application gateway.

## Navigation and live state

Routes include conversations at `/:conversationId`, Brain pages under `/brain`, and Workspace pages under `/workspace`. Tabs such as `/brain/skills` and `/workspace/sandboxes` are addressable routes, not only local UI state.

Shared generation and voice state must survive in-app navigation. Text generation reconnects to backend SSE after a reload; a browser reload is not equivalent to preserving an active microphone/voice session. Do not tie request cancellation solely to a page component unmounting.

The workspace panel combines a resizable file tree, highlighted file view, agent terminal, independent user shell, and a separate app preview tab.

## Tests and formatting

```sh
npm test
npm run format:check
npm run build
```

From the repository root, use `make test-frontend` or `make test`. Tests use Node's built-in runner and TypeScript stripping. Store/API tests mock browser/provider boundaries; they do not start Ollama.

Use `npm run format` to apply Prettier. When adding UI text, update all three locale files. Keep code, terminal content, and English skill instructions left-to-right within Arabic layouts.

## Browser requirements

Voice needs microphone permission on localhost or HTTPS. Browser notifications need permission, and sound/background delivery depends on browser and OS policies. The app uses an in-app toast when focused and browser notifications for background completion, subject to those permissions.
