"""JWT authentication with two roles.

- POST /api/v1/auth/token exchanges username/password for a short-lived HS256 token.
- `require("admin")` / `require("viewer")` guard endpoints; admin implies viewer.
The signing secret is shared by all API replicas (JWT_SECRET), so a token issued by one replica
is valid on every other one behind the load balancer. Demo users come from the environment;
a real deployment would back this with an identity provider.
"""
from __future__ import annotations

import hmac
import os
import time
import uuid
from typing import Dict, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

ALGORITHM = "HS256"
TOKEN_TTL_S = int(os.getenv("JWT_TTL_S", "3600"))
ROLE_RANK = {"viewer": 1, "admin": 2}


def _secret() -> str:
    secret = os.getenv("JWT_SECRET", "")
    if len(secret) < 32:
        # Fail closed: without a strong shared secret no token can be issued or accepted.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not configured")
    return secret


def _users() -> Dict[str, tuple[str, str]]:
    users = {}
    for role in ("admin", "viewer"):
        name, pw = os.getenv(f"{role.upper()}_USERNAME"), os.getenv(f"{role.upper()}_PASSWORD")
        if name and pw:
            users[name] = (pw, role)
    return users


def issue_token(username: str, role: str) -> Dict:
    now = int(time.time())
    claims = {"sub": username, "role": role, "iat": now, "exp": now + TOKEN_TTL_S, "jti": uuid.uuid4().hex}
    return {"access_token": jwt.encode(claims, _secret(), algorithm=ALGORITHM), "token_type": "bearer",
            "expires_in": TOKEN_TTL_S, "role": role}


def decode_token(token: str) -> Dict:
    try:
        return jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["exp", "sub", "role"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token",
                            headers={"WWW-Authenticate": "Bearer"}) from None


_bearer = HTTPBearer(auto_error=False)


def require(role: str):
    """Dependency: a valid token whose role is at least `role`. Returns the token's claims."""
    def check(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> Dict:
        if credentials is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        claims = decode_token(credentials.credentials)
        if ROLE_RANK.get(claims.get("role"), 0) < ROLE_RANK[role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role}")
        return claims
    return check


class Login(BaseModel):
    username: str
    password: str


router = APIRouter()


@router.post("/api/v1/auth/token")
def login(body: Login):
    record = _users().get(body.username)
    # Compare against a dummy when the user doesn't exist, so timing doesn't reveal valid names.
    expected, role = record if record else ("\0" * 32, "viewer")
    if not hmac.compare_digest(body.password.encode(), expected.encode()) or record is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return issue_token(body.username, role)


@router.get("/api/v1/auth/me")
def me(claims: Dict = Depends(require("viewer"))):
    return {"username": claims["sub"], "role": claims["role"], "expires_at": claims["exp"]}
