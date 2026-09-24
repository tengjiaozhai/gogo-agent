"""Data models for authentication, accounts, and session tokens."""

from typing import Optional
from pydantic import BaseModel, Field


class UserAccount(BaseModel):
    """用户登录账号模型，对应数据表 user_account。"""

    user_id: str = Field(..., description="用户唯一标识 (关联 user_profile.user_id)")
    username: str = Field(..., description="登录账号")
    password_hash: str = Field(..., description="密码哈希值或兼容的待升级明文")
    real_name: Optional[str] = Field(default=None, description="真实姓名")
    role: str = Field(default="USER", description="角色: USER / ADMIN")

    def is_admin(self) -> bool:
        """是否为管理员。"""
        return self.role.upper() == "ADMIN"


class LoginRequest(BaseModel):
    """用户登录请求体。"""

    username: str = Field(..., description="登录账号")
    password: str = Field(..., description="登录密码")


class LoginResponse(BaseModel):
    """登录成功响应体，严格对齐原前端契约。"""

    token: str = Field(..., description="鉴权令牌")
    tokenName: str = Field(default="Authorization", description="前端需携带的 Header 名称")


class LogoutResponse(BaseModel):
    """退出登录响应。"""

    message: str = Field(default="退出成功", description="退出结果消息")


class UserInfoResponse(BaseModel):
    """当前用户信息响应体。"""

    userId: str = Field(..., description="当前用户ID")
    admin: bool = Field(default=False, description="是否具备管理员权限")
    realName: Optional[str] = Field(default=None, description="用户真实姓名")
