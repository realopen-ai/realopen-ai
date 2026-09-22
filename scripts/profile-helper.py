#!/usr/bin/env python3
"""
Helper script to parse profiles.yml and modules.yml from bash.
Called by setup.sh — NOT meant for direct use.

Usage:
    python3 scripts/profile-helper.py <profile> <command>
    python3 scripts/profile-helper.py modules <profile> <command>

Profile commands:
    default-model    Print the default model ID for a profile
    all-models       Print all model IDs for a profile (one per line)
    models-json      Print all models for a profile as JSON
    description      Print the profile description
    label            Print the human-readable profile label
    engine           Print the inference engine for the profile
    validate         Check that profiles.yml is valid (includes the voice
                     section when present)
    voice-config     Print the merged voice (ASR/TTS) config for a profile
                     as JSON (top-level `voice:` defaults merged with the
                     optional `profiles.<name>.voice:` overrides)
    voice-models     Print the voice model IDs for a profile (one per line,
                     ASR first, then TTS)
    voice-summary    Print a human-readable summary of the voice models

Module commands (use "modules" as first arg):
    list             List all module names (one per line)
    list-optional    List optional module names only
    module-models    Print model IDs for a module on a profile (one per line)
    module-info      Print module info as JSON for a profile
    validate-modules Check that modules.yml is valid
    all-module-models Print all model IDs from all enabled optional modules for a profile
"""

import sys
import json
from pathlib import Path

# Minimal YAML parser — no external dependency needed
# Supports: nested dicts, lists, basic scalars, multiline strings
# This is intentionally simple; profiles.yml/modules.yml don't use advanced YAML features.

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
    """Parse a subset of YAML sufficient for profiles.yml and modules.yml."""
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
                                stack[-1] = (new_list, parent_indent)
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


def load_modules(modules_path: str) -> dict:
    """Load and parse modules.yml."""
    with open(modules_path, "r") as f:
        text = f.read()
    return parse_yaml_simple(text)


VALID_ASR_PROVIDERS = {"qwen3-asr"}
VALID_TTS_PROVIDERS = {"pocket-tts"}


def merge_voice_config(data: dict, profile_name: str) -> dict:
    """Merge the top-level `voice:` defaults with the optional per-profile
    override `profiles.<name>.voice:` (shallow merge per side: asr / tts).

    profiles.yml is the single source of truth for ASR/TTS selection — this
    function is the host-side (stdlib-only) equivalent of the backend's
    voice config resolver.
    """
    base = data.get("voice") or {}
    if not isinstance(base, dict):
        base = {}
    profile = (data.get("profiles") or {}).get(profile_name) or {}
    override = profile.get("voice") if isinstance(profile, dict) else None
    if not isinstance(override, dict):
        override = {}

    merged = {}
    for side in ("asr", "tts"):
        side_cfg = dict(base.get(side) or {})
        side_cfg.update(override.get(side) or {})
        merged[side] = side_cfg
    return merged


def handle_profile_command(profile_name: str, command: str) -> None:
    """Handle profile-related commands."""
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

    elif command == "voice-config":
        # Merged voice (ASR/TTS) config for this profile as JSON.
        # Empty JSON object when profiles.yml has no `voice:` section.
        merged = merge_voice_config(data, profile_name)
        print(json.dumps(merged, indent=2))
        return

    elif command == "voice-models":
        # Voice model IDs, one per line (ASR first, then TTS).
        merged = merge_voice_config(data, profile_name)
        for side in ("asr", "tts"):
            cfg = merged.get(side) or {}
            if isinstance(cfg, dict) and cfg.get("model"):
                print(cfg["model"])
        return

    elif command == "voice-summary":
        # Human-readable summary (used by setup.sh).
        merged = merge_voice_config(data, profile_name)
        if not merged.get("asr") and not merged.get("tts"):
            print("No voice models configured (no `voice:` section in profiles.yml)")
            return
        for side, label in (("asr", "ASR"), ("tts", "TTS")):
            cfg = merged.get(side) or {}
            if not cfg:
                print(f"{label}: (not configured)")
                continue
            model = cfg.get("model", "?")
            provider = cfg.get("provider", "?")
            extra = cfg.get("revision") or cfg.get("language") or ""
            voice = cfg.get("voice", "")
            desc = cfg.get("description", "")
            size = cfg.get("size", "")
            line = f"{label}: {model}"
            if extra:
                line += f" @ {extra}"
            if voice:
                line += f" (voice: {voice})"
            line += f"  [{provider}]"
            print(line)
            if desc:
                print(f"       {desc}")
            if size:
                print(f"       approx. size: {size}")
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
                    f"Profile '{pname}': unsupported engine '{engine}'. Supported: ollama"
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

        # Check the voice section (when present) — provider IDs are
        # capability strings; the *selection* itself comes from this file.
        voice_base = data.get("voice")
        if voice_base is not None:
            if not isinstance(voice_base, dict):
                errors.append("voice: section must be a mapping (asr/tts)")
            else:
                for side in ("asr", "tts"):
                    side_cfg = voice_base.get(side)
                    if side_cfg is None:
                        errors.append(f"voice: missing '{side}' section")
                        continue
                    if not isinstance(side_cfg, dict):
                        errors.append(f"voice.{side}: must be a mapping")
                        continue
                    if not side_cfg.get("model"):
                        errors.append(f"voice.{side}: missing 'model'")
                    if not side_cfg.get("type"):
                        errors.append(f"voice.{side}: missing 'type'")
                    if not side_cfg.get("role"):
                        errors.append(f"voice.{side}: missing 'role'")
                asr_cfg = voice_base.get("asr") or {}
                tts_cfg = voice_base.get("tts") or {}
                if isinstance(asr_cfg, dict) and asr_cfg.get("provider") not in VALID_ASR_PROVIDERS:
                    errors.append(
                        "voice.asr: unsupported provider "
                        f"'{asr_cfg.get('provider')}'. Supported: "
                        + ", ".join(sorted(VALID_ASR_PROVIDERS))
                    )
                if isinstance(tts_cfg, dict) and tts_cfg.get("provider") not in VALID_TTS_PROVIDERS:
                    errors.append(
                        "voice.tts: unsupported provider "
                        f"'{tts_cfg.get('provider')}'. Supported: "
                        + ", ".join(sorted(VALID_TTS_PROVIDERS))
                    )

            # Per-profile voice overrides must also be mappings with valid sides.
            for pname, pdata in profiles.items():
                if not isinstance(pdata, dict):
                    continue
                p_voice = pdata.get("voice")
                if p_voice is None:
                    continue
                if not isinstance(p_voice, dict):
                    errors.append(f"Profile '{pname}': voice override must be a mapping")
                    continue
                for side in ("asr", "tts"):
                    side_cfg = p_voice.get(side)
                    if side_cfg is None:
                        continue
                    if not isinstance(side_cfg, dict):
                        errors.append(
                            f"Profile '{pname}': voice.{side} override must be a mapping"
                        )
                    elif side_cfg.get("provider") is not None:
                        valid = (
                            VALID_ASR_PROVIDERS if side == "asr" else VALID_TTS_PROVIDERS
                        )
                        if side_cfg["provider"] not in valid:
                            errors.append(
                                f"Profile '{pname}': voice.{side} unsupported provider "
                                f"'{side_cfg['provider']}'"
                            )

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
        sys.exit(1)


def handle_module_command(profile_name: str, command: str) -> None:
    """Handle module-related commands."""
    script_dir = Path(__file__).parent
    modules_path = script_dir.parent / "modules.yml"

    if not modules_path.exists():
        print(f"Error: {modules_path} not found", file=sys.stderr)
        sys.exit(1)

    data = load_modules(str(modules_path))
    modules = data.get("modules", {})

    if command == "list":
        for name in modules:
            print(name)
        return

    elif command == "list-optional":
        for name, mdata in modules.items():
            if not mdata.get("required", False):
                print(name)
        return

    elif command == "module-models":
        # Print model IDs for a specific module on a profile
        # Usage: profile-helper.py modules <profile> module-models <module_name>
        if len(sys.argv) < 5:
            print(
                "Usage: profile-helper.py modules <profile> module-models <module_name>",
                file=sys.stderr,
            )
            sys.exit(1)

        module_name = sys.argv[4]
        mdata = modules.get(module_name)
        if not mdata:
            print(f"Error: module '{module_name}' not found", file=sys.stderr)
            sys.exit(1)

        models_data = mdata.get("models", {})
        # For optional modules, models is a dict of profile → model list
        if isinstance(models_data, dict) and not mdata.get("required", False):
            profile_models = models_data.get(profile_name, [])
            if profile_models is None:
                profile_models = []
            for m in profile_models:
                if isinstance(m, dict) and m.get("id"):
                    print(m["id"])
        return

    elif command == "module-info":
        # Print module info as JSON for a profile
        result = []
        for name, mdata in modules.items():
            required = mdata.get("required", False)
            label = mdata.get("label", name)
            description = mdata.get("description", "")
            available = False
            module_models = []

            models_data = mdata.get("models", {})
            if required:
                available = True
            elif isinstance(models_data, dict):
                profile_models = models_data.get(profile_name)
                available = profile_models is not None
                if profile_models:
                    module_models = profile_models

            result.append(
                {
                    "name": name,
                    "required": required,
                    "label": label,
                    "description": description,
                    "available": available,
                    "models": module_models,
                }
            )

        print(json.dumps(result, indent=2))
        return

    elif command == "validate-modules":
        errors = []

        # Check that 'assistant' module exists and is required
        if "assistant" not in modules:
            errors.append("Required module 'assistant' is missing from modules.yml")
        elif not modules["assistant"].get("required", False):
            errors.append("Module 'assistant' must be required=true")

        # Check each module
        for name, mdata in modules.items():
            if not mdata.get("label"):
                errors.append(f"Module '{name}': missing 'label'")
            if not mdata.get("tools"):
                errors.append(f"Module '{name}': missing 'tools'")

            # Check optional modules have models
            if not mdata.get("required", False):
                models_data = mdata.get("models", {})
                if not models_data:
                    errors.append(
                        f"Module '{name}': optional module must define 'models' "
                        f"with at least one profile"
                    )
                elif isinstance(models_data, dict):
                    for pname, profile_models in models_data.items():
                        if pname not in VALID_PROFILES:
                            errors.append(
                                f"Module '{name}': invalid profile '{pname}' in models"
                            )
                        if profile_models and isinstance(profile_models, list):
                            for m in profile_models:
                                if isinstance(m, dict) and not m.get("id"):
                                    errors.append(
                                        f"Module '{name}' profile '{pname}': model missing 'id'"
                                    )

        if errors:
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        else:
            print("OK: modules.yml is valid")
        return

    elif command == "all-module-models":
        # Print all model IDs from all optional modules that are available for the profile
        for name, mdata in modules.items():
            if mdata.get("required", False):
                continue
            models_data = mdata.get("models", {})
            if isinstance(models_data, dict):
                profile_models = models_data.get(profile_name, [])
                if profile_models:
                    for m in profile_models:
                        if isinstance(m, dict) and m.get("id"):
                            print(m["id"])
        return

    else:
        print(f"Unknown module command: {command}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: profile-helper.py <profile> <command>", file=sys.stderr)
        print("       profile-helper.py modules <profile> <command>", file=sys.stderr)
        sys.exit(1)

    # Check if this is a module command
    if sys.argv[1] == "modules":
        if len(sys.argv) < 4:
            print(
                "Usage: profile-helper.py modules <profile> <command>", file=sys.stderr
            )
            sys.exit(1)
        profile_name = sys.argv[2]
        command = sys.argv[3]
        handle_module_command(profile_name, command)
    else:
        profile_name = sys.argv[1]
        command = sys.argv[2]
        handle_profile_command(profile_name, command)


if __name__ == "__main__":
    main()
