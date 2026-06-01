#!/usr/bin/env python3
"""
Helper script to parse profiles.yml from bash.
Called by setup.sh — NOT meant for direct use.

Usage:
    python3 scripts/profile-helper.py <profile> <command>

Commands:
    default-model    Print the default model ID for a profile
    all-models       Print all model IDs for a profile (one per line)
    models-json      Print all models for a profile as JSON
    description      Print the profile description
    label            Print the human-readable profile label
    engine           Print the inference engine for the profile
    validate         Check that profiles.yml is valid
"""

import sys
import json
from pathlib import Path

# Minimal YAML parser — no external dependency needed
# Supports: nested dicts, lists, basic scalars, multiline strings
# This is intentionally simple; profiles.yml doesn't use advanced YAML features.

# Valid profile names — used for validation
VALID_PROFILES = {
    "cpu_small",
    "cpu_medium",
    "nvidia_small",
    "nvidia_medium",
    "nvidia_large",
    "nvidia_xlarge",
    "apple_small",
    "apple_medium",
    "apple_large",
    "apple_xlarge",
}


def parse_yaml_simple(text: str) -> dict:
    """Parse a subset of YAML sufficient for profiles.yml."""
    result = {}
    # Stack stores tuples: (current_dict_or_list, indent_level)
    stack = [(result, -1)]

    for raw_line in text.split("\n"):
        stripped = raw_line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())
        content = stripped.strip()

        # 1. Pop stack until we find the correct parent scope
        # We pop while the stack top is at an equal or deeper indent level
        while len(stack) > 1 and stack[-1][1] >= indent:
            stack.pop()

        parent, parent_indent = stack[-1]

        # 2. Handle List Items (- ...)
        if content.startswith("- "):
            value_str = content[2:].strip()

            # CASE: "models:" was seen, creating a dict, but now we see a list item.
            # We must convert that empty dict into a list.
            if isinstance(parent, dict):
                # Find the key that opened this scope
                # (it must be the last key added to the grandparent)
                # We look at the stack history to find the context
                if len(stack) >= 2:
                    grandparent, _ = stack[-2]
                    if isinstance(grandparent, dict):
                        # Find the last key added to grandparent that points to our empty parent
                        # This is a heuristic for "last key defined"
                        for k, v in list(grandparent.items()):
                            if v is parent:
                                # Convert to list
                                new_list = []
                                grandparent[k] = new_list
                                parent = new_list
                                stack[-1] = (
                                    new_list,
                                    parent_indent,
                                )  # Update current stack entry
                                break

            if isinstance(parent, list):
                if ": " in value_str:
                    # List item with key-value (e.g., "- id: qwen...")
                    k, v = _split_key_value(value_str)
                    new_item = {k: _parse_value(v)}
                    parent.append(new_item)
                    # Push this new dictionary onto the stack
                    # so following lines (type:, role:) add to it
                    stack.append((new_item, indent))
                else:
                    # Simple list item
                    parent.append(_parse_value(value_str))
            continue

        # 3. Handle Key-Value Pairs (key: value)
        if ": " in content:
            key, value_str = _split_key_value(content)
            value = _parse_value(value_str)

            if isinstance(parent, dict):
                parent[key] = value
            elif isinstance(parent, list):
                # This handles adding properties to the dictionary we pushed onto the stack
                # in the previous step (the list item logic).
                if len(parent) > 0 and isinstance(parent[-1], dict):
                    parent[-1][key] = value
            continue

        # 4. Handle Dictionary Keys (key:)
        if content.endswith(":"):
            key = content[:-1].strip()
            new_dict = {}
            if isinstance(parent, dict):
                parent[key] = new_dict
                stack.append((new_dict, indent))
            elif isinstance(parent, list):
                # Edge case: dict inside list defined by key:
                if len(parent) > 0 and isinstance(parent[-1], dict):
                    parent[-1][key] = new_dict
                else:
                    parent.append(new_dict)
                stack.append((new_dict, indent))

    return result


def _split_key_value(s: str) -> tuple[str, str]:
    """Split 'key: value' into (key, value_string)."""
    idx = s.index(": ")
    return s[:idx].strip(), s[idx + 2 :].strip()


def _parse_key_value(s: str) -> tuple[str, str | int | float | bool | None]:
    """Parse a 'key: value' string into (key, value)."""
    key, value_str = _split_key_value(s)
    return key, _parse_value(value_str)


def _parse_value(v: str) -> str | int | float | bool | None:
    """Parse a YAML scalar value."""
    if v == "":
        return None
    if v == "true":
        return True
    if v == "false":
        return False
    if (v.startswith('"') and v.endswith('"')) or (
        v.startswith("'") and v.endswith("'")
    ):
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def load_profiles(profiles_path: str) -> dict:
    """Load and parse profiles.yml."""
    with open(profiles_path, "r") as f:
        text = f.read()
    return parse_yaml_simple(text)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: profile-helper.py <profile> <command>", file=sys.stderr)
        sys.exit(1)

    profile_name = sys.argv[1]
    command = sys.argv[2]

    # Find profiles.yml — look relative to this script's location
    script_dir = Path(__file__).parent
    profiles_path = script_dir.parent / "profiles.yml"

    if not profiles_path.exists():
        print(f"Error: {profiles_path} not found", file=sys.stderr)
        sys.exit(1)

    data = load_profiles(str(profiles_path))
    profiles = data.get("profiles", {})

    if profile_name not in profiles:
        print(
            f"Error: profile '{profile_name}' not found in profiles.yml",
            file=sys.stderr,
        )
        print(f"Available profiles: {', '.join(profiles.keys())}", file=sys.stderr)
        sys.exit(1)

    profile = profiles[profile_name]

    if command == "default-model":
        models = profile.get("models", [])
        for m in models:
            if isinstance(m, dict) and m.get("role") == "default":
                print(m["id"])
                return
        print("Error: no model with role='default' found", file=sys.stderr)
        sys.exit(1)

    elif command == "all-models":
        models = profile.get("models", [])
        for m in models:
            if isinstance(m, dict):
                print(m["id"])
        return

    elif command == "models-json":
        models = profile.get("models", [])
        output = []
        for m in models:
            if isinstance(m, dict):
                output.append(m)
        print(json.dumps(output, indent=2))
        return

    elif command == "description":
        print(profile.get("description", ""))
        return

    elif command == "label":
        print(profile.get("label", profile_name))
        return

    elif command == "engine":
        print(profile.get("engine", "ollama"))
        return

    elif command == "validate":
        # Basic validation
        errors = []
        for pname, pdata in profiles.items():
            # Check profile name is valid
            if pname not in VALID_PROFILES:
                errors.append(
                    f"Profile '{pname}': invalid profile name. "
                    f"Must be one of: {', '.join(sorted(VALID_PROFILES))}"
                )

            # Check required fields
            if not pdata.get("description"):
                errors.append(f"Profile '{pname}': missing 'description'")
            if not pdata.get("label"):
                errors.append(f"Profile '{pname}': missing 'label'")

            # Check engine
            engine = pdata.get("engine", "ollama")
            if engine not in ("ollama",):
                errors.append(
                    f"Profile '{pname}': unsupported engine '{engine}'. "
                    f"Supported: ollama"
                )

            # Check models
            models = pdata.get("models", [])
            has_default = False
            for m in models:
                if isinstance(m, dict):
                    if not m.get("id"):
                        errors.append(f"Profile '{pname}': model missing 'id'")
                    if not m.get("type"):
                        errors.append(
                            f"Profile '{pname}': model {m.get('id')} missing 'type'"
                        )
                    if m.get("role") == "default":
                        has_default = True
            if not has_default:
                errors.append(f"Profile '{pname}': no model with role='default'")

        # Check all required profiles exist
        for required in VALID_PROFILES:
            if required not in profiles:
                errors.append(
                    f"Required profile '{required}' is missing from profiles.yml"
                )

        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        else:
            print("OK: profiles.yml is valid")
        return

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print(
            "Available commands: default-model, all-models, models-json, "
            "description, label, engine, validate",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
