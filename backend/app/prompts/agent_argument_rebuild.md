ARGUMENT REBUILD ROUND — overrides the normal tool-output protocol for this round only.
The previous call to {tool} was rejected. Rebuild its arguments from the user's request and relevant context, not from the rejected JSON. No new task is being requested. Preserve already successful actions; do not execute them again.
Purpose: {purpose}
Validation feedback: {errors}
Required shape: {shape}
Shape markers are instructions, NEVER values to copy. An array example shows one item, not the requested item count. Conditional fields apply only to the matching type. Omit irrelevant optional fields; never invent source IDs or missing facts.
Authoritative argument schema: {schema}
Return exactly one JSON object: {{"tool":"{tool}","args":{{...}}}}. No native function call, code fence, explanation, or answer text in this repair round. Use real arrays/objects, not JSON-encoded strings. Fill every applicable required field and honor the requested count. Keep strings properly JSON-escaped. All normal validation still applies before execution.
