"""Deterministic, settings-aware tool exposure; no inference or execution."""

import re
from collections import Counter

from app.agent.tools.tool_defaults import default_tags

_CREATION_WORDS = (
    "create",
    "generate",
    "write",
    "compile",
    "produce",
    "prepare",
    "give",
    "créer",
    "générer",
    "génère",
    "rédiger",
    "rédige",
    "compiler",
    "أنشئ",
    "اكتب",
)

NOTEBOOK_TOOLS = frozenset(
    {
        "rag_search",
        "list_artifacts",
        "read_artifact",
        "summarize_artifact",
        "create_study_note",
        "create_flashcard_deck",
        "create_quiz",
        "discover_tools",
        "read_tool_result",
    }
)


def words(text):
    return re.findall(r"[^\W_]+", str(text).casefold(), re.UNICODE)


def matches(phrase, text):
    """Phrase boundaries, not substrings like 'note' in 'denotes'."""
    parts = words(phrase)
    if not parts:
        return False
    # Match ordinary plural forms too, without stemming arbitrary code/IDs.
    pattern = r"\W+".join(
        re.escape(part) + (r"s?" if len(part) >= 4 else "") for part in parts
    )
    return bool(re.search(r"(?<!\w)" + pattern + r"(?!\w)", text.casefold()))


def enabled_tools(registry, configs, *, notebook=False):
    return {
        tool.name: tool
        for tool in registry.all_tools()
        if configs.get(tool.name, {}).get("enabled", True)
        and (not notebook or tool.name in NOTEBOOK_TOOLS)
    }


def select_tools(query, registry, configs, *, limit=4):
    """Rank request tools without spending their budget on always-load tools.

    Explicit/custom keyword matches remain mandatory. User-configured
    always-load tools are a separate baseline, not competitors for that budget.
    """
    available = enabled_tools(registry, configs)
    tags_by_name = {}
    for name in available:
        tags = configs.get(name, {}).get("tags", default_tags(name)) or []
        if isinstance(tags, str):
            tags = tags.split(",")
        tags_by_name[name] = [
            str(tag).strip().casefold() for tag in tags if str(tag).strip()
        ]
    frequencies = Counter(tag for tags in tags_by_name.values() for tag in set(tags))
    vocabulary = {
        name: set(
            words(
                tool.description
                + " "
                + tool.get_display_name()
                + " "
                + name.replace("_", " ")
            )
        )
        for name, tool in available.items()
    }
    word_frequencies = Counter(
        word for values in vocabulary.values() for word in values
    )
    query_words = set(words(query))
    always_loaded = {
        name for name in available if configs.get(name, {}).get("always_load", False)
    }
    mandatory, ranked = set(), []
    for name, tool in available.items():
        if name == "read_tool_result" and not configs.get(name, {}).get("always_load"):
            continue  # Expose lazily only when an output was shortened.
        cfg = configs.get(name, {})
        tags = tags_by_name[name]
        hits = [tag for tag in tags if matches(tag, query)]
        explicit = bool(
            re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", query, re.I)
        ) or matches(tool.get_display_name(), query)
        customized = set(tags) != {tag.casefold() for tag in default_tags(name)}
        requested_format = (
            not customized
            and any(matches(fmt, query) for fmt in tool.output_formats)
            and any(matches(verb, query) for verb in _CREATION_WORDS)
        )
        if (
            cfg.get("always_load", False)
            or explicit
            or requested_format
            or (customized and hits)
        ):
            mandatory.add(name)
            continue
        if name == "discover_tools":
            continue
        if customized and not hits:
            continue
        score = sum(4 / frequencies[tag] for tag in hits)
        score += sum(
            1 / word_frequencies[word]
            for word in query_words & vocabulary[name]
            if len(word) >= 4
        )
        if score >= 2:
            ranked.append((-score, name))
    # An ambiguous generic tag should not compete with a much more specific
    # capability match. Explicit/custom keyword choices are never capped here.
    if ranked:
        threshold = max(2, -min(ranked)[0] * 0.5)
        ranked = [candidate for candidate in ranked if -candidate[0] >= threshold]
    return mandatory | {
        name
        for _, name in sorted(ranked)[: max(0, limit - len(mandatory - always_loaded))]
    }


def catalog(available, selected):
    return [
        {"name": name, "description": tool.description.splitlines()[0][:140]}
        for name, tool in sorted(available.items())
        if name not in selected and name not in {"discover_tools", "read_tool_result"}
    ]
