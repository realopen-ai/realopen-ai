"""Generated behavior tests. Extend these for task-specific rules and UI flows."""

import pytest
from fastapi.testclient import TestClient
from backend.app import create_app

CASES = ENTITY_CASES


@pytest.mark.parametrize("name,payload,changes,required", CASES)
def test_resource_lifecycle(client, name, payload, changes, required):
    path = "/api/" + name
    response = client.post(path, json=payload)
    assert response.status_code == 201, response.text
    record = response.json()
    assert all(record[key] == value for key, value in payload.items())
    assert client.get(f"{path}/{record['id']}").json() == record
    page = client.get(path).json()
    assert page["total"] == 1 and page["items"] == [record]
    updated = client.patch(f"{path}/{record['id']}", json=changes)
    assert updated.status_code == 200
    assert updated.json() == {**record, **changes}
    # A fresh app/connection sees persistent changes, not in-memory test state.
    with TestClient(create_app()) as reopened:
        assert reopened.get(f"{path}/{record['id']}").json() == {**record, **changes}
    assert client.delete(f"{path}/{record['id']}").status_code == 204
    assert client.get(f"{path}/{record['id']}").status_code == 404
    assert client.patch(f"{path}/{record['id']}", json=changes).status_code == 404
    assert client.delete(f"{path}/{record['id']}").status_code == 404


@pytest.mark.parametrize("name,payload,changes,required", CASES)
def test_validation_and_listing(client, name, payload, changes, required):
    path = "/api/" + name
    assert (
        client.post(path, json={**payload, "unknown_field": "value"}).status_code == 422
    )
    for field in required:
        incomplete = {key: value for key, value in payload.items() if key != field}
        assert client.post(path, json=incomplete).status_code == 422
        assert client.post(path, json={**payload, field: None}).status_code == 422
    for _ in range(3):
        assert client.post(path, json=payload).status_code == 201
    assert len(client.get(path, params={"limit": 2, "offset": 1}).json()["items"]) == 2
    assert client.get(path, params={"sort": "invented"}).status_code == 422
    assert client.get(path, params={"limit": 0}).status_code == 422
