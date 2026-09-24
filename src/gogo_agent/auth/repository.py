"""User account repository providing data access and initial test seeds."""

from typing import Optional
from .models import UserAccount


class InMemoryUserAccountRepository:
    """内存用户账户仓储，供轻量无外部依赖的单测环境使用。"""

    def __init__(self, load_default_seeds: bool = True):
        self._accounts_by_user_id: dict[str, UserAccount] = {}
        self._accounts_by_username: dict[str, UserAccount] = {}
        if load_default_seeds:
            self.load_default_seeds()

    def load_default_seeds(self) -> None:
        """加载与原 Java schema.sql 完全一致的初始脱敏账号基线。"""
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


class SQLUserAccountRepository:
    """基于 SQLAlchemy 连接 MariaDB/MySQL 的生产级用户账户仓储。"""

    def __init__(self, session_factory=None):
        from gogo_agent.db.session import get_session_factory
        self._session_factory = session_factory or get_session_factory()

    def _to_domain(self, row) -> Optional[UserAccount]:
        if row is None:
            return None
        return UserAccount(
            user_id=row.user_id,
            username=row.username,
            password_hash=row.password,
            real_name=row.real_name,
            role=row.role,
        )

    def find_by_username(self, username: str) -> Optional[UserAccount]:
        if not username or not self._session_factory:
            return None
        from sqlalchemy import select
        from gogo_agent.db.models import UserAccountModel

        with self._session_factory() as session:
            stmt = select(UserAccountModel).where(UserAccountModel.username == username.strip())
            row = session.scalar(stmt)
            return self._to_domain(row)

    def find_by_user_id(self, user_id: str) -> Optional[UserAccount]:
        if not user_id or not self._session_factory:
            return None
        from sqlalchemy import select
        from gogo_agent.db.models import UserAccountModel

        with self._session_factory() as session:
            stmt = select(UserAccountModel).where(UserAccountModel.user_id == user_id.strip())
            row = session.scalar(stmt)
            return self._to_domain(row)

    def save(self, account: UserAccount) -> None:
        if not self._session_factory:
            return
        from sqlalchemy import select
        from gogo_agent.db.models import UserAccountModel

        with self._session_factory() as session:
            stmt = select(UserAccountModel).where(UserAccountModel.user_id == account.user_id)
            row = session.scalar(stmt)
            if row:
                row.username = account.username
                row.password = account.password_hash
                row.real_name = account.real_name
                row.role = account.role
            else:
                row = UserAccountModel(
                    user_id=account.user_id,
                    username=account.username,
                    password=account.password_hash,
                    real_name=account.real_name,
                    role=account.role,
                )
                session.add(row)
            session.commit()

    def update_password_hash(self, user_id: str, new_hash: str) -> bool:
        if not self._session_factory:
            return False
        from sqlalchemy import update
        from gogo_agent.db.models import UserAccountModel

        with self._session_factory() as session:
            stmt = (
                update(UserAccountModel)
                .where(UserAccountModel.user_id == user_id.strip())
                .values(password=new_hash)
            )
            result = session.execute(stmt)
            session.commit()
            return result.rowcount > 0


# 保持向后兼容：UserAccountRepository 默认仍指向内存实现以保证纯单元测试隔离
UserAccountRepository = InMemoryUserAccountRepository


def create_user_account_repository():
    """工厂方法：若已配置真实数据库则优先使用 SQLUserAccountRepository，否则使用内存仓储。"""
    from gogo_agent.db.session import get_session_factory
    if get_session_factory() is not None:
        return SQLUserAccountRepository()
    return InMemoryUserAccountRepository()

