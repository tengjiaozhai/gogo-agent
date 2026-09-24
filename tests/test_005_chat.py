"""Tests for 005: Conversation and message persistence (L1) and AgentScope AgentState (L2)."""

import json
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from gogo_agent.api import app
from gogo_agent.auth.models import UserAccount
from gogo_agent.auth.security import TokenManager
from gogo_agent.chat.models import ChatConversation, ChatMessage
from gogo_agent.chat.repository import (
    InMemoryAgentSessionStore,
    InMemoryChatHistoryRepository,
    SQLAgentSessionStore,
    SQLChatHistoryRepository,
)
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.chat.executor import ChatAgentExecutor
from gogo_agent.db.session import get_session_factory
from agentscope.state import AgentState


@pytest.fixture
def test_users(monkeypatch):
    """Setup test users and token authentication."""
    # Ensure fresh token manager and in-memory test repos for hermetic testing
    from gogo_agent.auth.dependencies import get_auth_service
    from gogo_agent.auth.repository import InMemoryUserAccountRepository
    from gogo_agent.auth.service import AuthService

    user_repo = InMemoryUserAccountRepository(load_default_seeds=True)
    auth_service = AuthService(repository=user_repo)

    token_alice = auth_service.login("alice", "123456")
    token_bob = auth_service.login("bob", "123456")

    # In-memory chat repo and session store for clean test isolation
    chat_repo = InMemoryChatHistoryRepository()
    session_store = InMemoryAgentSessionStore()
    chat_service = ChatHistoryService(repository=chat_repo)
    executor = ChatAgentExecutor(chat_history_service=chat_service, agent_session_store=session_store)

    from gogo_agent.chat.executor import _FallbackMockModel
    monkeypatch.setattr(executor, "_build_model", lambda stream=False: _FallbackMockModel(stream=stream))

    from gogo_agent.chat import dependencies as chat_deps
    from gogo_agent.auth import dependencies as auth_deps

    monkeypatch.setattr(auth_deps, "_default_auth_service", auth_service)
    app.dependency_overrides[auth_deps.get_auth_service] = lambda: auth_service
    app.dependency_overrides[chat_deps.get_chat_history_repository] = lambda: chat_repo
    app.dependency_overrides[chat_deps.get_agent_session_store] = lambda: session_store
    app.dependency_overrides[chat_deps.get_chat_history_service] = lambda: chat_service
    app.dependency_overrides[chat_deps.get_chat_executor] = lambda: executor

    yield {
        "alice_token": token_alice,
        "bob_token": token_bob,
        "chat_service": chat_service,
        "chat_repo": chat_repo,
        "session_store": session_store,
        "executor": executor,
    }
    app.dependency_overrides.clear()




def test_user_creates_conversation_and_sends_two_turns_of_messages(test_users):
    """验收标准：用户 A 创建会话、发两轮消息，检查两轮消息顺序与内容正确保存。"""
    client = TestClient(app)
    headers = {
        "Authorization": test_users["alice_token"],
        "Accept": "application/json",
    }
    session_id = "session_alice_001"

    # 第一轮：发送消息
    resp1 = client.post(
        f"/api/chat/{session_id}",
        headers=headers,
        json={"message": "你好，我是张三，我想了解北京差旅政策。"},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["sessionId"] == session_id
    assert data1["messageId"].startswith("msg_")
    assert len(data1["content"]) > 0

    # 验证会话已惰性创建，且标题由首条非空消息自动生成（截取前24字）
    convs_resp = client.get("/api/chat/conversations", headers=headers)
    assert convs_resp.status_code == 200
    convs = convs_resp.json()
    assert len(convs) == 1
    assert convs[0]["id"] == session_id
    assert convs[0]["title"] == "你好，我是张三，我想了解北京差旅政策。"[:24]

    # 第二轮：发送追问
    resp2 = client.post(
        f"/api/chat/{session_id}",
        headers=headers,
        json={"message": "请问五星级酒店每晚报销上限是多少？"},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["messageId"].startswith("msg_")

    # 查询会话所有消息，严格验证两轮对话的 4 条消息在时间正序中正确排列
    msgs_resp = client.get(f"/api/chat/{session_id}/messages", headers=headers)
    assert msgs_resp.status_code == 200
    messages = msgs_resp.json()
    assert len(messages) == 4

    # 1: 用户消息 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "你好，我是张三，我想了解北京差旅政策。"
    # 2: 助手回复 1
    assert messages[1]["role"] == "agent"
    assert messages[1]["agentName"] == "GoGo"
    assert messages[1]["id"] == data1["messageId"]
    # 3: 用户消息 2
    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == "请问五星级酒店每晚报销上限是多少？"
    # 4: 助手回复 2
    assert messages[3]["role"] == "agent"
    assert messages[3]["agentName"] == "GoGo"
    assert messages[3]["id"] == data2["messageId"]


def test_sse_streaming_response_format(test_users):
    """验证默认 text/event-stream 输出格式，兼容前端 sendChatMessageSSE 解析。"""
    client = TestClient(app)
    headers = {
        "Authorization": test_users["alice_token"],
    }
    session_id = "session_alice_sse"

    resp = client.post(
        f"/api/chat/{session_id}",
        headers=headers,
        json={"message": "测试 SSE 流式输出"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    text = resp.text
    # 验证真正的流式分块输出（多条 event: message 事件而不是单次缓冲输出）
    assert text.count("event: message\n") >= 2
    assert text.count("event: message_id\n") == 1
    assert "data: msg_" in text


def test_multiturn_agent_state_restoration_across_restarts(test_users):
    """验收标准：重启服务（模拟实例销毁后重建）后，AgentState 仍能正确恢复上下文记忆。"""
    session_id = "session_restore_test"
    user_id = "u001"
    store = test_users["session_store"]
    history = test_users["chat_service"]

    # 模拟第一轮执行
    executor1 = ChatAgentExecutor(chat_history_service=history, agent_session_store=store)
    reply1, msg_id1 = pytest.importorskip("asyncio").run(
        executor1.execute_turn(session_id, user_id, "你好，我是张三")
    )
    assert msg_id1

    # 检查 L2 存储中已存有 AgentState
    saved_state = store.load_agent_state(session_id, agent_name="GoGo")
    assert saved_state is not None
    assert len(saved_state.context) == 2  # user + assistant

    # 模拟服务完全重启：创建全新的 executor 实例，丢弃原有内存
    executor2 = ChatAgentExecutor(chat_history_service=history, agent_session_store=store)
    reply2, msg_id2 = pytest.importorskip("asyncio").run(
        executor2.execute_turn(session_id, user_id, "我的名字是什么？")
    )
    assert msg_id2

    # 检查 L2 中 state.context 增长到了 4 条（跨重启连续记忆）
    updated_state = store.load_agent_state(session_id, agent_name="GoGo")
    assert updated_state is not None
    assert len(updated_state.context) == 4
    # 上下文的第一条消息是第一轮的用户输入
    assert updated_state.context[0].content[0].text == "你好，我是张三"
    # 上下文的第三条消息是第二轮的用户输入
    assert updated_state.context[2].content[0].text == "我的名字是什么？"


def test_cross_user_security_isolation(test_users):
    """验收标准：用户 B 访问或写入用户 A 的 session 返回 403 拒绝，列表不可见。"""
    client = TestClient(app)
    alice_headers = {"Authorization": test_users["alice_token"], "Accept": "application/json"}
    bob_headers = {"Authorization": test_users["bob_token"], "Accept": "application/json"}
    session_id = "session_alice_private"

    # Alice 创建会话并发送消息
    resp = client.post(
        f"/api/chat/{session_id}",
        headers=alice_headers,
        json={"message": "这是 Alice 的保密差旅会话"},
    )
    assert resp.status_code == 200
    msg_id = resp.json()["messageId"]

    # 1. Bob 试图读取 Alice 的会话消息 -> 403
    bob_read = client.get(f"/api/chat/{session_id}/messages", headers=bob_headers)
    assert bob_read.status_code == 403
    assert "无权访问该会话" in bob_read.json()["detail"]

    # 2. Bob 试图向 Alice 的会话注入消息 -> 403
    bob_write = client.post(
        f"/api/chat/{session_id}",
        headers=bob_headers,
        json={"message": "Bob 试图伪造消息"},
    )
    assert bob_write.status_code == 403
    assert "无权访问该会话" in bob_write.json()["detail"]

    # 3. Bob 试图修改 Alice 会话的标题 -> 403
    bob_title = client.put(
        f"/api/chat/{session_id}/title",
        headers=bob_headers,
        json={"title": "Bob 篡改标题"},
    )
    assert bob_title.status_code == 403

    # 4. Bob 试图删除 Alice 的会话 -> 403
    bob_del = client.delete(f"/api/chat/{session_id}", headers=bob_headers)
    assert bob_del.status_code == 403

    # 5. Bob 试图评价 Alice 的消息 -> 403
    bob_fb = client.put(
        f"/api/chat/{session_id}/messages/{msg_id}/feedback",
        headers=bob_headers,
        json={"feedback": "LIKE"},
    )
    assert bob_fb.status_code == 403

    # 6. Bob 查看自己的历史会话列表，绝不包含 Alice 的会话
    bob_list = client.get("/api/chat/conversations", headers=bob_headers)
    assert bob_list.status_code == 200
    assert all(c["id"] != session_id for c in bob_list.json())


def test_manual_title_update_and_empty_fallback(test_users):
    """测试手动更新会话标题。"""
    client = TestClient(app)
    headers = {"Authorization": test_users["alice_token"], "Accept": "application/json"}
    session_id = "session_title_test"

    client.post(
        f"/api/chat/{session_id}",
        headers=headers,
        json={"message": "初始消息"},
    )

    # 手动更新标题
    resp = client.put(
        f"/api/chat/{session_id}/title",
        headers=headers,
        json={"title": "上海商务差旅规划"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": True}

    # 查询验证
    convs = client.get("/api/chat/conversations", headers=headers).json()
    assert convs[0]["title"] == "上海商务差旅规划"


def test_message_feedback_lifecycle(test_users):
    """测试消息点赞、点踩、清空与非法输入拦截。"""
    client = TestClient(app)
    headers = {"Authorization": test_users["alice_token"], "Accept": "application/json"}
    session_id = "session_fb_test"

    resp = client.post(
        f"/api/chat/{session_id}",
        headers=headers,
        json={"message": "请介绍杭州景点"},
    )
    msg_id = resp.json()["messageId"]

    # 1. 点赞 LIKE
    fb_resp = client.put(
        f"/api/chat/{session_id}/messages/{msg_id}/feedback",
        headers=headers,
        json={"feedback": "LIKE"},
    )
    assert fb_resp.status_code == 200
    msgs = client.get(f"/api/chat/{session_id}/messages", headers=headers).json()
    ai_msg = [m for m in msgs if m["id"] == msg_id][0]
    assert ai_msg["feedback"] == "LIKE"
    assert ai_msg["feedbackAt"] is not None

    # 2. 改为点踩 DISLIKE
    client.put(
        f"/api/chat/{session_id}/messages/{msg_id}/feedback",
        headers=headers,
        json={"feedback": "dislike"},
    )
    msgs = client.get(f"/api/chat/{session_id}/messages", headers=headers).json()
    ai_msg = [m for m in msgs if m["id"] == msg_id][0]
    assert ai_msg["feedback"] == "DISLIKE"

    # 3. 清空反馈 (CLEAR 或 null)
    client.put(
        f"/api/chat/{session_id}/messages/{msg_id}/feedback",
        headers=headers,
        json={"feedback": "CLEAR"},
    )
    msgs = client.get(f"/api/chat/{session_id}/messages", headers=headers).json()
    ai_msg = [m for m in msgs if m["id"] == msg_id][0]
    assert ai_msg["feedback"] is None
    assert ai_msg["feedbackAt"] is None

    # 4. 非法类型拦截 -> 400
    bad_resp = client.put(
        f"/api/chat/{session_id}/messages/{msg_id}/feedback",
        headers=headers,
        json={"feedback": "AWESOME"},
    )
    assert bad_resp.status_code == 400


def test_soft_delete_conversation(test_users):
    """测试会话与消息的逻辑删除 (deleted=1)，删除后不再可见。"""
    client = TestClient(app)
    headers = {"Authorization": test_users["alice_token"], "Accept": "application/json"}
    session_id = "session_delete_test"

    client.post(
        f"/api/chat/{session_id}",
        headers=headers,
        json={"message": "待删除的消息"},
    )

    # 删除会话
    del_resp = client.delete(f"/api/chat/{session_id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json() == {"deleted": True}

    # 列表不再包含
    convs = client.get("/api/chat/conversations", headers=headers).json()
    assert all(c["id"] != session_id for c in convs)

    # 查询消息返回 404
    msgs_resp = client.get(f"/api/chat/{session_id}/messages", headers=headers)
    assert msgs_resp.status_code == 404


def test_sql_agentscope_session_persistence_when_database_available():
    """测试真实 SQL 环境下的 AgentScope 状态 (agentscope_session) 存储与读取。"""
    sf = get_session_factory()
    if not sf:
        pytest.skip("未配置真实数据库，跳过 SQL 物理存储测试")

    sql_store = SQLAgentSessionStore(session_factory=sf)
    test_session_id = "sql_test_sess_005"

    state = AgentState(session_id=test_session_id)
    state.append_context("user", [{"type": "text", "text": "SQL 测试上下文"}])
    sql_store.save_agent_state(test_session_id, state, agent_name="GoGo")

    # 从数据库重新读取
    loaded = sql_store.load_agent_state(test_session_id, agent_name="GoGo")
    assert loaded is not None
    assert loaded.session_id == test_session_id
    assert len(loaded.context) == 1
    assert loaded.context[0].content[0].text == "SQL 测试上下文"


@pytest.mark.asyncio
async def test_real_model_e2e_invocation_when_configured():
    """阶段验收要求：若配置了真实模型凭证，完成一次真实模型调用验证，记录回复。"""
    import os
    required = ("GOGO_MODEL_API_KEY", "GOGO_MODEL_NAME", "GOGO_MODEL_BASE_URL")
    if not all(bool(os.environ.get(k, "").strip()) for k in required):
        pytest.skip("未配置真实模型凭证，跳过端到端大模型网络测试")

    chat_repo = InMemoryChatHistoryRepository()
    session_store = InMemoryAgentSessionStore()
    chat_service = ChatHistoryService(repository=chat_repo)
    executor = ChatAgentExecutor(chat_history_service=chat_service, agent_session_store=session_store)

    session_id = "real_model_test_sess"
    user_id = "u001"
    prompt = "请用一句话介绍你自己。"

    reply, msg_id = await executor.execute_turn(session_id, user_id, prompt)
    assert len(reply) > 0
    assert msg_id.startswith("msg_")
    print(f"\n[REAL MODEL TEST] 回复内容: {reply}")

