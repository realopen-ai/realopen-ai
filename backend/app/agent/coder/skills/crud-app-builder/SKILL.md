---
name: crud-app-builder
description: Scaffold and refine CRUD apps with FastAPI/SQLite APIs and React/Vite/TypeScript UI, or either alone. Includes real component and API tests; vanilla TypeScript is optional.
roles:
  - coder
enabled: true
---

# CRUD App Builder

Use frontend="react-vite" by default: editable React TSX components and hooks, not imperative HTML/DOM construction. frontend="vite" is optional vanilla TypeScript only when requested; frontend="none" is backend-only. Both tools use local Tailwind CSS. Keep existing projects' frameworks and styling unchanged unless the user requests a migration.

For a new empty CRUD project matching FastAPI/SQLite and/or React/Vite/TypeScript, use `scaffold_crud_app` rather than rebuilding routing, forms and persistence by hand. Read references/crud.md for the small specification, generated file layout and limitations. Inspect existing files first; never scaffold over user work or replace an explicitly requested framework.

The tool generates an editable baseline, not a guarantee that every requirement is implemented. First use its compact project map and run generated checks, not a full-file reading tour. Check explicit requirements against the result, add custom rules or UI only where needed, and keep working generated source. Use the map's edit targets for focused inspection. For custom full-stack requirements, implement/test backend behavior, inspect its runtime OpenAPI contract with check_preview (/openapi.json, optional api_path), then focus on frontend and integration. Prove a thin vertical slice early for uncertain interfaces; backend fixes remain allowed when justified. No Auth, WebSocket server, relationships or file uploads are generated yet. Do not promise these features merely because extension hooks exist.

Validate the generated application: backend tests, frontend DOM tests, typecheck/build and real HTTP through the preview proxy. The generated resource/form tests count as behavior tests only after passing; add tests for custom requirements. Use `run_checks` for independent commands. Start supervised backend/frontend previews from .realopen-project.json. If useful, load the full-stack-app-builder skill's references/testing.md, fastapi.md or vite.md; avoid loading unrelated examples. Real-browser testing remains optional and must be reported separately.

Do not import unrelated cloud orchestration or install external plugin runtimes.
