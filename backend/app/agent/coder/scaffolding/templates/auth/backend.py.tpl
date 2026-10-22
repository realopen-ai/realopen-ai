"""Local accounts and revocable sessions. No cloud service or signing secret."""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.db import connection

SESSION_SECONDS = 8 * 60 * 60
HASH_SLOTS = threading.BoundedSemaphore(2)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=48)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,47}", value):
            raise ValueError("Use 3–48 letters, numbers, dots, hyphens or underscores")
        return value


class User(BaseModel):
    id: int
    username: str


class SessionData(BaseModel):
    user: User
    csrf_token: str


def password_hash(password, salt):
    # scrypt's low-memory configuration: N=2^14, r=8, p=5, ~16 MiB per hash.
    # Bound concurrent work and keep expensive hashing off the event loop.
    if not HASH_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Authentication is busy; try again shortly")
    try:
        return hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=16384,
            r=8,
            p=5,
            dklen=64,
            maxmem=32 * 1024 * 1024,
        )
    finally:
        HASH_SLOTS.release()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(token):
    return hmac.new(token.encode(), b"csrf", hashlib.sha256).hexdigest()


def browser_write(request: Request):
    # Cross-origin forms cannot supply this header. No permissive CORS policy
    # is installed. Session-bound CSRF tokens additionally protect signed-in writes.
    if (
        request.headers.get("x-app-request") != "1"
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise HTTPException(403, "Use the application client for this request")


def install(app, services):
    router = APIRouter(prefix="/api/auth", tags=["auth"])
    state = {}

    def initialize():
        with connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS _auth_meta (id INTEGER PRIMARY KEY CHECK (id = 1), namespace TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO _auth_meta VALUES (1, ?)",
                (secrets.token_hex(12),),
            )
            state["cookie"] = (
                "roai_session_"
                + db.execute(
                    "SELECT namespace FROM _auth_meta WHERE id = 1"
                ).fetchone()[0]
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS _auth_users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, salt BLOB NOT NULL, password_hash BLOB NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS _auth_sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES _auth_users(id) ON DELETE CASCADE, expires_at INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS _auth_expiry ON _auth_sessions(expires_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS _auth_attempts (key TEXT PRIMARY KEY, started INTEGER NOT NULL, count INTEGER NOT NULL)"
            )

    def require_user(request: Request):
        token = request.cookies.get(state["cookie"], "")
        if len(token) != 64:
            raise HTTPException(401, "Sign in to continue")
        with connection() as db:
            row = db.execute(
                "SELECT u.id, u.username FROM _auth_sessions s JOIN _auth_users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
                (digest(token), int(time.time())),
            ).fetchone()
        if row is None:
            raise HTTPException(401, "Your session expired; sign in again")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            browser_write(request)
            if not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), csrf_token(token)
            ):
                raise HTTPException(
                    403, "Session verification failed; reload and try again"
                )
        return {"id": row["id"], "username": row["username"]}

    def throttle(request):
        now = int(time.time())
        key = digest(request.client.host if request.client else "local")
        with connection() as db:
            db.execute("DELETE FROM _auth_attempts WHERE started < ?", (now - 60,))
            db.execute(
                "INSERT INTO _auth_attempts VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET count = count + 1",
                (key, now),
            )
            count = db.execute(
                "SELECT count FROM _auth_attempts WHERE key = ?", (key,)
            ).fetchone()[0]
        if count > 30:
            raise HTTPException(
                429,
                "Too many authentication attempts; try again in a minute",
                headers={"Retry-After": "60"},
            )

    def session_response(user, request, response):
        token = secrets.token_hex(32)
        now = int(time.time())
        with connection() as db:
            db.execute(
                "DELETE FROM _auth_sessions WHERE expires_at <= ? OR token_hash = ?",
                (now, digest(request.cookies.get(state["cookie"], ""))),
            )
            db.execute(
                "INSERT INTO _auth_sessions VALUES (?, ?, ?)",
                (digest(token), user["id"], now + SESSION_SECONDS),
            )
        response.set_cookie(
            state["cookie"],
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=os.environ.get("AUTH_COOKIE_SECURE", "0") == "1"
            or request.url.scheme == "https",
            samesite="strict",
            path="/api",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"user": user, "csrf_token": csrf_token(token)}

    @router.post(
        "/register",
        status_code=201,
        response_model=SessionData,
        dependencies=[Depends(browser_write)],
    )
    def register(data: Credentials, request: Request, response: Response):
        throttle(request)
        salt = secrets.token_bytes(16)
        encoded = password_hash(data.password, salt)
        try:
            with connection() as db:
                cursor = db.execute(
                    "INSERT INTO _auth_users (username, salt, password_hash) VALUES (?, ?, ?)",
                    (data.username, salt, encoded),
                )
                user = {"id": cursor.lastrowid, "username": data.username}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username is unavailable") from None
        return session_response(user, request, response)

    @router.post(
        "/login", response_model=SessionData, dependencies=[Depends(browser_write)]
    )
    def login(data: Credentials, request: Request, response: Response):
        throttle(request)
        with connection() as db:
            row = db.execute(
                "SELECT * FROM _auth_users WHERE username = ?", (data.username,)
            ).fetchone()
        # Perform the same expensive hash for unknown users; do not expose account existence.
        encoded = password_hash(
            data.password, bytes(row["salt"]) if row else b"\0" * 16
        )
        if row is None or not hmac.compare_digest(encoded, bytes(row["password_hash"])):
            raise HTTPException(401, "Invalid username or password")
        return session_response(
            {"id": row["id"], "username": row["username"]}, request, response
        )

    @router.get("/session", response_model=SessionData)
    def session(request: Request, response: Response, user=Depends(require_user)):
        response.headers["Cache-Control"] = "no-store"
        return {
            "user": user,
            "csrf_token": csrf_token(request.cookies[state["cookie"]]),
        }

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response, user=Depends(require_user)):
        with connection() as db:
            db.execute(
                "DELETE FROM _auth_sessions WHERE token_hash = ?",
                (digest(request.cookies[state["cookie"]]),),
            )
        response.delete_cookie(
            state["cookie"], path="/api", httponly=True, samesite="strict"
        )
        response.headers["Cache-Control"] = "no-store"

    services.authenticated_user = require_user
    services.initializers.append(initialize)
    services.route_dependencies.append(Depends(require_user))
    app.include_router(router)
