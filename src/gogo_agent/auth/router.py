"""FastAPI router for authentication endpoints: login, logout, and info."""

from fastapi import APIRouter, Depends, HTTPException, status

from .dependencies import extract_token_from_header, get_auth_service, get_current_user
from .models import LoginRequest, LoginResponse, LogoutResponse, UserAccount, UserInfoResponse
from .service import AuthService

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="用户登录并获取 Token",
)
async def login(
    req: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> LoginResponse:
    """用户登录接口。

    校验通过后返回 Token，前端后续请求需在 Header 中携带:
    Authorization: <token>
    """
    try:
        token = auth_service.login(req.username, req.password)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    return LoginResponse(token=token, tokenName="Authorization")


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="退出登录并注销当前 Token",
)
async def logout(
    token: str = Depends(extract_token_from_header),
    current_user: UserAccount = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> LogoutResponse:
    """退出登录接口。

    主动注销当前 Token 会话，销毁登录态。
    """
    auth_service.logout(token)
    return LogoutResponse(message="退出成功")


@router.get(
    "/info",
    response_model=UserInfoResponse,
    summary="获取当前已认证用户信息",
)
async def get_user_info(
    current_user: UserAccount = Depends(get_current_user),
) -> UserInfoResponse:
    """获取当前登录用户信息。

    服务端从已验证 Token 获取当前用户身份，不接受任何外部传入的伪造 userId。
    """
    return UserInfoResponse(
        userId=current_user.user_id,
        admin=current_user.is_admin(),
        realName=current_user.real_name,
    )
