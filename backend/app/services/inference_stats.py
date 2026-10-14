"""Only runtime-reported counts/timings, never inferred cache hits."""

import math


def _number(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def runtime_usage(data, provider="ollama"):
    if provider == "ollama":
        fields = {
            key: _number(data[key])
            for key in (
                "prompt_eval_count",
                "prompt_eval_cached_count",
                "prompt_eval_duration",
                "eval_count",
                "eval_duration",
                "total_duration",
                "load_duration",
            )
            if key in data
        }
        if not fields:
            return None
        return {
            "provider": provider,
            "prompt_tokens": fields.get("prompt_eval_count"),
            "cached_prompt_tokens": fields.get("prompt_eval_cached_count"),
            "completion_tokens": fields.get("eval_count"),
            "prompt_eval_duration_ns": fields.get("prompt_eval_duration"),
            "generation_duration_ns": fields.get("eval_duration"),
            "runtime_fields": fields,
        }
    usage = data.get("usage") or (data.get("x_groq") or {}).get("usage") or {}
    if not isinstance(usage, dict) or not usage:
        return None
    details = usage.get("prompt_tokens_details") or {}
    return {
        "provider": provider,
        "prompt_tokens": _number(usage.get("prompt_tokens")),
        "cached_prompt_tokens": (
            _number(details.get("cached_tokens")) if isinstance(details, dict) else None
        ),
        "completion_tokens": _number(usage.get("completion_tokens")),
        "runtime_fields": {
            key: _number(usage[key])
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_time",
                "completion_time",
                "total_time",
            )
            if key in usage
        },
    }
