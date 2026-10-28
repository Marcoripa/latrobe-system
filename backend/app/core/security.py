import logging
from pathlib import Path
from typing import Any, Callable, Literal

import firebase_admin
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth, credentials
from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger(__name__)

# Security scheme for extracting Bearer tokens from Authorization headers
bearer_scheme = HTTPBearer(auto_error=True)

RoleType = Literal["operator", "manager", "admin"]


class UserContext(BaseModel):
    """Authenticated user context extracted from a verified Firebase ID token."""

    uid: str
    email: str | None = None
    name: str | None = None
    role: RoleType = "operator"
    claims: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_manager_or_admin(self) -> bool:
        return self.role in ("manager", "admin")


def initialize_firebase() -> firebase_admin.App | None:
    """Initialize the Firebase Admin SDK idempotently using configured credentials."""
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass

    cred_path = Path(settings.FIREBASE_CREDENTIALS_PATH)
    try:
        if cred_path.is_file():
            cred = credentials.Certificate(str(cred_path))
            app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized with service account key at %s", cred_path)
            return app
        else:
            logger.warning(
                "Firebase credentials file not found at '%s'. Falling back to Application Default Credentials.",
                cred_path,
            )
            app = firebase_admin.initialize_app()
            logger.info("Firebase Admin initialized with Application Default Credentials.")
            return app
    except Exception as exc:
        logger.error("Failed to initialize Firebase Admin SDK: %s", exc)
        return None


# Initialize Firebase when the module is loaded
initialize_firebase()


def _extract_role(decoded_token: dict[str, Any]) -> RoleType:
    """Extract normalized role ('operator', 'manager', 'admin') from Firebase token claims."""
    raw_role = decoded_token.get("role")
    if isinstance(raw_role, str) and raw_role.lower() in ("admin", "manager", "operator"):
        return raw_role.lower()  # type: ignore[return-value]

    roles = decoded_token.get("roles")
    if isinstance(roles, list):
        lowered = {str(r).lower() for r in roles}
        if "admin" in lowered:
            return "admin"
        if "manager" in lowered:
            return "manager"
        if "operator" in lowered:
            return "operator"

    if decoded_token.get("admin") is True:
        return "admin"
    if decoded_token.get("manager") is True:
        return "manager"

    return "operator"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> UserContext:
    """Verify Firebase Bearer ID token and return the authenticated UserContext."""
    token = credentials.credentials
    try:
        decoded_token: dict[str, Any] = auth.verify_id_token(token)
    except auth.ExpiredIdTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase ID token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except auth.RevokedIdTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase ID token has been revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except auth.InvalidIdTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Firebase ID token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except Exception as exc:
        logger.error("Unexpected error verifying Firebase ID token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    uid = decoded_token.get("uid") or decoded_token.get("sub")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload is missing subject UID.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role = _extract_role(decoded_token)
    display_name = decoded_token.get("name") or decoded_token.get("display_name")
    email_val = decoded_token.get("email")

    if not display_name:
        try:
            user_record = auth.get_user(uid)
            display_name = user_record.display_name
            if not email_val:
                email_val = user_record.email
        except Exception:
            pass

    if not display_name and email_val:
        display_name = email_val.split("@")[0]

    return UserContext(
        uid=uid,
        email=email_val,
        name=display_name,
        role=role,
        claims=decoded_token,
    )


def require_roles(*allowed_roles: RoleType) -> Callable[[UserContext], UserContext]:
    """Factory creating a FastAPI dependency that restricts access to specific roles."""

    async def _role_checker(current_user: UserContext = Depends(get_current_user)) -> UserContext:
        if current_user.role not in allowed_roles:
            allowed_str = ", ".join(allowed_roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role(s): {allowed_str}. Current role: {current_user.role}.",
            )
        return current_user

    return _role_checker


async def require_manager(
    current_user: UserContext = Depends(get_current_user),
) -> UserContext:
    """FastAPI dependency ensuring the authenticated user has 'manager' or 'admin' privileges."""
    if not current_user.is_manager_or_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manager or Admin privileges required to perform this action.",
        )
    return current_user


async def require_admin(
    current_user: UserContext = Depends(get_current_user),
) -> UserContext:
    """FastAPI dependency ensuring the authenticated user has 'admin' privileges."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to perform this action.",
        )
    return current_user
