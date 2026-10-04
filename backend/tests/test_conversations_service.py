"""
Direct unit tests for app/services/conversations.py — the persistence service
layer used by the chat API and the voice WebSocket session.

Functions covered (with a recording FakeSession — NO live Postgres):
  - create_conversation            (defaults + explicit title/model, flush)
  - get_conversation               (scalar passthrough, missing → None)
  - update_conversation_title      (UPDATE values, flush)
  - delete_conversation            (messages deleted before the conversation)
  - add_message                    (all column defaults + conversation touch)
  - get_messages                   (ordering + limit)
  - persist_message_standalone     (independent session: success, commit
                                    failure → None, factory failure → None)
  - update_message_standalone      (rowcount 1 → True, 0 → False, error → False)
  - save_document                  (row construction)
  - conversation_to_dict           (summary/sandbox/flag serialization)
  - message_to_dict                (blocks/deliverables/modality branches)

set_conversation_flags / list_conversations ordering are already covered by
tests/test_conversation_flags.py and are not duplicated here.

The standalone-session functions import async_session_factory from
app.db.session at CALL time, so the module attribute is monkeypatched with an
in-memory factory — the real engine is never connected to.
"""

import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Delete

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import models  # noqa: E402
from app.services import conversations as svc  # noqa: E402

CONV_ID = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════════════


class FakeResult:
    """SQLAlchemy Result stand-in for scalar / list reads."""

    def __init__(self, value=None, rowcount=None):
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeSession:
    """AsyncSession stand-in recording execute()/add()/flush()/commit()."""

    def __init__(self, results=None, commit_error=None):
        self._results = list(results or [])
        self.executed = []
        self.added = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.commit_error = commit_error

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        if self._results:
            return self._results.pop(0)
        return FakeResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


class FakeSessionFactory:
    """async_session_factory() replacement yielding a FakeSession."""

    def __init__(self, session):
        self.session = session
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def run(coro):
    return asyncio.run(coro)


def update_values(stmt):
    """Extract the SET values dict from a SQLAlchemy Update statement."""
    return dict(stmt.compile().params)


# ══════════════════════════════════════════════════════════════════════
# create / get / rename / delete
# ══════════════════════════════════════════════════════════════════════


def test_create_conversation_defaults():
    db = FakeSession()
    conv = run(svc.create_conversation(db))
    assert isinstance(conv, models.Conversation)
    assert isinstance(conv.id, uuid.UUID)
    assert conv.title == svc.DEFAULT_TITLE == "New Chat"
    assert conv.model is None
    assert conv.created_at is not None and conv.updated_at is not None
    assert db.added == [conv]
    assert db.flush_count == 1


def test_create_conversation_explicit_title_and_model():
    db = FakeSession()
    conv = run(svc.create_conversation(db, title="Budget talk", model="qwen3:4b"))
    assert conv.title == "Budget talk"
    assert conv.model == "qwen3:4b"


def test_get_conversation_returns_row():
    row = SimpleNamespace(id=CONV_ID)
    db = FakeSession(results=[FakeResult(row)])
    assert run(svc.get_conversation(db, CONV_ID)) is row


def test_get_conversation_missing_returns_none():
    db = FakeSession(results=[FakeResult(None)])
    assert run(svc.get_conversation(db, CONV_ID)) is None


def test_update_conversation_title_writes_values_and_flushes():
    db = FakeSession()
    run(svc.update_conversation_title(db, CONV_ID, "Renamed"))
    values = update_values(db.executed[0])
    assert values["title"] == "Renamed"
    assert values["updated_at"] is not None
    assert db.flush_count == 1


def test_delete_conversation_removes_messages_first():
    db = FakeSession()
    run(svc.delete_conversation(db, CONV_ID))
    assert len(db.executed) == 2
    assert isinstance(db.executed[0], Delete)
    assert isinstance(db.executed[1], Delete)
    # first statement targets messages, second the conversation
    assert db.executed[0].table.name == "messages"
    assert db.executed[1].table.name == "conversations"
    assert db.flush_count == 1


# ══════════════════════════════════════════════════════════════════════
# add_message / get_messages
# ══════════════════════════════════════════════════════════════════════


def test_add_message_user_defaults():
    db = FakeSession()
    msg = run(svc.add_message(db, CONV_ID, "user", "hello"))
    assert db.added == [msg]
    assert msg.role == "user"
    assert msg.content == "hello"
    assert msg.model is None
    assert msg.has_image is False
    assert msg.has_document is False
    assert msg.image_count == 0
    assert msg.document_count == 0
    assert msg.blocks is None
    assert msg.deliverables is None
    assert msg.modality is None
    assert msg.completion_status == "completed"
    assert isinstance(msg.id, uuid.UUID)
    # conversation updated_at is refreshed
    assert "updated_at" in update_values(db.executed[0])
    assert db.flush_count == 1


def test_add_message_assistant_with_blocks_and_deliverables():
    db = FakeSession()
    blocks = [{"type": "text", "content": "hi"}]
    deliverables = [{"type": "report", "filename": "r.pdf"}]
    msg = run(
        svc.add_message(
            db,
            CONV_ID,
            "assistant",
            "hi",
            model="qwen3:4b",
            tokens=17,
            generation_duration=1.5,
            blocks=blocks,
            deliverables=deliverables,
            modality="voice",
            completion_status="interrupted",
        )
    )
    assert msg.model == "qwen3:4b"
    assert msg.tokens == 17
    assert msg.blocks == blocks
    assert msg.deliverables == deliverables
    assert msg.modality == "voice"
    assert msg.completion_status == "interrupted"


def test_get_messages_orders_and_limits():
    db = FakeSession(results=[FakeResult([])])
    run(svc.get_messages(db, CONV_ID, limit=25))
    stmt = db.executed[0]
    assert "ORDER BY messages.created_at ASC" in str(stmt)
    assert stmt._limit_clause.value == 25


def test_get_messages_returns_list_in_order():
    m1 = SimpleNamespace(id=uuid.uuid4())
    m2 = SimpleNamespace(id=uuid.uuid4())
    db = FakeSession(results=[FakeResult([m1, m2])])
    msgs = run(svc.get_messages(db, CONV_ID))
    assert msgs == [m1, m2]


# ══════════════════════════════════════════════════════════════════════
# persist_message_standalone / update_message_standalone
# ══════════════════════════════════════════════════════════════════════


def test_persist_message_standalone_commits_and_returns_id(monkeypatch):
    session = FakeSession()
    factory = FakeSessionFactory(session)
    monkeypatch.setattr("app.db.session.async_session_factory", factory)

    msg_id = run(svc.persist_message_standalone(CONV_ID, "user", "hello"))
    assert msg_id == session.added[0].id
    assert session.commit_count == 1
    assert factory.calls == 1


def test_persist_message_standalone_commit_failure_returns_none(monkeypatch):
    session = FakeSession(commit_error=RuntimeError("db down"))
    monkeypatch.setattr(
        "app.db.session.async_session_factory", FakeSessionFactory(session)
    )
    assert run(svc.persist_message_standalone(CONV_ID, "user", "hello")) is None


def test_persist_message_standalone_factory_failure_returns_none(monkeypatch):
    def exploding_factory():
        raise RuntimeError("cannot connect")

    monkeypatch.setattr("app.db.session.async_session_factory", exploding_factory)
    assert run(svc.persist_message_standalone(CONV_ID, "user", "hello")) is None


def test_update_message_standalone_success(monkeypatch):
    session = FakeSession(results=[FakeResult(None, rowcount=1)])
    monkeypatch.setattr(
        "app.db.session.async_session_factory", FakeSessionFactory(session)
    )
    ok = run(
        svc.update_message_standalone(
            uuid.uuid4(),
            content="final",
            blocks=[{"type": "text", "content": "final"}],
            generation_duration=2.0,
            completion_status="completed",
        )
    )
    assert ok is True
    assert session.commit_count == 1
    values = update_values(session.executed[0])
    assert values["content"] == "final"
    assert values["completion_status"] == "completed"


def test_update_message_standalone_missing_row_returns_false(monkeypatch):
    session = FakeSession(results=[FakeResult(None, rowcount=0)])
    monkeypatch.setattr(
        "app.db.session.async_session_factory", FakeSessionFactory(session)
    )
    ok = run(svc.update_message_standalone(uuid.uuid4(), content="x"))
    assert ok is False


def test_update_message_standalone_error_returns_false(monkeypatch):
    session = FakeSession(commit_error=RuntimeError("write failed"))
    monkeypatch.setattr(
        "app.db.session.async_session_factory", FakeSessionFactory(session)
    )
    assert run(svc.update_message_standalone(uuid.uuid4(), content="x")) is False


# ══════════════════════════════════════════════════════════════════════
# save_document
#
# NOTE: save_document is currently DEAD CODE — it constructs Document with a
# `content` kwarg, but the Document model was migrated to the RAG schema
# (original_filename / file_path / digestion_status, no `content` column).
# The test below pins that incompatibility so a future fix or model change
# gets noticed.
# ══════════════════════════════════════════════════════════════════════


def test_save_document_is_incompatible_with_current_model():
    db = FakeSession()
    with pytest.raises(TypeError):
        run(svc.save_document(db, "notes.pdf", "extracted text"))
    assert db.added == []


# ══════════════════════════════════════════════════════════════════════
# serialization
# ══════════════════════════════════════════════════════════════════════


def make_conv(**kw):
    now = datetime.utcnow()
    data = dict(
        id=CONV_ID,
        title="A Chat",
        model="qwen3:4b",
        sandbox_id=None,
        created_at=now,
        updated_at=now,
        pinned=False,
        archived=False,
        pinned_at=None,
        archived_at=None,
        summary=None,
        summary_at=None,
    )
    data.update(kw)
    return SimpleNamespace(**data)


def make_msg(**kw):
    data = dict(
        id=uuid.uuid4(),
        conversation_id=CONV_ID,
        role="assistant",
        content="answer",
        model="qwen3:4b",
        tokens=10,
        has_image=False,
        has_document=True,
        image_count=0,
        document_count=2,
        blocks=[{"type": "text", "content": "answer"}],
        generation_duration=3.2,
        deliverables=[{"type": "report"}],
        modality=None,
        completion_status="completed",
        created_at=datetime(2024, 5, 1, 12, 0, 0),
    )
    data.update(kw)
    return SimpleNamespace(**data)


def test_conversation_to_dict_serializes_all_keys():
    summary_at = datetime(2024, 6, 1)
    sandbox_id = uuid.uuid4()
    d = run(
        svc.conversation_to_dict(
            make_conv(
                sandbox_id=sandbox_id,
                summary="Discussed budgets",
                summary_at=summary_at,
            )
        )
    )
    assert d["id"] == str(CONV_ID)
    assert d["title"] == "A Chat"
    assert d["model"] == "qwen3:4b"
    assert d["sandboxId"] == str(sandbox_id)
    assert d["createdAt"] == d["updatedAt"]
    assert isinstance(d["createdAt"], int)
    assert d["pinned"] is False
    assert d["archived"] is False
    assert d["pinnedAt"] is None
    assert d["archivedAt"] is None
    assert d["summary"] == "Discussed budgets"
    assert d["summaryAt"] == int(summary_at.timestamp() * 1000)


def test_conversation_to_dict_handles_missing_dates_and_summary():
    d = run(svc.conversation_to_dict(make_conv(created_at=None, updated_at=None)))
    assert d["createdAt"] == 0
    assert d["updatedAt"] == 0
    assert d["summary"] is None
    assert d["summaryAt"] is None
    assert d["sandboxId"] is None


def test_message_to_dict_full_payload():
    d = run(svc.message_to_dict(make_msg()))
    assert d["id"] and d["conversationId"] == str(CONV_ID)
    assert d["role"] == "assistant"
    assert d["content"] == "answer"
    assert d["model"] == "qwen3:4b"
    assert d["tokens"] == 10
    assert d["hasImage"] is False
    assert d["hasDocument"] is True
    assert d["imageCount"] == 0
    assert d["documentCount"] == 2
    assert d["blocks"] == [{"type": "text", "content": "answer"}]
    assert d["generationDuration"] == 3.2
    assert d["deliverables"] == [{"type": "report"}]
    assert "modality" not in d  # omitted when NULL
    assert d["completionStatus"] == "completed"
    assert d["createdAt"] == int(datetime(2024, 5, 1, 12, 0, 0).timestamp() * 1000)


def test_message_to_dict_minimal_payload():
    d = run(
        svc.message_to_dict(
            make_msg(blocks=None, deliverables=None, generation_duration=None,
                     created_at=None)
        )
    )
    assert d["blocks"] is None
    assert d["deliverables"] is None
    assert "generationDuration" not in d
    assert d["createdAt"] == 0


def test_message_to_dict_voice_modality_included():
    d = run(svc.message_to_dict(make_msg(modality="voice")))
    assert d["modality"] == "voice"


def test_message_to_dict_completion_status_passthrough():
    d = run(svc.message_to_dict(make_msg(completion_status="error")))
    assert d["completionStatus"] == "error"
