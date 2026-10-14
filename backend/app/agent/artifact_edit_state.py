"""Request-local read receipts for safe agent edits; never infer section IDs."""

import json
import uuid


class ArtifactEditState:
    def __init__(self):
        self.reads = {}

    def record_read(self, output):
        try:
            data = json.loads(output)
            key = (str(uuid.UUID(data["artifact_id"])), data["version"])
            if type(key[1]) is not int or key[1] < 1:
                return
            receipt = self.reads.setdefault(key, {"sections": set(), "content": set()})
            if isinstance(data.get("sections"), list):
                receipt["sections"].update(
                    item["id"]
                    for item in data["sections"]
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                )
            elif isinstance(data.get("id"), str) and isinstance(
                data.get("content"), str
            ):
                receipt["sections"].add(data["id"])
                receipt["content"].add(data["id"])
        except (ValueError, TypeError, KeyError, AttributeError):
            return

    def missing_read(self, arguments):
        try:
            identifier = str(uuid.UUID(arguments.get("artifact_id")))
        except (ValueError, TypeError, AttributeError):
            return "The target artifact has not been successfully read in this request."
        version = arguments.get("expected_version")
        if type(version) is not int:
            return (
                "Read the artifact to obtain its exact current version and section IDs."
            )
        receipt = self.reads.get((identifier, version))
        if receipt is None:
            return (
                "No successful read exists for this artifact at the requested version."
            )
        changes = arguments.get("changes")
        if not isinstance(changes, list):
            return None  # Normal schema validation handles malformed changes.
        for change in changes:
            if not isinstance(change, dict):
                continue
            section = change.get("section_id")
            if not isinstance(section, str) or section not in receipt["sections"]:
                return "A target section ID was not returned by a successful read of this artifact/version."
            if (
                change.get("operation", "replace") != "delete"
                and section not in receipt["content"]
            ):
                return "Read each section's content before providing its complete replacement."
        return None

    def invalidate(self, identifier):
        try:
            identifier = str(uuid.UUID(identifier))
        except (ValueError, TypeError, AttributeError):
            return
        self.reads = {
            key: value for key, value in self.reads.items() if key[0] != identifier
        }
