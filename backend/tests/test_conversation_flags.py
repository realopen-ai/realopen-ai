"""
Tests for conversation sidebar organization: pin / archive / rename.

Covers the three layers touched by the three-dots conversation menu:

1. Service layer (app/services/conversations.py):
   - set_conversation_flags — pin/unpin/archive/unarchive value matrices,
     including the cross-flag rules (pin implies unarchive, archive implies
     unpin) and the not-found path.
   - list_conversations — archived filtering + pinned-first ordering.
   - conversation_to_dict — the pinned/archived/pinnedAt/archivedAt keys.

2. API layer (app/api/chat.py):
   - PATCH /conversations/{id} — title / pinned / archived params are
     forwarded correctly; 404 when the conversation doesn't exist; the
     response embeds the updated conversation dict.
   - GET /conversations — the `archived` query param is forwarded.

No live database is required — the SQLAlchemy async session is replaced
by a recording FakeSession and the API endpoints are invoked directly
with monkeypatched service calls, following the strategy used by the
other test modules (see conftest.py).
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import conversations as conv_service  # noqa: E402

CONV_ID = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════════════


class FakeResult:
    """Stand-in for a SQLAlchemy Result with the methods we use."""

    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeSession:
    """Minimal AsyncSession stand-in that records every execute() call."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.executed = []
        self.flush_count = 0

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        if self._results:
            return self._results.pop(0)
        return FakeResult(None)

    async def flush(self):
        self.flush_count += 1


def make_conv(
    pinned=False,
    archived=False,
    pinned_at=None,
    archived_at=None,
    title="Some Chat",
):
    """Build a Conversation-like object for conversation_to_dict."""
    now = datetime.utcnow()
    return SimpleNamespace(
        id=CONV_ID,
        title=title,
        model=None,
        sandbox_id=None,
        created_at=now,
        updated_at=now,
        pinned=pinned,
        archived=archived,
        pinned_at=pinned_at,
        archived_at=archived_at,
        summary=None,
        summary_at=None,
    )


def update_values(stmt):
    """Extract the SET values dict from a SQLAlchemy Update statement."""
    return dict(stmt.compile().params)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════
# 1. set_conversation_flags
# ══════════════════════════════════════════════════════════════════════


def test_pin_sets_pinned_and_pinned_at():
    db = FakeSession(results=[FakeResult(make_conv())])
    ok = run(conv_service.set_conversation_flags(db, CONV_ID, pinned=True))
    assert ok is True
    values = update_values(db.executed[1])  # [0] = SELECT, [1] = UPDATE
    assert values["pinned"] is True
    assert values["pinned_at"] is not None


def test_unpin_clears_pinned_at():
    db = FakeSession(
        results=[FakeResult(make_conv(pinned=True, pinned_at=datetime.utcnow()))]
    )
    ok = run(conv_service.set_conversation_flags(db, CONV_ID, pinned=False))
    assert ok is True
    values = update_values(db.executed[1])
    assert values["pinned"] is False
    assert values["pinned_at"] is None


def test_pin_archived_conversation_also_unarchives():
    """A pinned chat must be visible — pin implies unarchive."""
    db = FakeSession(
        results=[FakeResult(make_conv(archived=True, archived_at=datetime.utcnow()))]
    )
    ok = run(conv_service.set_conversation_flags(db, CONV_ID, pinned=True))
    assert ok is True
    values = update_values(db.executed[1])
    assert values["pinned"] is True
    assert values["archived"] is False
    assert values["archived_at"] is None


def test_archive_pinned_conversation_also_unpins():
    """An archived chat can't float on top of a list it's not in."""
    db = FakeSession(
        results=[FakeResult(make_conv(pinned=True, pinned_at=datetime.utcnow()))]
    )
    ok = run(conv_service.set_conversation_flags(db, CONV_ID, archived=True))
    assert ok is True
    values = update_values(db.executed[1])
    assert values["archived"] is True
    assert values["archived_at"] is not None
    assert values["pinned"] is False
    assert values["pinned_at"] is None


def test_archive_plain_conversation_only_touches_archive():
    db = FakeSession(results=[FakeResult(make_conv())])
    ok = run(conv_service.set_conversation_flags(db, CONV_ID, archived=True))
    assert ok is True
    values = update_values(db.executed[1])
    assert values["archived"] is True
    assert "pinned" not in values


def test_unarchive_clears_archived_at():
    db = FakeSession(
        results=[FakeResult(make_conv(archived=True, archived_at=datetime.utcnow()))]
    )
    ok = run(conv_service.set_conversation_flags(db, CONV_ID, archived=False))
    assert ok is True
    values = update_values(db.executed[1])
    assert values["archived"] is False
    assert values["archived_at"] is None


def test_flags_not_found_returns_false_without_update():
    db = FakeSession(results=[FakeResult(None)])
    ok = run(conv_service.set_conversation_flags(db, CONV_ID, pinned=True))
    assert ok is False
    assert len(db.executed) == 1  # only the SELECT, no UPDATE
    assert db.flush_count == 0


def test_flags_noop_when_nothing_passed():
    db = FakeSession(results=[FakeResult(make_conv())])
    ok = run(conv_service.set_conversation_flags(db, CONV_ID))
    assert ok is True
    assert len(db.executed) == 1  # only the SELECT, no UPDATE
    assert db.flush_count == 0


# ══════════════════════════════════════════════════════════════════════
# 2. list_conversations — filtering + ordering
# ══════════════════════════════════════════════════════════════════════


def test_list_orders_pinned_first_then_recency():
    db = FakeSession(results=[FakeResult([])])
    run(conv_service.list_conversations(db))
    sql = str(db.executed[0])
    assert "pinned DESC" in sql
    assert "pinned_at DESC" in sql
    assert "updated_at DESC" in sql


def test_list_default_does_not_filter_archived():
    db = FakeSession(results=[FakeResult([])])
    run(conv_service.list_conversations(db))
    sql = str(db.executed[0])
    assert "archived" not in sql.split("ORDER BY")[0].split("FROM conversations")[1]


def test_list_archived_true_filters():
    db = FakeSession(results=[FakeResult([])])
    run(conv_service.list_conversations(db, archived=True))
    sql = str(db.executed[0])
    assert "archived IS true" in sql


def test_list_archived_false_filters():
    db = FakeSession(results=[FakeResult([])])
    run(conv_service.list_conversations(db, archived=False))
    sql = str(db.executed[0])
    assert "archived IS false" in sql


def test_list_applies_limit_and_offset():
    db = FakeSession(results=[FakeResult([])])
    run(conv_service.list_conversations(db, limit=7, offset=3))
    statement = db.executed[0]
    assert statement._limit_clause.value == 7
    assert statement._offset_clause.value == 3


# ══════════════════════════════════════════════════════════════════════
# 3. conversation_to_dict — new keys
# ══════════════════════════════════════════════════════════════════════


def test_to_dict_includes_flag_keys():
    pinned_at = datetime.utcnow() - timedelta(hours=1)
    archived_at = datetime.utcnow() - timedelta(minutes=5)
    d = run(
        conv_service.conversation_to_dict(
            make_conv(
                pinned=True, archived=True, pinned_at=pinned_at, archived_at=archived_at
            )
        )
    )
    assert d["pinned"] is True
    assert d["archived"] is True
    assert d["pinnedAt"] == int(pinned_at.timestamp() * 1000)
    assert d["archivedAt"] == int(archived_at.timestamp() * 1000)


def test_to_dict_defaults_when_unset():
    d = run(conv_service.conversation_to_dict(make_conv()))
    assert d["pinned"] is False
    assert d["archived"] is False
    assert d["pinnedAt"] is None
    assert d["archivedAt"] is None


# ══════════════════════════════════════════════════════════════════════
# 4. API layer — PATCH /conversations/{id} + GET /conversations
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def chat_api(monkeypatch):
    """Import app.api.chat with its service calls recorded."""
    from app.api import chat as chat_module

    calls = SimpleNamespace(
        title=None,
        flags=None,
        flags_result=True,
        listed=None,
        conv=make_conv(title="Renamed"),
    )

    async def fake_update_title(db, conv_id, title):
        calls.title = title

    async def fake_set_flags(db, conv_id, pinned=None, archived=None):
        calls.flags = {"pinned": pinned, "archived": archived}
        return calls.flags_result

    async def fake_get(db, conv_id):
        return calls.conv

    async def fake_list(db, limit=50, offset=0, archived=None):
        calls.listed = {"limit": limit, "offset": offset, "archived": archived}
        return [calls.conv]

    async def fake_to_dict(conv):
        return {
            "id": str(conv.id),
            "title": conv.title,
            "pinned": conv.pinned,
            "archived": conv.archived,
        }

    monkeypatch.setattr(
        chat_module.conv_service, "update_conversation_title", fake_update_title
    )
    monkeypatch.setattr(
        chat_module.conv_service, "set_conversation_flags", fake_set_flags
    )
    monkeypatch.setattr(chat_module.conv_service, "get_conversation", fake_get)
    monkeypatch.setattr(chat_module.conv_service, "list_conversations", fake_list)
    monkeypatch.setattr(chat_module.conv_service, "conversation_to_dict", fake_to_dict)
    return chat_module, calls


def test_patch_title_only(chat_api):
    chat_module, calls = chat_api
    result = run(chat_module.update_conversation(str(CONV_ID), title="My Renamed Chat"))
    assert calls.title == "My Renamed Chat"
    assert calls.flags is None
    assert result["status"] == "updated"
    assert result["conversation"]["title"] == "Renamed"


def test_patch_pin_forwards_to_service(chat_api):
    chat_module, calls = chat_api
    run(chat_module.update_conversation(str(CONV_ID), pinned=True))
    assert calls.flags == {"pinned": True, "archived": None}
    assert calls.title is None


def test_patch_archive_forwards_to_service(chat_api):
    chat_module, calls = chat_api
    run(chat_module.update_conversation(str(CONV_ID), archived=True))
    assert calls.flags == {"pinned": None, "archived": True}


def test_patch_unarchive_forwards_to_service(chat_api):
    chat_module, calls = chat_api
    run(chat_module.update_conversation(str(CONV_ID), archived=False))
    assert calls.flags == {"pinned": None, "archived": False}


def test_patch_flags_not_found_raises_404(chat_api):
    chat_module, calls = chat_api
    calls.flags_result = False
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        run(chat_module.update_conversation(str(CONV_ID), pinned=True))
    assert exc_info.value.status_code == 404


def test_patch_title_and_flags_together(chat_api):
    chat_module, calls = chat_api
    run(chat_module.update_conversation(str(CONV_ID), title="Both", archived=True))
    assert calls.title == "Both"
    assert calls.flags == {"pinned": None, "archived": True}


def test_get_conversations_forwards_archived_param(chat_api):
    chat_module, calls = chat_api
    result = run(chat_module.list_conversations(archived=True))
    assert calls.listed == {"limit": 50, "offset": 0, "archived": True}
    assert result["conversations"][0]["id"] == str(CONV_ID)


def test_get_conversations_default_archived_is_none(chat_api):
    chat_module, calls = chat_api
    run(chat_module.list_conversations())
    assert calls.listed["archived"] is None


def test_patch_invalid_uuid_raises():
    from app.api import chat as chat_module
    from fastapi import HTTPException

    with pytest.raises((HTTPException, ValueError)):
        run(chat_module.update_conversation("not-a-uuid", pinned=True))


# ══════════════════════════════════════════════════════════════════════
# 5. Route registration sanity
# ══════════════════════════════════════════════════════════════════════


def test_patch_route_registered_with_query_params():
    from app.api import chat as chat_module

    patch_routes = [
        r
        for r in chat_module.router.routes
        if getattr(r, "path", "") == "/conversations/{conversation_id}"
        and "PATCH" in getattr(r, "methods", set())
    ]
    assert patch_routes, "PATCH /conversations/{conversation_id} route not found"
