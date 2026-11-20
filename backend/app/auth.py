"""
JWT authentication utilities.
"""
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .config import get_settings
from .database import get_db
from .models import User
from .schemas import TokenPayload

settings = get_settings()
security = HTTPBearer(auto_error=False)



def create_access_token(telegram_id: int, version: int = 0, scope: str = "viewer",
                        minutes: Optional[int] = None) -> str:
    """Create a JWT access token. scope = viewer | admin."""
    expire = datetime.utcnow() + timedelta(minutes=minutes or settings.jwt_expiry_minutes)
    payload = {
        "sub": str(telegram_id),  # Subject must be string
        "exp": expire,
        "type": "access",
        "ver": version,
        "scope": scope,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_refresh_token(telegram_id: int, version: int = 0) -> str:
    """Create a JWT refresh token (longer expiry)."""
    expire = datetime.utcnow() + timedelta(days=settings.jwt_refresh_days)
    payload = {
        "sub": str(telegram_id),  # Subject must be string
        "exp": expire,
        "type": "refresh",
        "ver": version
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_token_payload(token: str, token_type: str = "access") -> Optional[dict]:
    """Verify JWT token and return full payload if valid."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("type") != token_type:
            return None
        return payload
    except (JWTError, ValueError):
        return None


def verify_token(token: str, token_type: str = "access") -> Optional[int]:
    """Verify JWT token and return telegram_id if valid."""
    payload = verify_token_payload(token, token_type)
    if not payload:
        return None
    
    sub = payload.get("sub")
    return int(sub) if sub is not None else None



async def get_current_user(
    request: Request = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Dependency to get current authenticated user. Supports Bearer token or query param."""
    token = None
    
    # Try getting token from Authorization header
    if credentials:
        token = credentials.credentials
    
    # If not in header, try query parameter (for streaming/images)
    if not token and request and "token" in request.query_params:
        token = request.query_params["token"]
        
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Verify token
    try:
        payload = verify_token_payload(token)
        telegram_id = int(payload.get("sub")) if payload and payload.get("sub") else None
        token_version = payload.get("ver") if payload else None
    except Exception:
        telegram_id = None
        token_version = None
    
    if not telegram_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    
    # Check token version for global logout
    if token_version is not None and token_version < user.auth_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return user

async def require_admin(
    request: Request = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Like get_current_user, but the token must carry scope=admin.

    Admin tokens are issued only from the /up panel (POST /api/auth/admin-session).
    """
    token = credentials.credentials if credentials else None
    if not token and request is not None:
        token = request.query_params.get("token")
    payload = verify_token_payload(token or "")
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("scope") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin privileges required",
        )
    try:
        telegram_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if payload.get("ver") is not None and payload["ver"] < user.auth_version:
        raise HTTPException(status_code=401, detail="Session has been invalidated")
    return user

async def get_scope(
    request: Request = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    """Return the token scope: admin (issued from /up) or viewer."""
    token = credentials.credentials if credentials else None
    if not token and request is not None:
        token = request.query_params.get("token")
    payload = verify_token_payload(token or "")
    if payload and payload.get("scope") == "admin":
        return "admin"
    return "viewer"

def create_download_token(telegram_id: int, version: int = 0, folder_id: int = None,
                          days: int = 30) -> str:
    """Read-only token embedded in folder download lists (30 days).

    The token only unlocks files inside `folder_id`'s subtree.
    """
    expire = datetime.utcnow() + timedelta(days=days)
    payload = {
        "sub": str(telegram_id),
        "exp": expire,
        "type": "download",
        "ver": version,
        "scope": "viewer",
        "dlf": folder_id,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


async def stream_auth(
    request: Request = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Auth for media endpoints: accepts normal access tokens and 30-day download tokens."""
    token = credentials.credentials if credentials else None
    if not token and request is not None:
        token = request.query_params.get("token")
    payload = None
    if token:
        try:
            payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        except (JWTError, ValueError):
            payload = None
    if not payload or payload.get("type") not in ("access", "download"):
        raise HTTPException(status_code=401, detail="Missing or invalid token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        telegram_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if payload.get("ver") is not None and payload["ver"] < user.auth_version:
        raise HTTPException(status_code=401, detail="Session has been invalidated")
    scope = "admin" if payload.get("scope") == "admin" else "viewer"
    return user, scope, payload

