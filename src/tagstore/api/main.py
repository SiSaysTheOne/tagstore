"""Minimal FastAPI service for tagstore, with username/password auth.

Auth follows FastAPI's own documented pattern - the OAuth2 "password" flow
with a JWT bearer token:

  1. ``POST /users`` - register with a username + password (public)
  2. ``POST /token``  - exchange username + password for a short-lived JWT
  3. every request after that carries ``Authorization: Bearer <token>``

Auth/authz is enforced with FastAPI's dependency-injection mechanism rather
than a bespoke decorator - that *is* the common/idiomatic pattern here
(FastAPI's own docs use it for exactly this). Two ways to require it:

* per-route: add ``user: UserInDB = Depends(get_current_active_user)`` to any
  path function's signature (see ``read_current_user`` below).
* per-group: attach ``dependencies=[Depends(...)]`` to an ``APIRouter`` (see
  ``protected_router`` / ``admin_router`` below) so every route registered on
  it requires auth without repeating the dependency on each one - this is the
  "make an arbitrary request require auth" mechanism.

``require_admin`` stacks a second, authorization-only check (role) on top of
``get_current_active_user``'s authentication + account-enabled check, so the
two concerns compose instead of being one big function.

Users live in an in-memory dict for now (``_users_db``) - swap ``get_user``/
``create_user`` for a real store (a DB, or a users.json in the bucket)
without touching any of the auth machinery below.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from pyparsing import Literal
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # TODO: How does the JWT token issued to users differ in payload besides the username? If it doesn't, we can just use the username as the token and skip JWT entirely.

    # Set SECRET_KEY in .env for a real deployment. Left unset, a fresh random
    # key is generated on every process start - every previously issued token
    # is invalidated on restart, and multiple worker processes won't agree
    # with each other on what a valid token looks like.
    secret_key: str = secrets.token_hex(32)
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30


settings = Settings()

api = FastAPI(title="tagstore")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(raw_password: str) -> str:
    return bcrypt.hashpw(raw_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(raw_password.encode("utf-8"), hashed_password.encode("utf-8"))


# ---------------------------------------------------------------------------
# User model + storage
# ---------------------------------------------------------------------------


class UserIn(BaseModel):
    """What a client submits to register. Never carries ``is_admin`` -
    privilege can only be granted server-side, not by a client's own request.
    """

    username: str
    password: str
    email: EmailStr
    full_name: str | None = None


class UserOut(BaseModel):
    """What a client is shown back - notably, never the password hash."""

    username: str
    email: EmailStr
    full_name: str | None = None
    is_admin: bool = False


class UserInDB(UserOut):
    hashed_password: str
    disabled: bool = False


# TODO Replace with a real store (a DB, or a users.json in the bucket) -
# this dict is only here so the auth flow has something to check against.
_users_db: dict[str, UserInDB] = {}


def get_user(username: str) -> UserInDB | None:
    return _users_db.get(username)


def authenticate_user(username: str, password: str) -> UserInDB | None:
    user = get_user(username)
    if user is None or not verify_password(password, user.hashed_password):
        return None
    return user


def _to_user_out(user: UserInDB) -> UserOut:
    """Strip storage-only fields (hashed_password, disabled) before returning a user."""
    return UserOut(
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
    )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


def create_access_token(username: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(
        {"sub": username, "exp": expires_at},
        settings.secret_key,
        algorithm=settings.algorithm,
    )


# ---------------------------------------------------------------------------
# Auth dependencies - the reusable "require auth on this route" building
# blocks. FastAPI extracts the bearer token via oauth2_scheme, which is also
# what documents the "Authorize" button in the auto-generated /docs UI.
# ---------------------------------------------------------------------------

# TODO How does Depends work to recursively resolve get_current_active_user -> get_current_user -> oauth2_scheme?

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


async def get_current_user(token: str = Depends(oauth2_scheme)) -> UserInDB:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        username = payload.get("sub")
    except jwt.PyJWTError:
        raise credentials_exception
    if username is None:
        raise credentials_exception
    user = get_user(username)
    if user is None:
        raise credentials_exception
    return user


async def get_current_active_user(user: UserInDB = Depends(get_current_user)) -> UserInDB:
    """Authentication, plus the most basic authorization check: is the account enabled."""
    if user.disabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user")
    return user


async def require_admin(user: UserInDB = Depends(get_current_active_user)) -> UserInDB:
    """A second, role-based authorization layer stacked on top of authentication."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


@api.get("/")
async def root():
    return {"message": "Hello World"}


@api.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(user_in: UserIn) -> UserOut:
    if get_user(user_in.username) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already registered")
    user = UserInDB(
        username=user_in.username,
        email=user_in.email,
        full_name=user_in.full_name,
        hashed_password=hash_password(user_in.password),
    )
    _users_db[user.username] = user
    return _to_user_out(user)

# Why does OAuth2PasswordRequestForm require `= Depends()`, but UserIn doesn't? 

@api.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()) -> Token:
    user = authenticate_user(form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Token(access_token=create_access_token(user.username))


# ---------------------------------------------------------------------------
# Protected routes
#
# Anything registered on `protected_router` requires a valid bearer token for
# an active account; anything on `admin_router` additionally requires
# `is_admin`. To make a new arbitrary route require auth, add it to one of
# these routers (or add the matching `Depends(...)` directly to its
# signature) - no per-route boilerplate needed.
# ---------------------------------------------------------------------------

protected_router = APIRouter(dependencies=[Depends(get_current_active_user)])


@protected_router.get("/users/me", response_model=UserOut)
async def read_current_user(user: UserInDB = Depends(get_current_active_user)) -> UserOut:
    return _to_user_out(user)


admin_router = APIRouter(dependencies=[Depends(require_admin)])


@admin_router.get("/users", response_model=list[UserOut])
async def list_users() -> list[UserOut]:
    return [_to_user_out(u) for u in _users_db.values()]

# TODO: Homepage
    # Filesystem limits
    # Usage by user

class FSStatus(BaseModel):
    total_files: int
    total_size_bytes: int
    used_size_bytes: int
    free_size_bytes: int
    last_updated: datetime

async def fs_status() -> FSStatus:
    # Implementation for fetching filesystem status
    pass

async def download_file(file_ids: list[str]) -> StreamingResponse:
    # Implementation for downloading files
    pass

# TODO: Download history

async def download_history(user: UserInDB) -> list[dict]:
    # Implementation for fetching download history for a user
    pass

# TODO: Upload file(s)

async def upload_files(files: list[UploadFile], user: UserInDB) -> dict:
    # Implementation for uploading files
    pass

# TODO: Upload history, status and validation results

UploadStatus = Literal["pending", "processing", "completed", "failed"]
class UploadHistoryEntry(BaseModel):
    file_id: list[str]
    status: UploadStatus
    upload_date: datetime

async def get_upload_history(user: UserInDB) -> list[UploadHistoryEntry]:
    # Implementation for fetching upload history for a user
    pass

# TODO: Paginated query of files
    # Include thumbnail
    # Include tags
    # Include author
    # Include upload date

class FileQueryResult(BaseModel):
    file_id: str
    thumbnail_url: str | None
    tags: list[str]

async def query_files(query: str, page: int = 1, page_size: int = 10) -> list[FileQueryResult]:
    # Implementation for querying files with pagination
    pass

api.include_router(protected_router)
api.include_router(admin_router)


def run():
    uvicorn.run("tagstore.api.main:api", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()
