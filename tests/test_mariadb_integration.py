"""MariaDB integration test suite.

验证真实连接 172.22.22.116:3306 的 test 数据库：
1. 验证表结构初始化与用户数据查询；
2. 验证真实数据库中明文密码向 PBKDF2 安全哈希的透明升级与落库；
3. 验证端到端从数据库登录认证并成功返回 Token 与用户信息。
"""

from datetime import datetime, timezone
import os
import pytest
from sqlalchemy import select, text
from gogo_agent.db.session import get_session_factory
from gogo_agent.db.models import UserAccountModel
from gogo_agent.db.init_db import init_database
from gogo_agent.auth.repository import SQLUserAccountRepository
from gogo_agent.auth.service import AuthService
from gogo_agent.auth.security import TokenManager


@pytest.fixture
def db_session_factory():
    factory = get_session_factory()
    if not factory:
        pytest.skip("GOGO_DATABASE_URL 未配置，跳过 MariaDB 集成测试。")
    return factory


def test_init_database_idempotent():
    """验证 init_database 幂等执行无异常。"""
    success = init_database()
    assert success is True



def test_mariadb_connection_and_user_query(db_session_factory):
    """验证真实 MariaDB 连接与种子账号查询。"""
    repo = SQLUserAccountRepository(session_factory=db_session_factory)
    alice = repo.find_by_username("alice")
    assert alice is not None
    assert alice.user_id == "u001"
    assert alice.real_name == "张三"
    assert alice.role == "USER"

    admin = repo.find_by_username("admin")
    assert admin is not None
    assert admin.user_id == "u_001"
    assert admin.is_admin() is True


def test_mariadb_transparent_password_hash_migration(db_session_factory):
    """验证从 MariaDB 读取旧明文并在用户真实登录后原地升级为 PBKDF2 哈希。"""
    repo = SQLUserAccountRepository(session_factory=db_session_factory)
    auth_service = AuthService(repository=repo, token_manager=TokenManager())

    # 1. 临时重置 david 账号为明文密码 '123456'
    with db_session_factory() as session:
        session.execute(
            text("UPDATE user_account SET password = '123456' WHERE username = 'david'")
        )
        session.commit()

    # 确认数据库中此时为明文
    with db_session_factory() as session:
        pwd_before = session.scalar(
            select(UserAccountModel.password).where(UserAccountModel.username == "david")
        )
        assert pwd_before == "123456"

    # 2. 执行真实登录
    token = auth_service.login("david", "123456")
    assert token is not None and token.startswith("tk_")

    # 3. 检查 MariaDB 数据库中的密码已自动透明升级为 PBKDF2 哈希
    with db_session_factory() as session:
        pwd_after = session.scalar(
            select(UserAccountModel.password).where(UserAccountModel.username == "david")
        )
        assert pwd_after.startswith("pbkdf2:sha256:100000:")
        assert "123456" not in pwd_after

    # 4. 再次使用原密码 '123456' 登录，验证通过
    token_second = auth_service.login("david", "123456")
    assert token_second is not None


def test_mariadb_chat_history_and_agent_session(db_session_factory):
    """验证真实 MariaDB 数据库上 chat_conversation, chat_message 和 agentscope_session 的完整持久化链路。"""
    from gogo_agent.chat.repository import SQLChatHistoryRepository, SQLAgentSessionStore
    from gogo_agent.chat.models import ChatConversation, ChatMessage
    from agentscope.state import AgentState

    chat_repo = SQLChatHistoryRepository(session_factory=db_session_factory)
    session_store = SQLAgentSessionStore(session_factory=db_session_factory)

    test_session_id = "mariadb_e2e_session_005"
    test_user_id = "u001"

    # 1. 保存会话
    conv = ChatConversation(
        conversation_id=test_session_id,
        user_id=test_user_id,
        title="MariaDB持久化验证会话",
    )
    chat_repo.save_conversation(conv)

    # 2. 查询会话
    queried_conv = chat_repo.find_conversation_by_id(test_session_id)
    assert queried_conv is not None
    assert queried_conv.title == "MariaDB持久化验证会话"
    assert queried_conv.user_id == test_user_id

    # 3. 保存并查询消息
    msg1 = ChatMessage(
        message_id="msg_mariadb_001",
        conversation_id=test_session_id,
        role="user",
        content="在 MariaDB 中持久化用户消息",
    )
    msg2 = ChatMessage(
        message_id="msg_mariadb_002",
        conversation_id=test_session_id,
        role="agent",
        content="在 MariaDB 中持久化助手回复",
        agent_name="GoGo",
    )
    chat_repo.save_message(msg1)
    chat_repo.save_message(msg2)

    msgs = chat_repo.find_messages_by_conversation_id(test_session_id)
    assert len(msgs) >= 2
    assert msgs[-2].content == "在 MariaDB 中持久化用户消息"
    assert msgs[-1].content == "在 MariaDB 中持久化助手回复"

    # 4. 反馈更新
    chat_repo.update_feedback("msg_mariadb_002", "LIKE", datetime.now(timezone.utc))
    updated_msg = chat_repo.find_message_by_id("msg_mariadb_002")
    assert updated_msg is not None
    assert updated_msg.feedback == "LIKE"

    # 5. L2 AgentState 存取验证
    state = AgentState(session_id=test_session_id)
    state.append_context("user", [{"type": "text", "text": "用户上下文测试"}])
    session_store.save_agent_state(test_session_id, state, agent_name="GoGo")

    loaded_state = session_store.load_agent_state(test_session_id, agent_name="GoGo")
    assert loaded_state is not None
    assert loaded_state.session_id == test_session_id
    assert len(loaded_state.context) == 1
    assert loaded_state.context[0].content[0].text == "用户上下文测试"

    # 6. 逻辑删除验证
    chat_repo.delete_conversation(test_session_id)
    assert chat_repo.find_conversation_by_id(test_session_id) is None

