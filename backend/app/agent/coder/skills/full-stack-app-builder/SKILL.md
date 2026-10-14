---
name: Full-Stack App Builder
description: Build, test and connect FastAPI/SQLite APIs and Vite vanilla TypeScript frontends, including backend-only or frontend-only apps.
roles:
  - coder
enabled: true
---

# Full-Stack App Builder

Load this skill only for relevant web projects. Preserve an existing repository's stack and package manager; do not replace it with this starter.

For a new empty project, use scaffold_project with backend="fastapi" or "none", frontend="vite" or "none", and a short project name. It provides local dependencies, SQLite connection helpers, backend health tests and a frontend API proxy. Read the generated files before extending them. The starter is not the user's finished application.

Load references/fastapi.md only when implementing Python API/database behavior, and references/vite.md only for browser/UI work. Prefer edit_file over full replacement and copy exact text from read_file. Follow pagination; never guess missing source.

Implement one complete user flow first: persisted data, a real endpoint, a browser request and visible result. Add backend tests for the actual application, not only starter health. Handle invalid input and loading/empty/error states. Style with locally installed Pico CSS and small custom CSS; never use CDN assets. Use another framework only when requested or already present.

Validate after the latest edit: run the project's backend tests and frontend build. Start backend first, frontend last, with start_preview on ports 6969 and 6767 respectively. check_preview must verify an /api request through frontend port 6767 for a full-stack app. An API-only app uses port 6969; frontend-only apps can check / on 6767. Keep data in the persistent workspace. Report exact checks and unresolved blockers; do not invent success.
