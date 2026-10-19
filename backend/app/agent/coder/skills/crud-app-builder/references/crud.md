# CRUD scaffolding

Use `scaffold_crud_app` only in an empty project whose requested stack fits. Keep explicit requirements; a scaffold choice is not permission to omit features or switch frameworks.

```json
{
  "name": "Inventory",
  "backend": "fastapi",
  "frontend": "react-vite",
  "styling": "tailwind",
  "entities": [{
    "name": "products",
    "fields": [
      {"name": "title", "type": "string"},
      {"name": "quantity", "type": "integer", "default": 0},
      {"name": "available", "type": "boolean", "default": true},
      {"name": "description", "type": "string", "required": false}
    ]
  }]
}
```

Names are lowercase snake_case; resource names are plural. `id` is automatic. Fields support string, integer, number and boolean; `required` defaults to true, optional fields permit null. Strings are trimmed and limited to 1,000 characters. Labels are optional display text. Supply defaults only when meaningful. Do not encode dates, relationships, enums or authentication as imaginary field types.

Backend-only: frontend="none". Frontend-only: backend="none" (explicit localStorage persistence, no fake API/backend). Full-stack always uses the real API and never falls back to localStorage on network failure.

The default frontend is react-vite: src/App.tsx, components/RecordForm.tsx, RecordList.tsx and Filters.tsx, hooks/useCrud.ts, main.tsx, shared types.ts/data.ts and React Testing Library tests in App.test.tsx. Keep markup in TSX and state in React; use targeted component/hook edits instead of constructing HTML strings or replacing the app with imperative DOM code. frontend="vite" explicitly selects the optional vanilla implementation. Existing workspaces are never migrated by scaffolding.

`styling` is `tailwind`, installed locally, never from a CDN. It uses the v4 Vite plugin and src/theme.css with semantic base styles, so generated forms are usable immediately. Extend with complete literal utility class names in TypeScript/HTML; avoid dynamically assembled class-name fragments that the compiler cannot detect. Existing projects keep their own styling unless the user requests a migration.

Output is normal editable code: per-resource schemas/routes in backend/resources/, shared repository.py and services.py, frontend React components (or crud.ts for vanilla), generated.ts and extensions.ts. GET /api/<resource> returns `{items,total,limit,offset}` and supports search/sort/descending/limit/offset; POST creates, PATCH updates only supplied fields, DELETE returns 204. Keep false, zero, empty optional values and omitted fields distinct. Read the generated schema and relevant source before editing; .realopen-app.json records the initial spec/version, not a command to overwrite later edits. Regeneration is intentionally refused.

CRUD includes create/edit/delete, boolean controls, search/sort/pagination, loading/error/empty states, the selected local styling and tests of actual generated forms/routes. Extra business rules, relationships, Auth and WebSockets are not implemented. For requested extra behavior, extend the generated app rather than reporting it as complete. services.py provides shared route dependencies and post-commit mutation listeners; extensions.ts provides a shared request transport, invalidation and cleanup. Future features should use these integration points, not fork combined templates.

After scaffolding, use the returned project map and run backend tests and frontend tests/typecheck/build with run_checks before exploring source. Generated behavior tests count as application tests when actually run, but add cases for custom requirements. Do not read the entire scaffold. Use edit_targets for targeted inspection; start previews with .realopen-project.json commands and read the running backend's /openapi.json using check_preview, optionally api_path for one resource. Implement and verify custom backend behavior first, then consume that contract in the frontend and smoke-test the API through the frontend proxy. Return to backend only for concrete failures or requirements. For uncertain integration, prove a thin vertical slice early. Report DOM/API/build verification separately from real-browser verification.
