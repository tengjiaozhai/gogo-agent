"""同一回合内多次委派同一子 Agent 时，后一次应接续前一次的状态，回合结束保存的应是完整链条。"""

import json

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from fastapi.testclient import TestClient

from gogo_agent.api import app
from gogo_agent.chat.executor import _FallbackMockModel
from tests.test_023_master_agent import CHILD_ANSWER, master_http_case  # noqa: F401  测试夹具

FIRST_QUESTION = "FIRST_QUESTION_SIGHTSEEING"
SECOND_QUESTION = "SECOND_QUESTION_FOOD"


class TwoInfoCallsMaster(_FallbackMockModel):
    """同一回合先后调用两次 info_agent，然后给出最终回答。"""

    def __init__(self):
        super().__init__(stream=False)
        self.calls = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content=[ToolCallBlock(id="call-1", name="info_agent", input=json.dumps({"question": FIRST_QUESTION}, ensure_ascii=False))], is_last=True)
        if self.calls == 2:
            return ChatResponse(content=[ToolCallBlock(id="call-2", name="info_agent", input=json.dumps({"question": SECOND_QUESTION}, ensure_ascii=False))], is_last=True)
        return ChatResponse(content=[TextBlock(text="主 Agent 完成")], is_last=True)


class RecordingInfoModel(_FallbackMockModel):
    """记录每次子 Agent 调用收到的完整消息。"""

    def __init__(self):
        super().__init__(stream=False)
        self.inputs: list[str] = []

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert not tools
        self.inputs.append(str(messages))
        return ChatResponse(content=[TextBlock(text=CHILD_ANSWER)], is_last=True)


def run_turn(master_http_case, monkeypatch, session_id):
    auth, _, executor = master_http_case
    master = TwoInfoCallsMaster()
    child = RecordingInfoModel()

    def build_model(stream=False, *, role="master"):
        return master if role == "master" else child

    monkeypatch.setattr(executor, "_build_model", build_model)
    response = TestClient(app).post(
        f"/api/chat/{session_id}",
        headers={"Authorization": auth.login("alice", "123456"), "Accept": "application/json"},
        json={"message": json.dumps({"userId": "u002", "question": "请介绍一处景点"})},
    )
    assert response.status_code == 200, response.text
    assert master.calls == 3
    assert len(child.inputs) == 2
    return executor, child


def test_second_info_call_in_same_turn_continues_first_call_context(master_http_case, monkeypatch):
    _, child = run_turn(master_http_case, monkeypatch, "session-chain-continue")
    # 对照：第一次调用的输入必须包含自己的问题，否则读取方式有误。
    assert FIRST_QUESTION in child.inputs[0]
    assert FIRST_QUESTION in child.inputs[1], "第二次子 Agent 调用未接续第一次的上下文"


def test_sse_turn_also_chains_child_state_across_calls(master_http_case, monkeypatch):
    auth, _, executor = master_http_case
    master = TwoInfoCallsMaster()
    child = RecordingInfoModel()

    def build_model(stream=False, *, role="master"):
        return master if role == "master" else child

    monkeypatch.setattr(executor, "_build_model", build_model)
    session_id = "session-chain-sse"
    response = TestClient(app).post(
        f"/api/chat/{session_id}",
        headers={"Authorization": auth.login("alice", "123456"), "Accept": "text/event-stream"},
        json={"message": json.dumps({"userId": "u002", "question": "请介绍一处景点"})},
    )
    assert response.status_code == 200, response.text
    assert FIRST_QUESTION in child.inputs[1], "SSE 路径第二次子 Agent 调用未接续第一次的上下文"
    saved = executor._session_store.load_agent_state(
        executor._state_session_id(session_id, "u001"), agent_name="InfoAgent",
    )
    assert saved is not None and FIRST_QUESTION in str(saved.context), "SSE 路径回合结束丢失了第一次调用"


def test_turn_end_saves_state_containing_every_info_call(master_http_case, monkeypatch):
    session_id = "session-chain-save"
    executor, _ = run_turn(master_http_case, monkeypatch, session_id)
    saved = executor._session_store.load_agent_state(
        executor._state_session_id(session_id, "u001"), agent_name="InfoAgent",
    )
    assert saved is not None
    saved_text = str(saved.context)
    assert FIRST_QUESTION in saved_text, "回合结束保存的 InfoAgent 状态丢失了第一次调用"
    assert SECOND_QUESTION in saved_text
