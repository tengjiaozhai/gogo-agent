"""AgentState 容量、既有表迁移和真实 SQL/SSE 保存回归。

MySQL 用例只读取显式 GOGO_TEST_MYSQL_URL，要求空的可丢弃测试库；
不会读取 .env 的业务库 URL，也不调用真实模型或供应商。
"""

import os

import pytest
from agentscope.message import AssistantMsg, UserMsg
from agentscope.state import AgentState
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from gogo_agent.chat.dependencies import get_chat_history_service
from gogo_agent.chat.repository import SQLAgentSessionStore, SQLChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.db.models import AgentScopeSessionModel, ChatConversationModel, ChatMessageModel
from scripts.demo_execution_trace import QUESTION, create_demo_app
from scripts.migrate_agent_state_mediumtext import inspect_state_data, migrate_state_data


@pytest.mark.parametrize("dialect, expected", [
    (mysql.dialect(), "MEDIUMTEXT"), (MariaDBDialect(), "MEDIUMTEXT"), (sqlite.dialect(), "TEXT"),
])
def test_state_column_ddl(dialect, expected):
    ddl = str(CreateTable(AgentScopeSessionModel.__table__).compile(dialect=dialect))
    assert f"state_data {expected} NOT NULL" in ddl


def test_migration_rejects_sqlite():
    engine = create_engine("sqlite://")
    try:
        with pytest.raises(ValueError, match="只接受 MySQL/MariaDB"):
            migrate_state_data(engine, apply=True)
    finally:
        engine.dispose()


@pytest.fixture
def mysql_engine():
    url = os.environ.get("GOGO_TEST_MYSQL_URL")
    if not url:
        pytest.skip("需显式设置 GOGO_TEST_MYSQL_URL 指向空的可丢弃 MySQL/MariaDB 库")
    engine = create_engine(url, hide_parameters=True, pool_pre_ping=True)
    tables = (ChatConversationModel.__table__, ChatMessageModel.__table__, AgentScopeSessionModel.__table__)
    created = []
    try:
        assert engine.dialect.name in ("mysql", "mariadb")
        assert not inspect(engine).get_table_names(), "容量回归要求空的可丢弃测试库"
        for table in tables:
            table.create(engine)
            created.append(table)
        yield engine
    finally:
        # 只清理本用例成功创建的表，避免预检失败时删除已有表。
        for table in reversed(created):
            table.drop(engine)
        engine.dispose()


@pytest.fixture
def mysql_factory(mysql_engine):
    return sessionmaker(bind=mysql_engine, expire_on_commit=False, autoflush=False)


@pytest.mark.parametrize("content", ["x" * 70000, "中文✅" * 10000], ids=["ascii-70kb", "unicode-100kb"])
def test_long_state_round_trip(mysql_factory, content):
    store = SQLAgentSessionStore(mysql_factory)
    state = AgentState(context=[UserMsg(name="user", content=content)])
    assert len(state.model_dump_json().encode()) > 65535
    store.save_agent_state("long-state", state)
    assert store.load_agent_state("long-state").model_dump_json() == state.model_dump_json()
    # 下一轮继续累加，再次覆盖同一行；重新装配仓储验证实际读回。
    state.context.append(AssistantMsg(name="GoGo", content=content))
    store.save_agent_state("long-state", state)
    assert SQLAgentSessionStore(mysql_factory).load_agent_state("long-state").model_dump_json() == state.model_dump_json()


@pytest.mark.parametrize("default_clause", ["", " DEFAULT ('{}')"], ids=["no-default", "json-default"])
def test_existing_text_migration_preserves_state_and_is_idempotent(mysql_factory, default_clause):
    engine = mysql_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE agentscope_session MODIFY state_data TEXT NOT NULL"
                                + default_clause + " COMMENT 'AgentState 序列化 JSON 文本'"))
    store = SQLAgentSessionStore(mysql_factory)
    old = AgentState(context=[UserMsg(name="user", content="原始中文状态✅")])
    store.save_agent_state("existing-state", old)
    dry_run = migrate_state_data(engine)
    assert dry_run["changed"] is False
    assert inspect_state_data(engine)["data_type"] == "text"
    large = AgentState(context=[UserMsg(name="user", content="x" * 70000)])
    with pytest.raises(DataError):
        store.save_agent_state("existing-state", large)
    assert store.load_agent_state("existing-state").model_dump_json() == old.model_dump_json()
    result = migrate_state_data(engine, apply=True)
    assert result["changed"] is True
    assert store.load_agent_state("existing-state").model_dump_json() == old.model_dump_json()
    store.save_agent_state("existing-state", large)
    assert store.load_agent_state("existing-state").model_dump_json() == large.model_dump_json()
    assert migrate_state_data(engine, apply=True)["changed"] is False


def test_migration_refuses_to_shrink_larger_column(mysql_factory):
    engine = mysql_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE agentscope_session MODIFY state_data LONGTEXT NOT NULL"))
    with pytest.raises(ValueError, match="不会自动缩容"):
        migrate_state_data(engine, apply=True)
    assert inspect_state_data(engine)["data_type"] == "longtext"


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_long_chat_reply_commits_with_state(mysql_factory, accept):
    application, auth, _, executor = create_demo_app()
    history = ChatHistoryService(SQLChatHistoryRepository(mysql_factory))
    executor._history_service = history
    executor._session_store = SQLAgentSessionStore(mysql_factory)
    application.dependency_overrides[get_chat_history_service] = lambda: history
    session_id = "long-http"
    state_key = executor._state_session_id(session_id, "u001")
    content = "历史上下文✅" * 5000
    state = AgentState(context=[UserMsg(name="user", content=content), AssistantMsg(name="GoGo", content="已记录")])
    assert len(state.model_dump_json().encode()) > 65535
    executor.session_store.save_agent_state(state_key, state)
    with TestClient(application) as client:
        headers = {"Authorization": auth.login("alice", "123456"), "Accept": accept}
        response = client.post(f"/api/chat/{session_id}", headers=headers, json={"message": QUESTION})
        assert response.status_code == 200
        if accept == "text/event-stream":
            assert "event: error" not in response.text
            confirmation = next(block for block in response.text.split("\n\n")
                                if block.startswith("event: message_id\n"))
            message_id = confirmation.split("\ndata: ", 1)[1].strip()
        else:
            message_id = response.json()["messageId"]
        # MySQL DATETIME 的秒精度不能证明同秒两条消息的先后；按保存确认 ID 核验。
        saved = history.repository.find_message_by_id(message_id)
        assert saved is not None and saved.role == "agent"
        assert saved.conversation_id == session_id and "GoGo 汇总" in saved.content
    loaded = SQLAgentSessionStore(mysql_factory).load_agent_state(state_key)
    assert any(message.get_text_content() == content for message in loaded.context)
    assert len(loaded.model_dump_json().encode()) > 65535
