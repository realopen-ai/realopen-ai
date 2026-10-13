# Agent trace schema changelog

This folder documents the persisted JSONL format, independently of the [usage guide](../agent-traces.md). These references describe captured observations, not correctness labels or training examples.

Read the version matching a record's `schema_version`. Fields may be absent when execution ends before their capture point. Redaction, truncation, and omission annotations apply in every version. Existing records are not migrated or rewritten.

## 1.2 — Separate coder trajectories

[Full reference](v1.2.md)

- Adds `agent`, `parent_trace_id`, and `parent_tool_call_id` to distinguish and link trajectories.
- Adds coder task/workspace metadata and per-round `model_attempts`, including native-call failures and JSON recovery.
- Adds tool `origin` to distinguish model-selected execution from application-triggered execution.
- Adds `tool_call_id` where assigned by the application, making parent delegation links resolvable within traces.
- Stores coder records and referenced prompts under `coder/`, separately from general-agent files.

Compatibility: older records without `agent` are general-agent traces. Coder discovery requires reading the `coder/` subdirectory; linked parent and child records have independent outcomes. Existing inference and outcome meanings remain unchanged.

## 1.1 — Resolved inference settings

[Full reference](v1.1.md)

- Separates explicitly requested settings into `inference.requested`.
- Resolves `think`, `temperature`, and `max_output_tokens` from the provider request or exposed local model defaults.
- Adds `inference.sources` to explain where resolved values came from.
- Uses `provider_default` for unexposed defaults and `provider_managed` for thinking controlled by the provider adapter, rather than treating unknown values as numeric defaults.
- Adds `inference.thinking_observed` and per-round `model_defaults` metadata.

Compatibility: consumers must accept string sentinels as well as numeric/boolean settings. A missing setting in older records cannot be reconstructed from later metadata. Top-level inference describes the first round; later rounds retain their own settings.

## 1.0 — Initial trace format

[Full reference](v1.0.md)

Introduces execution metadata, captured model context, prompt references, rounds, parsing diagnostics, tool execution records, and termination outcomes. Inference settings reflect supplied values; omitted settings can be `null` without revealing the provider's effective default.
