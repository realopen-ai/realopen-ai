"""Authentication and cross-user authorization checks against an isolated database."""

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import connection

CASES = AUTH_CASES
PASSWORD = "test-only-long-password"


def register(client, username):
    client.headers["X-App-Request"] = "1"
    response = client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def test_sessions_password_storage_and_revocation(client):
    assert client.get("/api/auth/session").status_code == 401
    registered = register(client, "tester")
    user = registered.json()["user"]
    assert set(user) == {"id", "username"}
    cookie = next(iter(client.cookies.values()))
    headers = registered.headers["set-cookie"].lower()
    assert (
        "httponly" in headers
        and "samesite=strict" in headers
        and "path=/api" in headers
    )
    with connection() as db:
        stored = db.execute("SELECT * FROM _auth_users").fetchone()
        token_hash = db.execute("SELECT token_hash FROM _auth_sessions").fetchone()[0]
    assert PASSWORD.encode() not in bytes(stored["password_hash"])
    assert len(stored["salt"]) == 16 and len(stored["password_hash"]) == 64
    assert token_hash == hashlib.sha256(cookie.encode()).hexdigest()
    with TestClient(create_app()) as reopened:
        reopened.cookies.update(client.cookies)
        assert reopened.get("/api/auth/session").json()["user"] == user
    old_cookies = type(client.cookies)(client.cookies)
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.update(old_cookies)
    assert client.get("/api/auth/session").status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "tester", "password": "wrong-password-value"},
        ).status_code
        == 401
    )
    response = client.post(
        "/api/auth/login", json={"username": "TESTER", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert next(iter(client.cookies.values())) != cookie
    assert client.get("/api/auth/session").json()["user"] == user


def test_csrf_expiration_and_validation(client):
    payload = {"username": "tester", "password": PASSWORD}
    assert client.post("/api/auth/register", json=payload).status_code == 403
    register(client, "tester")
    assert client.post("/api/auth/register", json=payload).status_code == 409
    assert (
        client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/register", json={"username": "new_user", "password": "short"}
        ).status_code
        == 422
    )
    with connection() as db:
        db.execute("UPDATE _auth_sessions SET expires_at = ?", (int(time.time()) - 1,))
    assert client.get("/api/auth/session").status_code == 401


def test_authentication_attempts_are_bounded(client):
    register(client, "tester")
    with connection() as db:
        db.execute("UPDATE _auth_attempts SET count = 30")
    response = client.post(
        "/api/auth/login", json={"username": "tester", "password": PASSWORD}
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "60"


def test_secure_cookie_and_session_rotation(client, monkeypatch):
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "1")
    with TestClient(create_app(), base_url="https://testserver") as secure:
        response = register(secure, "tester")
        assert "secure" in response.headers["set-cookie"].lower()
        old_cookies = type(secure.cookies)(secure.cookies)
        logged_in = secure.post(
            "/api/auth/login", json={"username": "tester", "password": PASSWORD}
        )
        assert logged_in.status_code == 200
        assert secure.get("/api/auth/session").status_code == 200
        with TestClient(create_app(), base_url="https://testserver") as stale:
            stale.cookies.update(old_cookies)
            assert stale.get("/api/auth/session").status_code == 401


def test_each_account_has_an_independent_password_salt(client):
    register(client, "first_user")
    register(client, "second_user")
    with connection() as db:
        values = db.execute(
            "SELECT salt, password_hash FROM _auth_users ORDER BY id"
        ).fetchall()
    assert values[0]["salt"] != values[1]["salt"]
    assert values[0]["password_hash"] != values[1]["password_hash"]


@pytest.mark.parametrize("name,payload,changes,required", CASES)
def test_anonymous_access_and_cross_user_records(
    client, name, payload, changes, required
):
    path = "/api/" + name
    assert client.get(path).status_code == 401
    assert client.post(path, json=payload).status_code == 401
    alice = register(client, "alice").json()["user"]
    notifications = []

    async def listener(event):
        notifications.append(event)

    client.app.state.services.mutation_listeners.append(listener)
    assert (
        client.post(path, json=payload, headers={"X-CSRF-Token": "wrong"}).status_code
        == 403
    )
    first = client.post(path, json=payload).json()
    if "owner_id" in client.app.state.services.scope_columns:
        assert notifications[-1]["scope"] == {"owner_id": alice["id"]}
    alice_cookies, alice_headers = type(client.cookies)(client.cookies), dict(
        client.headers
    )
    client.cookies.clear()
    register(client, "bob")
    if "owner_id" in client.app.state.services.scope_columns:
        assert client.get(path).json()["total"] == 0
        for suffix in ("", "?search=Example", "?limit=1&offset=0"):
            assert client.get(path + suffix).json()["items"] == []
        target = f"{path}/{first['id']}"
        assert client.get(target).status_code == 404
        assert client.patch(target, json=changes).status_code == 404
        assert client.delete(target).status_code == 404
        assert len(notifications) == 1
        assert client.post(path, json={**payload, "owner_id": 1}).status_code == 422
        second = client.post(path, json=payload).json()
        assert (
            client.patch(f"{path}/{second['id']}", json={"owner_id": 1}).status_code
            == 422
        )
        assert client.get(path).json()["items"] == [second]
        client.cookies.clear()
        client.cookies.update(alice_cookies)
        client.headers.update(alice_headers)
        assert client.get(target).json() == first
        assert client.get(path).json()["items"] == [first]
    else:
        # Auth-only intentionally shares records between signed-in accounts.
        assert client.get(path).json()["items"] == [first]
