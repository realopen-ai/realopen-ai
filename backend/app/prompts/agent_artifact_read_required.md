This update_artifact call was NOT executed. Its read prerequisite is unmet: {reason}

Call read_artifact for the target artifact first. Omit section_id and version to read the latest outline. Use the returned version as expected_version and copy exact section IDs from the read result; never infer IDs from headings or guess a version.
For deletion, a successful outline read containing the target ID is sufficient. Before replacing content, also call read_artifact with that section_id and version to read its content (paginate when needed), preserving unrelated content.
Loading a tool schema, listing files, summaries, previous conversation text, and generated output receipts do not count as reading. After an update creates a new version, read that version again before further edits. If read_artifact is disabled or unavailable, explain that editing cannot safely proceed; do not bypass the prerequisite with another tool.
