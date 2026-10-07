"""028：真实服务与 SQL 仓储中的会话归属边界。"""

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gogo_agent.chat.repository import InMemoryChatHistoryRepository, SQLChatHistoryRepository
from gogo_agent.chat.executor import _FallbackMockModel
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.db.base import Base
from gogo_agent.api import app
from tests.test_023_master_agent import FixedInfoModel, master_http_case


@pytest.mark.parametrize("backend", ("memory", "sql"))
def test_deleted_session_id_stays_owned_and_old_messages_do_not_reappear(backend):
    engine = create_engine("sqlite:///:memory:") if backend == "sql" else None
    if engine is not None:
        Base.metadata.create_all(engine)
    repository = SQLChatHistoryRepository(sessionmaker(bind=engine)) if engine is not None else InMemoryChatHistoryRepository()
    history = ChatHistoryService(repository)
    try:
        history.save_user_message("reused-028", "u001", "Alice 的私有问题")
        history.delete_conversation("reused-028", "u001")

        with pytest.raises(HTTPException) as error:
            history.save_user_message("reused-028", "u002", "Bob 的越权问题")
        assert error.value.status_code == 403
        history.save_user_message("reused-028", "u001", "Alice 的新问题")
        messages = history.list_messages("reused-028", "u001")
        assert [message.content for message in messages] == ["Alice 的新问题"]
        with pytest.raises(HTTPException) as error:
            history.list_messages("reused-028", "u002")
        assert error.value.status_code == 403
    finally:
        if engine is not None:
            engine.dispose()


def test_model_supplied_user_id_and_session_id_cannot_select_another_users_info(master_http_case, monkeypatch):
    auth, history, executor = master_http_case
    captured_requests = []
    schemas = []
    original_build_info = executor._build_info_agent

    def build_info(request, state=None):
        captured_requests.append(request)
        return original_build_info(request, state)

    monkeypatch.setattr(executor, "_build_info_agent", build_info)

    class ForgedMasterModel(_FallbackMockModel):
        """调用真实 Toolkit，但在工具参数中伪造他人身份和计划引用。"""

        def __init__(self):
            super().__init__()
            self.calls = 0

        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
            self.calls += 1
            schemas.extend(tools or [])
            if self.calls == 1:
                return ChatResponse(content=[ToolCallBlock(
                    id="forged-028", name="info_agent",
                    input='{"question":"请介绍一处景点","userId":"u002","sessionId":"bob-028","planReference":"foreign-plan"}',
                )], is_last=True)
            return ChatResponse(content=[TextBlock(text="已处理本轮请求")], is_last=True)

    forged = ForgedMasterModel()
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": (
        forged if role == "master" else FixedInfoModel()
    ))
    client = TestClient(app)
    alice = {"Authorization": auth.login("alice", "123456"), "Accept": "application/json"}
    bob = {"Authorization": auth.login("bob", "123456"), "Accept": "application/json"}
    response = client.post("/api/chat/alice-028", headers=alice, json={"message": "请介绍一处景点"})
    assert response.status_code == 200
    assert schemas
    properties = schemas[0]["function"]["parameters"]["properties"]
    assert set(properties) == {"question"}
    assert captured_requests == []
    assert history.list_messages("alice-028", "u001")[0].content == "请介绍一处景点"
    assert client.get("/api/chat/alice-028/messages", headers=bob).status_code == 403
    assert executor.session_store.load_active_agent(executor._state_session_id("alice-028", "u001")) is None
    assert executor.session_store.load_active_agent(executor._state_session_id("alice-028", "u002")) is None
