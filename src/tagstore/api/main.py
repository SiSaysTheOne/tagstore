"""Minimal FastAPI service for tagstore, with username/password auth.

Auth follows FastAPI's own documented pattern - the OAuth2 "password" flow with a JWT bearer token:

  1. ``POST /users`` - register with a username + password (public)
  2. ``POST /token``  - exchange username + password for a short-lived JWT
  3. every request after that carries ``Authorization: Bearer <token>``

Auth/authz is enforced with FastAPI's dependency-injection mechanism (from the FastAPI docs). Two ways to require it:

* per-route: add ``user: UserInDB = Depends(get_current_active_user)`` to any path function's signature 
* per-group: attach ``dependencies=[Depends(...)]`` to an ``APIRouter`` so every arbitrary route registered on it requires auth without repeating the dependency

``require_admin`` stacks a second, authorization-only check (role) on top of
``get_current_active_user``'s authentication + account-enabled check, so the
two concerns compose instead of being one big function.

Users live in an in-memory dict for now (``_users_db``) - swap ``get_user``/
``create_user`` for a real store (a DB, or a users.json in the bucket)
without touching any of the auth machinery below.
"""

# TODO: Assume a single filepool for now, but eventually there can be multiple.

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from pyparsing import Literal
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Annotated
from pydantic_settings import BaseSettings, SettingsConfigDict

# TODO: Declare Pydantic models with Annotated ??

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Left unset, a fresh random key is generated on every process start. (Every previously issued token is invalidated on restart)
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

# TODO: Determine best way to keep the set of roles
Roles = Literal["user", "admin"]

UploadStatus = Literal["pending", "processing", "completed", "failed"]
class UploadHistoryEntry(BaseModel):
    file_id: list[str]
    status: UploadStatus
    upload_date: datetime

class FSStatus(BaseModel):
    total_files: int
    total_size_bytes: int
    used_size_bytes: int
    free_size_bytes: int
    last_updated: datetime

class UserIn(BaseModel):
    """What a client submits to register. Never carries role information."""

    username: str
    password: str
    email: EmailStr
    full_name: str | None = None


class UserOut(BaseModel):
    """What a client is shown back. Never carries the password hash."""

    username: str
    email: EmailStr
    full_name: str | None = None
    role: str = "user"


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
        role=user.role,
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

protected_router = APIRouter(dependencies=[Depends(get_current_active_user)])
api.include_router(protected_router)

async def require_admin(user: UserInDB = Depends(get_current_active_user)) -> UserInDB:
    """A second, role-based authorization layer stacked on top of authentication."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user

admin_router = APIRouter(dependencies=[Depends(require_admin)])
api.include_router(admin_router)

# ---------------------------------------------------------------------------
# Routes
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

@protected_router.post("/users/me", response_model=UserOut)
async def read_current_user(user: UserInDB = Depends(get_current_active_user)) -> UserOut:
    return _to_user_out(user)

# TODO: Do the routes require double-declaring the response model/return value?

@admin_router.post("/users", response_model=list[UserOut])
async def list_users() -> list[UserOut]:
    return [_to_user_out(u) for u in _users_db.values()]

@admin_router.post("/system/fs_status", response_model=FSStatus)
async def system_fs_status() -> FSStatus:
    # Implementation for fetching filesystem status
    pass

@admin_router.post("/users/fs_status", response_model=list[FSStatus])
async def users_fs_status() -> list[FSStatus]:
    # Implementation for fetching filesystem status for all users
    pass

@protected_router.post("/users/me/fs_status", response_model=FSStatus)
async def user_fs_status(user: UserInDB = Depends(get_current_active_user)) -> FSStatus:
    # Implementation for fetching user-specific filesystem status
    pass

@protected_router.post("/files/", response_model=FSStatus)
async def download_file(file_ids: list[str]) -> StreamingResponse:
    # Implementation for downloading files
    pass

@protected_router.post("/users/me/download_history", response_model=list[dict])
async def download_history(user: UserInDB = Depends(get_current_active_user)) -> list[dict]:
    # Implementation for fetching download history for a user
    pass

@protected_router.post("/users/me/upload", response_model=FSStatus)
async def upload_files(files: list[UploadFile], user: UserInDB = Depends(get_current_active_user)) -> dict:
    # Implementation for uploading files
    pass

@protected_router.post("/users/me/upload_history", response_model=list[UploadHistoryEntry])
async def get_upload_history(user: UserInDB = Depends(get_current_active_user)) -> list[UploadHistoryEntry]:
    # Implementation for fetching upload history for a user
    pass

@protected_router.post("/files/query", response_model=list[str])
async def query_files(user: UserInDB = Depends(get_current_active_user), query: str = "") -> list[str]:
    # Implementation for querying files to get file IDs
    pass

async def get_file_thumbnails(file_ids: list[str]) -> list[bytes]:
    # Implementation for fetching thumbnails for given file IDs
    pass

class FileMetadata(BaseModel):
    file_id: str
    owner: str
    upload_date: datetime
    tags: list[str]
    size_bytes: int


async def get_file_metadata(file_ids: list[str]) -> list[FileMetadata]:
    # Implementation for fetching metadata for given file IDs
    pass

# TODO: 







def run():
    uvicorn.run("tagstore.api.main:api", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()
