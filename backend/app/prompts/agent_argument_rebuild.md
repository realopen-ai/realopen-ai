ARGUMENT REBUILD ROUND — overrides the normal tool-output protocol for this round only.
The previous call to {tool} was rejected. Rebuild its arguments from the user's request and relevant context, not from the rejected JSON. No new task is being requested. Preserve already successful actions; do not execute them again.
Purpose: {purpose}
Validation feedback: {errors}
Required shape: {shape}
Conditional examples (use only the variant matching each item's type): {conditional_shapes}
Shape markers are instructions, NEVER values to copy. An array example shows one item, not the requested item count. Conditional fields apply only to the matching type. Omit irrelevant optional fields; never invent source IDs or missing facts.
Authoritative argument schema: {schema}
Return exactly one bare JSON object: {{"tool":"{tool}","args":{{...}}}}. No native function call, outer Markdown fence, explanation, or answer text in this repair round. Markdown code blocks remain allowed INSIDE string values when that field supports Markdown. Use real arrays/objects, not JSON-encoded strings. Fill every applicable required field and honor the requested count. Encode real line breaks once in JSON; escape double quotes and backslashes, not apostrophes. All normal validation still applies before execution.
