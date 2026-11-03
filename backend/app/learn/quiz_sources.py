"""Web provenance for quizzes; retrieval receipts are not correctness scores."""

from urllib.parse import urlsplit, urlunsplit


class QuizCitationError(ValueError):
    def __init__(self, message, issues):
        super().__init__(message)
        self.issues = issues


def source_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError
        parsed.port  # Validate malformed/out-of-range ports too.
    except ValueError as exc:
        raise ValueError(
            "Source URLs must be absolute HTTP(S) links without credentials"
        ) from exc
    return value


def citation_key(value: str) -> str:
    # A section anchor can be useful in a citation without changing the fetched
    # resource. Never match by hostname substring or ignore path/query changes.
    return urlunsplit(urlsplit(source_url(value))._replace(fragment=""))


def validate_web_sources(questions, evidence):
    """Validate supplied links only; missing citations never prevent saving."""
    fetched = set()
    for record in evidence:
        if record.get("tool") != "use_webfetch" or not record.get("text", "").strip():
            continue
        for url in (
            record.get("request", {}).get("url"),
            record.get("metadata", {}).get("url"),
        ):
            if url:
                try:
                    fetched.add(citation_key(url))
                except ValueError:
                    # A fetched URL with credentials or malformed metadata is
                    # not a publishable citation; unrelated questions still work.
                    continue
    issues = []
    for index, question in enumerate(questions):
        for url_index, url in enumerate(question.source_urls):
            if citation_key(url) not in fetched:
                issues.append(
                    {
                        "path": ["questions", index, "source_urls", url_index],
                        "message": "URL was not successfully fetched in this request. Use use_webfetch on the supporting page first; search snippets are not full-page evidence.",
                    }
                )
    if issues:
        details = "; ".join(
            f"{'.'.join(str(part) for part in issue['path'])}: {issue['message']}"
            for issue in issues[:3]
        )
        raise QuizCitationError(
            details
            + " Nothing was saved. Correct the affected links or omit unavailable optional citations; never invent references.",
            issues,
        )


def grounding_summary(questions):
    sourced = sum(
        bool(q.source_urls or q.source_document_id or q.source_artifact)
        for q in questions
    )
    return {
        "sourced_questions": sourced,
        "unsourced_questions": len(questions) - sourced,
        "message": (
            "Sources record provenance, not automatic answer verification. "
            "Disclose unsourced questions; do not claim the quiz is fact-checked."
        ),
    }
