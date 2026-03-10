"""
JWT & password utilities
"""
import secrets
import string
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import HTTPException, Request

from config import JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_DAYS, API_KEY_PREFIX

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalid or expired")


def require_token(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    return decode_token(token)


def require_admin(request: Request) -> dict:
    payload = require_token(request)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload


def generate_api_key() -> str:
    """Generate a random API key like 'oct-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'."""
    chars = string.ascii_letters + string.digits
    random_part = "".join(secrets.choice(chars) for _ in range(48))
    return f"{API_KEY_PREFIX}{random_part}"
