"""Authentication: first-user-becomes-admin bootstrap, login/logout, and the
FastAPI dependencies other routers use to require a logged-in user (or an
admin) on a request.

Sessions are opaque random tokens stored in Postgres (see db/init.sql), not
JWTs — revoking access is just deleting a row, no token-blocklist needed.
Passwords are hashed with bcrypt. A simple failed-attempt counter on the
user row is a cheap brute-force backstop; it is not a substitute for
keeping this app off a network you don't trust, which matters more here
than it did for a public-news dashboard since this one holds real names,
photos, and reports on real people.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import psycopg2
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, Field

import audit
from db import db_cursor

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE_NAME = "session"
SESSION_LIFETIME = timedelta(hours=float(os.environ.get("SESSION_LIFETIME_HOURS", "12")))

# LAN-only deployments commonly run over plain HTTP with no TLS — a Secure
# cookie would just never get sent back by the browser and silently break
# login. Default to False so it works out of the box; set
# SESSION_COOKIE_SECURE=true once this sits behind HTTPS (a reverse proxy,
# Tailscale, etc.), which is worth doing precisely because this app holds
# more sensitive data than a public-news dashboard — see "Security" in
# README.md.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 10
VALID_ROLES = ("admin", "analyst")


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    role: str = Field(default="analyst")


class LoginRequest(BaseModel):
    username: str
    password: str


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, AttributeError):
        # Malformed/missing hash — fail closed rather than raising a 500.
        return False


def _any_users_exist() -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM users LIMIT 1")
        return cur.fetchone() is not None


def _start_session(user_id: int, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
    with db_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires_at),
        )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


def get_session_user(
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Optional[dict]:
    """FastAPI dependency: resolves the session cookie to a user dict, or
    None if there's no valid session. Use `require_user`/`require_admin`
    below on any route that actually needs to reject unauthenticated
    requests — this bare version stays importable for the few endpoints
    (like /register during bootstrap) that behave differently depending on
    whether someone happens to already be logged in."""
    if not session:
        return None
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.role, u.is_active, s.expires_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = %s
            """,
            (session,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        user_id, username, role, is_active, expires_at = row
        if not is_active or expires_at < datetime.now(timezone.utc):
            cur.execute("DELETE FROM sessions WHERE token = %s", (session,))
            return None
        cur.execute(
            "UPDATE sessions SET last_seen_at = now() WHERE token = %s",
            (session,),
        )
    return {"id": user_id, "username": username, "role": role}


def require_user(user: Optional[dict] = Depends(get_session_user)) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/bootstrap-status")
def bootstrap_status():
    """Frontend uses this to decide whether to render "create the first
    admin account" or a normal login form."""
    return {"needs_bootstrap": not _any_users_exist()}


@router.post("/register", status_code=201)
def register(
    payload: RegisterRequest,
    response: Response,
    current_user: Optional[dict] = Depends(get_session_user),
):
    """Open only in two cases: (1) no users exist yet — the very first
    registration bootstraps an admin account and logs it straight in, or
    (2) the caller is already an authenticated admin creating an account
    for someone else. Anyone else gets a 403 — there is no general-purpose
    open signup on an app that stores case data."""
    bootstrap = not _any_users_exist()
    if not bootstrap:
        if current_user is None or current_user["role"] != "admin":
            raise HTTPException(
                status_code=403,
                detail="Registration is closed — ask an admin to create your account",
            )
        if payload.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")

    role = "admin" if bootstrap else payload.role

    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE username = %s", (payload.username,))
        if cur.fetchone() is not None:
            raise HTTPException(status_code=409, detail="Username already taken")

    password_hash = _hash_password(payload.password)
    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) "
                "RETURNING id, username, role",
                (payload.username, password_hash, role),
            )
            new_id, new_username, new_role = cur.fetchone()
    except psycopg2.errors.UniqueViolation:
        # Rare race against the pre-check above (two simultaneous
        # registrations for the same username) — same friendly error either way.
        raise HTTPException(status_code=409, detail="Username already taken")

    new_user = {"id": new_id, "username": new_username, "role": new_role}
    audit.record(
        "user.create",
        user=current_user if not bootstrap else new_user,
        object_type="user", object_id=new_id, object_label=new_username,
        detail={"role": new_role, "bootstrap": bootstrap},
    )
    if bootstrap:
        # The person bootstrapping the instance shouldn't have to turn
        # around and log in again immediately after.
        _start_session(new_id, response)
    return new_user


@router.post("/login")
def login(payload: LoginRequest, response: Response):
    # IMPORTANT: never `raise` from inside the `with db_cursor(commit=True)`
    # block below. db_cursor is a @contextmanager — an exception raised in
    # the `with` body is thrown into the generator at its `yield` point,
    # which skips the `conn.commit()` line entirely, so any UPDATE just run
    # (e.g. recording a failed attempt) gets silently rolled back on
    # disconnect instead of persisted. Recording the outcome in a local
    # variable and raising *after* the block exits normally is what
    # actually lets the write commit.
    error_status = None
    error_detail = None
    authed_user_id = None
    authed_role = None

    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT id, password_hash, role, is_active, failed_login_attempts, locked_until "
            "FROM users WHERE username = %s",
            (payload.username,),
        )
        row = cur.fetchone()

        if row is None:
            # Constant-shape handling whether or not the username exists,
            # so a timing/response difference doesn't confirm which
            # usernames are registered. Still runs a bcrypt check either way.
            _hash_password(payload.password)
            error_status, error_detail = 401, "Invalid username or password"
        else:
            user_id, password_hash, role, is_active, failed_attempts, locked_until = row

            if locked_until is not None and locked_until > datetime.now(timezone.utc):
                error_status = 423
                error_detail = (
                    f"Account locked until {locked_until.isoformat()} "
                    "after repeated failed logins"
                )
            elif not is_active:
                error_status, error_detail = 403, "Account is disabled"
            elif not _verify_password(payload.password, password_hash):
                failed_attempts += 1
                lock_until = None
                if failed_attempts >= MAX_FAILED_ATTEMPTS:
                    lock_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                cur.execute(
                    "UPDATE users SET failed_login_attempts = %s, locked_until = %s WHERE id = %s",
                    (failed_attempts, lock_until, user_id),
                )
                error_status, error_detail = 401, "Invalid username or password"
            else:
                cur.execute(
                    "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
                    (user_id,),
                )
                authed_user_id, authed_role = user_id, role

    if error_detail is not None:
        # Failed logins are the single most useful thing in an audit trail, so
        # the attempted username is recorded even though no session exists.
        # The password never is, in any form.
        audit.record(
            "auth.login_failed",
            actor_username=payload.username,
            actor_kind="anonymous",
            object_type="user",
            object_label=payload.username,
            outcome="failure",
            detail={"reason": error_detail},
        )
        raise HTTPException(status_code=error_status, detail=error_detail)

    _start_session(authed_user_id, response)
    user = {"id": authed_user_id, "username": payload.username, "role": authed_role}
    audit.record("auth.login", user=user, object_type="user",
                 object_id=authed_user_id, object_label=payload.username)
    return user


@router.post("/logout")
def logout(
    response: Response,
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    actor = get_session_user(session=session)
    if session:
        with db_cursor(commit=True) as cur:
            cur.execute("DELETE FROM sessions WHERE token = %s", (session,))
    if actor:
        audit.record("auth.logout", user=actor, object_type="user",
                     object_id=actor["id"], object_label=actor["username"])
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "logged_out"}


@router.get("/me")
def me(user: dict = Depends(require_user)):
    return user
