"""Conservative relevance gates for automatic context, not explicit searches."""

from app.services.memory import content_tokens

# Request boilerplate is not a reason to retrieve an unrelated old task.
_REQUEST_WORDS = frozenset(
    {
        "hello",
        "hi",
        "hey",
        "please",
        "make",
        "create",
        "give",
        "include",
        "using",
        "question",
        "questions",
        "quiz",
        "quizzes",
        "basics",
        "basic",
        "code",
        "snippets",
        "bonjour",
        "salut",
        "merci",
        "créer",
        "faire",
        "donne",
        "questions",
        "مرحبا",
        "شكرا",
    }
)


def topic_tokens(query):
    return content_tokens(query) - _REQUEST_WORDS


def relevant_context(query, text, score=0.0):
    topics = topic_tokens(query)
    if not topics:
        return False
    # Require topical overlap for weak embedding matches; permit strong
    # semantic matches (including paraphrases and cross-language retrieval).
    return bool(topics & content_tokens(text)) or score >= 0.8
