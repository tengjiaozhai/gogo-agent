"""Authentication service coordinating user validation, token issuance, and password upgrades."""

from typing import Optional
from .models import UserAccount
from .repository import UserAccountRepository
from .security import PasswordManager, TokenManager


class AuthService:
    """认证业务服务。"""

    def __init__(
        self,
        repository: Optional[UserAccountRepository] = None,
        token_manager: Optional[TokenManager] = None,
    ):
        self.repository = repository or UserAccountRepository()
        self.token_manager = token_manager or TokenManager()

    def login(self, username: str, plain_password: str) -> str:
        """用户登录校验。

        1. 检查账号是否存在
        2. 校验密码（支持安全哈希与兼容历史明文）
        3. 若为旧明文密码，透明自动升级为 PBKDF2 安全哈希并持久化写回
        4. 创建并返回鉴权 Token
        """
        if not username or not plain_password:
            raise ValueError("用户名或密码错误")

        account = self.repository.find_by_username(username)
        if not account:
            raise ValueError("用户名或密码错误")

        is_valid, needs_upgrade = PasswordManager.verify_password(
            plain_password, account.password_hash
        )
        if not is_valid:
            raise ValueError("用户名或密码错误")

        # 透明哈希升级（平滑迁移）
        if needs_upgrade:
            new_hash = PasswordManager.hash_password(plain_password)
            self.repository.update_password_hash(account.user_id, new_hash)

        return self.token_manager.create_token(account.user_id)

    def logout(self, token: str) -> bool:
        """销毁当前 Token 会话。"""
        return self.token_manager.revoke_token(token)

    def get_user_by_token(self, token: str) -> Optional[UserAccount]:
        """根据 Token 获取对应已认证的用户实体，失效或未找到时返回 None。"""
        user_id = self.token_manager.verify_token(token)
        if not user_id:
            return None
        return self.repository.find_by_user_id(user_id)

    def is_admin(self, user_id: str) -> bool:
        """检查指定用户是否具备管理员角色。"""
        if not user_id:
            return False
        account = self.repository.find_by_user_id(user_id)
        return account is not None and account.is_admin()
