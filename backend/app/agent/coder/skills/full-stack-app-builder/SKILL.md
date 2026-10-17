---
name: full-stack-app-builder
description: Build, test and connect FastAPI/SQLite APIs and Vite vanilla TypeScript frontends, including backend-only or frontend-only apps.
roles:
  - coder
enabled: true
---

# Full-Stack App Builder

Load this skill only for relevant web projects. Preserve an existing repository's stack and package manager; do not replace it with this starter.

For a new empty project, use scaffold_project with backend="fastapi" or "none", frontend="vite" or "none", and a short project name. It provides local dependencies, SQLite connection helpers, backend health tests and a frontend API proxy. Read the generated files before extending them. The starter is not the user's finished application.

Load references/fastapi.md only when implementing Python API/database behavior, and references/vite.md only for browser/UI work. Prefer edit_file over full replacement and copy exact current text. read_file uses 1-based start_line/end_line and next_line, never character offsets. Your latest successful write is also a known snapshot.

Implement one complete user flow first: persisted data, a real endpoint, a browser request and visible result. Add tests for the actual requested behavior; unchanged starter health/API-helper/status tests are setup smoke tests, not proof of the generated application. Backend tests must exercise the new routes and persistence. Frontend DOM tests must mount the real app, trigger its controls, assert the request method/body, and assert the resulting loading/success/error UI. Handle successful empty/204 API responses as well as JSON. Handle invalid input and loading/empty/error states. Style with locally installed Pico CSS and small custom CSS; never use CDN assets. Use another framework only when requested or already present.

Validate after the latest edit: backend tests with temporary databases; frontend typecheck/build and lightweight DOM interaction tests. Use run_checks for independent commands in one round; reuse current passing results. Load references/testing.md for test patterns and optional Playwright verification. Browser tests are opt-in, never a prerequisite for default completion. Start backend first, frontend last, with start_preview on ports 6969 and 6767 respectively. Preview commands are supervised and restored when the sandbox restarts, so replace the command for a port only when the project command changes. check_preview must verify an /api request through frontend port 6767 for a full-stack app. An API-only app uses port 6969; frontend-only apps can check / on 6767. Keep application data in the persistent workspace and test data isolated. Report exact scopes: application behavior tests, type/build, HTTP/API smoke, and optional real browser. Do not invent success.
