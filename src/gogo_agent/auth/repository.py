"""User account repository providing data access and initial test seeds."""

from typing import Optional
from .models import UserAccount


class UserAccountRepository:
    """用户账户仓储，支持按用户名或用户ID检索，并提供更新密码哈希能力。"""

    def __init__(self, load_default_seeds: bool = True):
        self._accounts_by_user_id: dict[str, UserAccount] = {}
        self._accounts_by_username: dict[str, UserAccount] = {}
        if load_default_seeds:
            self.load_default_seeds()

    def load_default_seeds(self) -> None:
        """加载与原 Java schema.sql 完全一致的初始脱敏账号基线。

        注：为验证 004 要求的“旧账号密码数据哈希迁移并在隔离数据上验证”，
        初始内置账号保留历史明文密码 '123456'，在用户首次登录时会自动迁移为安全哈希。
        """
        seeds = [
            UserAccount(
                user_id="u_001",
                username="admin",
                password_hash="123456",
                real_name="系统管理员",
                role="ADMIN",
            ),
            UserAccount(
                user_id="u001",
                username="alice",
                password_hash="123456",
                real_name="张三",
                role="USER",
            ),
            UserAccount(
                user_id="u002",
                username="bob",
                password_hash="123456",
                real_name="李四",
                role="USER",
            ),
            UserAccount(
                user_id="u003",
                username="charlie",
                password_hash="123456",
                real_name="王五",
                role="USER",
            ),
            UserAccount(
                user_id="u004",
                username="david",
                password_hash="123456",
                real_name="赵六",
                role="USER",
            ),
        ]
        self._accounts_by_user_id.clear()
        self._accounts_by_username.clear()
        for acc in seeds:
            self.save(acc)

    def find_by_username(self, username: str) -> Optional[UserAccount]:
        """按用户名查找账户。"""
        if not username:
            return None
        return self._accounts_by_username.get(username.strip())

    def find_by_user_id(self, user_id: str) -> Optional[UserAccount]:
        """按用户唯一标识查找账户。"""
        if not user_id:
            return None
        return self._accounts_by_user_id.get(user_id.strip())

    def save(self, account: UserAccount) -> None:
        """保存或更新账户。"""
        self._accounts_by_user_id[account.user_id] = account
        self._accounts_by_username[account.username] = account

    def update_password_hash(self, user_id: str, new_hash: str) -> bool:
        """更新指定用户的密码哈希（透明迁移写回）。"""
        account = self.find_by_user_id(user_id)
        if not account:
            return False
        account.password_hash = new_hash
        self.save(account)
        return True
