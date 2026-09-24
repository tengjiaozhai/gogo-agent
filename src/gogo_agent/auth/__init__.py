"""Authentication and user session management package."""

from .models import UserAccount, LoginRequest, LoginResponse, UserInfoResponse
from .security import (
    InMemoryTokenStore,
    PasswordManager,
    RedisTokenStore,
    TokenManager,
    TokenStoreProtocol,
    create_token_manager,
)
from .service import AuthService
from .dependencies import get_current_user, require_admin

__all__ = [
    "UserAccount",
    "LoginRequest",
    "LoginResponse",
    "UserInfoResponse",
    "PasswordManager",
    "TokenStoreProtocol",
    "InMemoryTokenStore",
    "RedisTokenStore",
    "TokenManager",
    "create_token_manager",
    "AuthService",
    "get_current_user",
    "require_admin",
]
