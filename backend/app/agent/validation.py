"""Small preflight validator for the JSON Schema subset exposed by our tools.

Application/business validation remains inside each tool. Never rewrite model
arguments or fabricate missing values to get a call to pass.
"""

import json


def normalize_structured_arguments(value, schema):
    """Decode only valid JSON where the declared type is an array/object.

    Some providers/models encode nested values as JSON strings. This is an
    encoding normalization, not a content repair: malformed JSON is untouched,
    scalars are never coerced, and the original call remains in the transcript.
    """
    if isinstance(value, str) and schema.get("type") in ("array", "object"):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if (schema["type"] == "array" and isinstance(decoded, list)) or (
            schema["type"] == "object" and isinstance(decoded, dict)
        ):
            value = decoded
    if isinstance(value, dict):
        return {
            key: normalize_structured_arguments(
                item, schema.get("properties", {}).get(key, {})
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            normalize_structured_arguments(item, schema.get("items", {}))
            for item in value
        ]
    return value


def argument_errors(value, schema, path="arguments"):
    return [
        f"{path}{''.join('.' + str(part) for part in issue['path'])}: {issue['message']}"
        for issue in argument_issues(value, schema)
    ]


def argument_issues(value, schema, path=()):
    """Return exact typed field locations; never reconstruct paths from prose."""
    errors = []

    def issue(message, parts=path):
        return {"path": list(parts), "message": message}

    if "not" in schema and not argument_issues(value, schema["not"], path):
        errors.append(issue(f"matches forbidden schema {json.dumps(schema['not'])}"))
    types = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    expected = schema.get("type")
    names = [expected] if isinstance(expected, str) else expected or []
    if names and not any(types.get(name, lambda _: True)(value) for name in names):
        error = f"expected {' or '.join(names)}, got {type(value).__name__}"
        if isinstance(value, str) and expected in ("array", "object"):
            try:
                json.loads(value)
            except ValueError as exc:
                error += f"; JSON decoding failed: {exc}"
        return [issue(error)]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(issue(f"allowed values are {json.dumps(schema['enum'])}"))
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(issue("required field is missing", (*path, key)))
        for key, field in schema.get("properties", {}).items():
            if key in value and not (value[key] is None and key not in required):
                errors.extend(argument_issues(value[key], field, (*path, key)))
        # Conditional requirements (e.g. MC answers versus short answers).
        for condition in schema.get("allOf", []):
            if not argument_issues(value, condition.get("if", {}), path):
                errors.extend(argument_issues(value, condition.get("then", {}), path))
    if isinstance(value, list):
        for constraint, op in (
            ("minItems", lambda n: len(value) < n),
            ("maxItems", lambda n: len(value) > n),
        ):
            if constraint in schema and op(schema[constraint]):
                errors.append(
                    issue(f"{constraint}={schema[constraint]}, got {len(value)} items")
                )
        for index, item in enumerate(value):
            errors.extend(
                argument_issues(item, schema.get("items", {}), (*path, index))
            )
    if isinstance(value, str):
        for constraint, op in (
            ("minLength", lambda n: len(value) < n),
            ("maxLength", lambda n: len(value) > n),
        ):
            if constraint in schema and op(schema[constraint]):
                errors.append(issue(f"{constraint}={schema[constraint]}"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for constraint, op in (
            ("minimum", lambda n: value < n),
            ("maximum", lambda n: value > n),
        ):
            if constraint in schema and op(schema[constraint]):
                errors.append(issue(f"{constraint}={schema[constraint]}"))
    return errors[:8]
