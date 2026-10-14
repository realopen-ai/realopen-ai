# Local agent execution traces

Tracing is **disabled by default** and separate from debug logging. It records text/voice executions of the main agent loop, including conversation and injected memory context, model reasoning, tools, results, and incomplete executions. Each coder delegation has a separate trace of its internal rounds and workspace tools, linked to its parent execution.

These are raw observations for future analysis, not training datasets or correctness evaluations.

## Enable and inspect

For Docker, set the following in `.env` and restart the backend:

```env
AGENT_TRACE_ENABLED=true
AGENT_TRACE_DIR=/app/data/agent_traces
```

Docker's existing `./data:/app/data` mount persists traces on the host at `data/agent_traces/`; no new volume is needed. A custom container directory needs a persistent mount.

Send a request and wait for completion, then run from the repository root:

```sh
python3 scripts/inspect-agent-traces.py --dir data/agent_traces list --limit 10
python3 scripts/inspect-agent-traces.py --dir data/agent_traces show TRACE_UUID
python3 scripts/inspect-agent-traces.py --dir data/agent_traces show TRACE_UUID --json
python3 scripts/inspect-agent-traces.py --dir data/agent_traces --agent coder list --limit 10
python3 scripts/inspect-agent-traces.py --dir data/agent_traces --agent all list --limit 10
```

The CLI runs on the host and uses Python 3's standard library only. It displays persisted rounds, attempted calls, validation/errors, durations, and termination status; `--json` prints the full record without expanding prompt references.

By default it reads general-agent traces. Use `--agent coder` to inspect coder traces or `--agent all` to read both. Pass the same option when showing a coder trace by ID.

Disable with `AGENT_TRACE_ENABLED=false` and restart. Existing files are not deleted.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `AGENT_TRACE_ENABLED` | `false` | Opt in independently of debug logging. |
| `AGENT_TRACE_DIR` | `/app/data/agent_traces` | Container directory, persisted at the repository's root-level `data/agent_traces/` through the existing mount. |
| `AGENT_TRACE_MAX_TOOL_RESULT_CHARS` | `16000` | Character limit for captured tool results, including forwarded result context. |
| `AGENT_TRACE_MAX_TEXT_CHARS` | `256000` | Character limit for other captured text fields. |
| `AGENT_TRACE_QUEUE_SIZE` | `64` | Maximum pending traces before new traces are dropped. |

Limits apply to individual captured values, not the entire trace. Truncated values are explicitly annotated. Applied minimums are 1 result character, 1024 text characters, and 1 queue entry.

## Privacy

Enabling tracing stores conversation content, retrieved memories, model reasoning, and tool outputs locally. No trace upload, telemetry, or dataset sharing is implemented.

Known secret fields, configured/environment secret values, credentialed URLs, bearer tokens, and labeled secrets are redacted before persistence. Binary/base64 assets are omitted. Redaction is defense-in-depth, not complete anonymization: arbitrary personal information or unlabeled secrets can remain. Review and sanitize before any later dataset use. Files are created with private permissions; restrict access to the host directory and backups.

## Storage and schema

Versioned field references and compatibility notes are in the [schema changelog](agent-trace-schema/CHANGELOG.md).

- Each append-only JSONL record represents one execution and includes a `schema_version`. Files are grouped by UTC start date (`YYYY-MM-DD.jsonl`). Existing records are never rewritten when the schema changes.
- General-agent records remain in the configured directory; coder records and their prompt references live in its `coder/` subdirectory. `agent` identifies the trajectory type. Coder records carry `parent_trace_id` and `parent_tool_call_id` when delegated, plus available task/workspace IDs. The parent's delegation result remains in its own trace.
- `trace_id`, timestamps, conversation identity, model/profile, inference settings, and round limit are execution metadata.
- `request` captures the original request/history and attachment counts; each round captures actual post-compaction input messages, dynamic context, exposed native schemas, and provider settings. Text-only tool instructions are preserved in the captured application-protocol context; `available_tools` records the exposed names.
- `runtime_usage`, when reported by the provider, retains token counts and prefill/generation timings. A missing cached-token count is unknown, not zero. Application prefix hashes help compare requests but are not runtime KV-cache keys. A tool's `model_view` distinguishes a shortened inference result from its raw `output`; existing redaction/size limits apply to both.
- System prompts are redacted and content-addressed in `prompts/<sha256>.json`. References reconstruct the captured prompt, not its original unredacted content. Keep this directory with the JSONL files. Prompts are published atomically under a file lock; existing prompt files are preserved.
- `model_response` contains streamed content, reasoning, and raw normalized native calls (including raw argument strings). Parsing diagnostics preserve invalid JSON candidates/arguments; malformed provider frames are retained when the adapter rejects them. `parsed_tool_calls` records what the existing parser accepted, including empty arguments substituted by that parser.
- Coder rounds also record `model_attempts`: exact captured inputs/schemas, raw normalized responses or provider errors, inference metadata, and elapsed time for both the native call and any JSON recovery call. Recovery output is preserved before conversion into tool calls. Application-triggered tools are distinguished by `origin`.
- `tool_calls` records attempted arguments, effective arguments, status, output/error, and elapsed time. Tool-owned validation is reported as `not_observed`, `tool_returned_success`, or `reported_validation_error` when explicitly reported as invalid arguments; tracing does not introduce a new schema validator or claim to distinguish every tool error from validation errors. Unparsed JSON tool candidates are counted separately in `unparsed_tool_attempts` and included in the attempted-call total, without claiming they executed.
- `outcome` records termination status/reason, round and tool-call counts, interrupted/skipped calls, duration, and final assistant content. `final_response_source` distinguishes model content from an application-provided response, such as the maximum-round fallback. `quality_evaluated` is always false. Completion is **not** a correctness label.
- Durations use monotonic clocks. UTC timestamps are for ordering, not duration subtraction. Large text/results are explicitly marked truncated; exact reconstruction is unavailable where data was redacted/omitted/truncated.
- Readers should use each record's `schema_version` to interpret its fields. Trace collection does not migrate records or transform them into datasets.

### Inference settings

`inference.requested` preserves explicitly supplied settings; omitted values can legitimately be `null`. Resolved `think`, `temperature`, and `max_output_tokens` fields include their `sources`: request payload first, then local model metadata. Each round records its own model/settings; execution-level inference reflects the first round.

When tracing is enabled, a trace-only local `/api/show` lookup retrieves model defaults using a short timeout and cached results. `model_defaults` records the lookup result, model modification timestamp when available, and fetch time. Disabled tracing makes no additional lookup, and inference payloads are never changed.

Unexposed defaults are labeled `provider_default` with source `not_exposed`, not guessed numbers. This includes `max_output_tokens` when neither the request nor metadata specifies a limit. `thinking_observed` records emitted reasoning; `false` does not prove thinking was disabled. The Groq adapter does not forward the requested `think` flag, so its resolved value is `provider_managed`.

### Termination statuses

| Status | Meaning |
| --- | --- |
| `completed` | The agent loop completed normally; content quality was not evaluated. |
| `max_rounds_reached` | The permitted round budget was exhausted. |
| `cancelled` | Execution was cancelled or its generator closed. |
| `failed` | An execution/provider error ended the request. |
| `shutdown` | An active execution was snapshotted during normal application shutdown. |

## Reliability and retention

A bounded in-process queue writes using a worker and off-event-loop disk I/O. POSIX file locking on the supported Linux/Docker and macOS backends prevents concurrent workers from interleaving records; an unsupported locking platform disables persistence gracefully. Referenced prompts are published atomically under the same lock. Errors/drop warnings contain no trace content and do not fail inference. Normal shutdown snapshots any still-active executions with status `shutdown` and drains pending writes, without cancelling agent tasks itself. Cancellation and generator closure finalize partial traces. Abrupt process kill/crash before enqueue or flush can lose an active execution; there are no per-token disk checkpoints. A full queue drops a trace rather than blocking the agent.

Daily rotation is automatic. There is no automatic deletion/retention policy; inspect disk usage and archive/remove files deliberately. Preserve referenced prompts when archiving. Trace directories are excluded from Git. No external service/dependency or background infrastructure is required.

## Troubleshooting

- **No record yet:** traces are queued when execution ends, not written per token. Wait for completion, or test cancellation using the application's Stop button. Browser disconnection alone should not end a backend-owned execution.
- **No files after completion:** verify tracing is enabled in the running backend, restart after configuration changes, check the storage path/mount and permissions, and look for trace persistence or queue-drop warnings in backend logs.
- **Settings look unknown:** `null` under `requested` means omitted; `provider_default` means the provider did not expose a value. Existing records retain the information captured at execution time.
- **Missing context:** check truncation/omission annotations. Redaction and size limits intentionally prevent exact reconstruction of every input.

The CLI does not run models, evaluate correctness, or construct training datasets. Fine-tuning, teacher correction, and benchmarking remain outside this implementation.
