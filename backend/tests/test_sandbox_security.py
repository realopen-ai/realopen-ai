"""Code-scanning security regression tests."""

from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.api import sandboxes


@pytest.mark.asyncio
async def test_sandbox_failure_does_not_expose_internal_exception(monkeypatch):
    monkeypatch.setattr(
        sandboxes, "provision_sandbox", AsyncMock(side_effect=RuntimeError("SECRET /private/path"))
    )
    with pytest.raises(HTTPException) as caught:
        await sandboxes.create_sandbox(sandboxes.CreateSandbox(name="Demo"), db=AsyncMock())
    assert caught.value.status_code == 503
    assert "SECRET" not in caught.value.detail
    assert "/private" not in caught.value.detail
