"""FastAPI authentication dependencies for trusted user context resolution."""

from typing import Optional
from fastapi import Depends, Header, HTTPException, status

from .models import UserAccount
from .service import AuthService

# 默认全局单例实例（支持在测试中覆盖替换）
_default_auth_service = AuthService()


def get_auth_service() -> AuthService:
    """获取全局 AuthService 实例。"""
    return _default_auth_service


def extract_token_from_header(authorization: Optional[str] = Header(None)) -> str:
    """从 Authorization 请求头提取 Token。

    兼容两种格式：
    1. 原前端格式: Authorization: <token>
    2. 标准 Bearer 格式: Authorization: Bearer <token>
    """
    if not authorization or not authorization.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Authorization 认证头",
        )

    raw = authorization.strip()
    if raw.lower().startswith("bearer "):
        token = raw[7:].strip()
    else:
        token = raw

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 Authorization 认证头",
        )
    return token


async def get_current_user(
    token: str = Depends(extract_token_from_header),
    auth_service: AuthService = Depends(get_auth_service),
) -> UserAccount:
    """可信当前用户解析依赖。

    从服务端已验证的 Token 中获取当前用户实体。
    严禁从请求体（Body）或模型生成文本中获取未受保护的 userId。
    """
    user = auth_service.get_user_by_token(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录态已失效",
        )
    return user


async def require_admin(
    current_user: UserAccount = Depends(get_current_user),
) -> UserAccount:
    """管理员权限拦截依赖，非管理员返回 HTTP 403。"""
    if not current_user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无管理员权限",
        )
    return current_user
