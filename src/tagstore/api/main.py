from fastapi import FastAPI
from fastapi.security import OAuth2PasswordBearer
import uvicorn

from pydantic_settings import BaseSettings, EmailStr

import secrets

class Settings(BaseSettings):
    secret_key: str = secrets.token_hex(32)
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    class Config:
        env_file = ".env"

settings = Settings()

# TODO Figure out how you want to have end devices interact

# TODO: Secret key generation

# TODO: User auth/authz

api = FastAPI()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    user = await get_user(payload["sub"])
    if user is None:
        raise HTTPException(status_code=401)
    return user

async def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    if user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user

class UserIn(BaseModel):
    username: str
    password: str
    email: EmailStr
    full_name: str | None = None


class UserOut(BaseModel):
    username: str
    email: EmailStr
    full_name: str | None = None


class UserInDB(BaseModel):
    username: str
    hashed_password: str
    email: EmailStr
    full_name: str | None = None


def fake_password_hasher(raw_password: str):
    return "supersecret" + raw_password


def fake_save_user(user_in: UserIn):
    hashed_password = fake_password_hasher(user_in.password)
    user_in_db = UserInDB(**user_in.model_dump(), hashed_password=hashed_password)
    print("User saved! ..not really")
    return user_in_db


@app.post("/user/", response_model=UserOut)
async def create_user(user_in: UserIn):
    user_saved = fake_save_user(user_in)
    return user_saved

@api.get("/")
async def root():
    return {"message": "Hello World"}

def run():
    uvicorn.run(api, host="0.0.0.0", port=8000, reload=True)
    
if __name__ == "__main__":
    run()