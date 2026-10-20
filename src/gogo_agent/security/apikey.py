"""用户级第三方 API Key 管理（059）：加密存储、脱敏展示、仅进程内取明文。

对照 Java ``apikey/service/ApiKeyService.java``、``apikey/entity/UserApiKeyEntry.java`` 与
``agent/tools/ApiKeyTools.java``。Python 版的差异：
- 密文用 ``SecretCipher``（AES-GCM，带版本），并把 (user_id, provider) 作为绑定数据；
- 表里多了密钥版本、脱敏后缀、状态与时间；撤销保留行但清空密文，旧 Key 立即不可再取用；
- 没有 Redis 缓存层：缓存会让“撤销后立即失效”依赖额外的失效广播，本编号不引入；
- 所有方法的 ``user_id`` 必须来自服务端可信身份（如 ``RequestContext``），不来自模型参数；
  不存在“管理员查看他人明文”的入口。

``resolve_for_use`` 是唯一返回明文的方法，只能由服务端在进程内注入请求头/环境变量时调用，
返回值是 ``SecretStr``，``repr``/``str`` 不会打印明文；不得放进提示词、工具响应或日志。
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import DateTime, Engine, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.db.base import Base
from gogo_agent.db.session import get_session_factory
from gogo_agent.security.cipher import SecretCipher, SecretCipherError
from gogo_agent.security.errors import SecurityError
from gogo_agent.security.masking import SensitiveMasker

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
INVALID_INPUT = "API_KEY_INVALID_INPUT"
NOT_CONFIGURED = "API_KEY_NOT_CONFIGURED"
UNREADABLE = "API_KEY_UNREADABLE"
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_KEY_LEN = 512


class ApiKeyError(SecurityError):
    """API Key 管理失败；错误文本不含明文 Key。"""


class UserApiKeyModel(Base):
    """用户第三方 API Key 表：只存 AES-GCM 密文、密钥版本与脱敏后缀，业务键 (user_id, provider)。"""

    __tablename__ = "user_api_key"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_user_api_key_user_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键自增 ID")
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="Key 所有者的认证用户标识")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, comment="服务商标识，如 tuniu-cli、flight-manager")
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, comment="AES-GCM 密文（版本.base64url）；已撤销时为空串")
    key_version: Mapped[str] = mapped_column(String(16), nullable=False, comment="加密所用密钥版本，轮换时据此找出待迁移行；已撤销时为空串")
    key_suffix: Mapped[str] = mapped_column(String(8), nullable=False, default="", comment="用于脱敏展示的明文末 4 位；Key 过短时为空串")
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="状态：active 可用 / revoked 已撤销（密文已清空）")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="首次保存时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="最近一次保存或撤销时间（UTC）")


class ApiKeyInfo(BaseModel):
    """对外展示的 Key 信息：只含脱敏展示串，不含明文或密文。"""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., description="服务商标识")
    status: str = Field(..., description="状态：active 可用 / revoked 已撤销")
    masked_key: str = Field(..., description="脱敏展示串，形如 ****abcd；已撤销或过短时为 ****")
    key_version: str = Field(..., description="当前密文使用的密钥版本；已撤销时为空串")
    created_at: datetime = Field(..., description="首次保存时间（UTC）")
    updated_at: datetime = Field(..., description="最近一次保存或撤销时间（UTC）")


def create_memory_session_factory() -> sessionmaker[Session]:
    """每次调用返回一个隔离的内存 SQLite 会话工厂，只建本模块的表。"""
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[UserApiKeyModel.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_api_key_service(
    cipher: SecretCipher | None = None, session_factory: sessionmaker[Session] | None = None,
) -> "ApiKeyService":
    """配置了数据库时使用项目会话，否则使用隔离内存 SQLite；密钥只从环境变量读取。"""
    factory = session_factory or get_session_factory() or create_memory_session_factory()
    return ApiKeyService(factory, cipher or SecretCipher.from_env())


def _aad(user_id: str, provider: str) -> str:
    return f"{user_id}\x1f{provider}"


class ApiKeyService:
    """用户只能增删查自己的 Key；除 ``resolve_for_use`` 外都不返回明文。"""

    def __init__(
        self, session_factory: sessionmaker[Session], cipher: SecretCipher,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._session_factory = session_factory
        self._cipher = cipher
        self._clock = clock

    def save(self, user_id: str, provider: str, api_key: str) -> ApiKeyInfo:
        """新增或更新（含撤销后重新启用）；更新后旧密文立即被覆盖，不可再取用。"""
        user_id, provider = self._check_identity(user_id, provider)
        api_key = api_key.strip() if isinstance(api_key, str) else ""
        if not api_key or len(api_key) > _MAX_KEY_LEN or any(ch.isspace() for ch in api_key):
            raise ApiKeyError(INVALID_INPUT, "API Key 为空、过长或包含空白字符")
        try:
            info = self._upsert(user_id, provider, api_key)
        except IntegrityError:  # 并发首次保存撞唯一键：此时行已存在，按更新重试一次
            info = self._upsert(user_id, provider, api_key)
        logger.info("[ApiKey] 已加密保存 user_id=%s provider=%s key_version=%s", user_id, provider, info.key_version)
        return info

    def revoke(self, user_id: str, provider: str) -> bool:
        """撤销自己的 Key：清空密文并置 revoked；原本没有 active Key 时返回 False。"""
        user_id, provider = self._check_identity(user_id, provider)
        with self._session_factory() as session, session.begin():
            row = self._row(session, user_id, provider)
            if row is None or row.status != STATUS_ACTIVE:
                return False
            row.api_key_enc, row.key_version, row.key_suffix = "", "", ""
            row.status, row.updated_at = STATUS_REVOKED, self._clock()
        logger.info("[ApiKey] 已撤销 user_id=%s provider=%s", user_id, provider)
        return True

    def get_info(self, user_id: str, provider: str) -> Optional[ApiKeyInfo]:
        """查询自己的某个 Key 的脱敏信息；不存在返回 None。"""
        user_id, provider = self._check_identity(user_id, provider)
        with self._session_factory() as session:
            row = self._row(session, user_id, provider)
            return self._info(row) if row else None

    def has_active(self, user_id: str, provider: str) -> bool:
        """是否存在可用 Key（对应 Java 的 ``hasApiKey``，不解密）。"""
        info = self.get_info(user_id, provider)
        return info is not None and info.status == STATUS_ACTIVE

    def list_for_user(self, user_id: str) -> list[ApiKeyInfo]:
        """列出自己的全部 Key 的脱敏信息。"""
        user_id = self._check_user(user_id)
        with self._session_factory() as session:
            rows = session.scalars(
                select(UserApiKeyModel).where(UserApiKeyModel.user_id == user_id).order_by(UserApiKeyModel.provider)
            )
            return [self._info(row) for row in rows]

    def resolve_for_use(self, user_id: str, provider: str) -> SecretStr:
        """唯一取明文入口，仅限服务端进程内注入；没有 active Key 或无法解密时失败，绝不回退到别人的 Key。"""
        user_id, provider = self._check_identity(user_id, provider)
        with self._session_factory() as session:
            row = self._row(session, user_id, provider)
            if row is None or row.status != STATUS_ACTIVE:
                raise ApiKeyError(NOT_CONFIGURED, "当前用户未配置该服务商的可用 API Key")
            token = row.api_key_enc
        try:
            return SecretStr(self._cipher.decrypt(token, _aad(user_id, provider)))
        except SecretCipherError as error:
            logger.warning("[ApiKey] 密文无法解密 user_id=%s provider=%s code=%s", user_id, provider, error.code)
            raise ApiKeyError(UNREADABLE, "已保存的 API Key 无法解密，请重新配置") from None

    def rotate_to_current_key(self) -> int:
        """把仍使用旧密钥版本的 active 行重新加密到当前密钥，返回迁移行数。"""
        rotated = 0
        with self._session_factory() as session, session.begin():
            rows = session.scalars(select(UserApiKeyModel).where(
                UserApiKeyModel.status == STATUS_ACTIVE,
                UserApiKeyModel.key_version != self._cipher.current_version,
            ))
            for row in rows:
                row.api_key_enc = self._cipher.reencrypt(row.api_key_enc, _aad(row.user_id, row.provider))
                row.key_version = self._cipher.current_version
                rotated += 1
        logger.info("[ApiKey] 密钥轮换完成 rotated=%d current_version=%s", rotated, self._cipher.current_version)
        return rotated

    @staticmethod
    def _check_user(user_id: str) -> str:
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 64:
            raise ApiKeyError(INVALID_INPUT, "缺少可信用户标识")
        return user_id

    @classmethod
    def _check_identity(cls, user_id: str, provider: str) -> tuple[str, str]:
        if not isinstance(provider, str) or not _PROVIDER.match(provider.strip().lower()):
            raise ApiKeyError(INVALID_INPUT, "服务商标识不合法")
        return cls._check_user(user_id), provider.strip().lower()

    @staticmethod
    def _row(session: Session, user_id: str, provider: str) -> Optional[UserApiKeyModel]:
        return session.scalar(select(UserApiKeyModel).where(
            UserApiKeyModel.user_id == user_id, UserApiKeyModel.provider == provider,
        ))

    def _upsert(self, user_id: str, provider: str, api_key: str) -> ApiKeyInfo:
        now = self._clock()
        with self._session_factory() as session, session.begin():
            row = self._row(session, user_id, provider)
            if row is None:
                row = UserApiKeyModel(user_id=user_id, provider=provider, created_at=now)
                session.add(row)
            row.api_key_enc = self._cipher.encrypt(api_key, _aad(user_id, provider))
            row.key_version = self._cipher.current_version
            row.key_suffix = SensitiveMasker.secret_suffix(api_key)
            row.status, row.updated_at = STATUS_ACTIVE, now
            session.flush()
            return self._info(row)

    @staticmethod
    def _info(row: UserApiKeyModel) -> ApiKeyInfo:
        masked = "****" + row.key_suffix if row.status == STATUS_ACTIVE else "****"
        return ApiKeyInfo(
            provider=row.provider, status=row.status, masked_key=masked, key_version=row.key_version,
            created_at=row.created_at, updated_at=row.updated_at,
        )
