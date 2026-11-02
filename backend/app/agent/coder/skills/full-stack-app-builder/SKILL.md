---
name: full-stack-app-builder
description: Build, test and connect FastAPI/SQLite APIs and React/Vite/TypeScript frontends, including backend-only or frontend-only apps. Vanilla TypeScript remains optional.
roles:
  - coder
enabled: true
---

# Full-Stack App Builder

For new frontends use frontend="react-vite" by default, with small TSX components and hooks. frontend="vite" is optional vanilla TypeScript only when requested, and frontend="none" is backend-only. Both scaffolds use local Tailwind CSS, never CDN assets. Existing apps keep their frameworks and styling unless the user requests a migration.

Load this skill only for relevant web projects. Preserve an existing repository's stack and package manager; do not replace it with this starter.

For a new empty CRUD project, use scaffold_crud_app and load crud-app-builder only if that tool is exposed. Otherwise use scaffold_project with backend="fastapi" or "none", frontend="react-vite" or "none", and a short project name. This minimal starter provides only runnable infrastructure: a health endpoint, a small App component, local Tailwind and an /api proxy. No persistence, CRUD, auth, helper layers or application tests are generated. Implement the user's requested app and isolated tests yourself. Keep the structure small; add files only when the implementation needs them. If the advanced tool is absent, do not load CRUD generation references or invent a call to it. Never scaffold over existing work.

Load references/fastapi.md only when implementing Python API/database behavior, and references/vite.md only for browser/UI work. Use the scaffold's compact project map, not a full-source reading tour. Generated CRUD should be checked before source exploration; a blank starter still needs implementation. For custom full-stack features, define the API contract, implement/test backend behavior, then implement frontend against the running backend's OpenAPI contract (check_preview /openapi.json, optional api_path). Verify integration afterward; establish a thin vertical slice early when the interface is uncertain. Reopen verified backend files only for a concrete requirement or failure. Prefer edit_file over full replacement and copy exact current text. read_file uses 1-based start_line/end_line and next_line, never character offsets. Your latest successful write is also a known snapshot.

Implement one complete user flow first: persisted data, a real endpoint, a browser request and visible result. Add tests for the actual requested behavior; unchanged starter health/API-helper/status tests are setup smoke tests, not proof of the generated application. Backend tests must exercise the new routes and persistence. Frontend DOM tests must mount the real app, trigger its controls, assert the request method/body, and assert the resulting loading/success/error UI. Handle successful empty/204 API responses as well as JSON. Handle invalid input and loading/empty/error states. Style with the selected local CSS framework and small custom CSS; never use CDN assets. Use another framework only when requested or already present.

Validate after the latest edit: backend tests with temporary databases; frontend typecheck/build and lightweight DOM interaction tests. Use run_checks for independent commands in one round; reuse current passing results. Load references/testing.md for test patterns and optional Playwright verification. Browser tests are opt-in, never a prerequisite for default completion. Start backend first, frontend last, with start_preview on ports 6969 and 6767 respectively. Preview commands are supervised and restored when the sandbox restarts, so replace the command for a port only when the project command changes. check_preview must verify an /api request through frontend port 6767 for a full-stack app. An API-only app uses port 6969; frontend-only apps can check / on 6767. Keep application data in the persistent workspace and test data isolated. Report exact scopes: application behavior tests, type/build, HTTP/API smoke, and optional real browser. Do not invent success.
