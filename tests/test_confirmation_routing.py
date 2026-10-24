"""确认提交的动作保真、真实意图流水线和管理工具可见性回归。"""

from datetime import date

import pytest

from gogo_agent.chat.repository import InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import IntentCategory, IntentRuleMatcher, MatchStatus, QueryInput
from gogo_agent.request_context import RequestContext
from tests.test_016_017_pipeline import ProbeRewriter, ProbeVector, pipeline_for
from tests.test_010_011_intent import FixedModel


CAPTURED_REWRITE = (
    "确认提交综合最佳方案（北京⇄上海，2026-11-10 出发，2026-11-12 返回，"
    "去程 F12 + 酒店 H2 两晚 + 返程 F2）"
)
APPLICATION = "确认提交北京到上海的差旅申请，2026-11-10出发，2026-11-12返回，事由为客户拜访，酒店H2两晚"
SELECTION = "选择北京到上海的综合最佳行程方案，2026-11-10出发，2026-11-12返回，酒店H2两晚"


@pytest.mark.parametrize("question", [CAPTURED_REWRITE, "确认提交方案（酒店H2）", "选择综合最佳方案（酒店H2）"])
def test_unresolved_action_cannot_become_a_hotel_search(question):
    match = IntentRuleMatcher().match(QueryInput(question=question))
    assert match.status is MatchStatus.AMBIGUOUS
    assert match.result is None


@pytest.mark.parametrize("question,expected", [
    (APPLICATION, IntentCategory.TRAVEL_APPLICATION),
    ("确认提交差旅申请（酒店H2两晚）", IntentCategory.TRAVEL_APPLICATION),
    (SELECTION, IntentCategory.ITINERARY_PLANNING),
    ("确认预订酒店H2", IntentCategory.BOOKING),
    ("确认取消差旅申请（酒店H2）", IntentCategory.TRAVEL_CANCEL),
    ("不要提交申请，只查上海酒店", IntentCategory.HOTEL_SEARCH),
])
def test_explicit_action_wins_over_background_hotel_details(question, expected):
    match = IntentRuleMatcher().match(QueryInput(question=question))
    assert match.status is MatchStatus.HIT
    assert match.result.primary_intent is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("question,rewritten,expected", [
    ("确认提交", APPLICATION, IntentCategory.TRAVEL_APPLICATION),
    ("选择综合最佳方案", SELECTION, IntentCategory.ITINERARY_PLANNING),
    ("确认", APPLICATION, IntentCategory.TRAVEL_APPLICATION),
])
async def test_short_confirmation_rewrites_before_false_hotel_vector_hit(question, rewritten, expected):
    history = ChatHistoryService(InMemoryChatHistoryRepository())
    history.save_user_message("confirm-route", "u1", "规划北京到上海的行程，并准备提交差旅申请，事由是客户拜访")
    history.save_assistant_message("confirm-route", "u1", "已给出综合最佳行程方案，含酒店H2；差旅申请尚未提交。")
    message_id = history.save_user_message("confirm-route", "u1", question)
    vector = ProbeVector(hit=IntentCategory.HOTEL_SEARCH)
    rewriter = ProbeRewriter(rewritten)
    prepared = await pipeline_for(history, rewriter, vector).prepare(
        RequestContext(user_id="u1", session_id="confirm-route", request_id=message_id),
        reference_date=date(2026, 10, 23),
    )
    assert prepared.branch == "rewritten"
    assert prepared.decision.result.primary_intent is expected
    assert prepared.fast_decision.attempted_layers == []
    assert vector.calls == []
    assert len(rewriter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["确认提交", "我确认提交", "确认", "选择综合最佳方案", "选择综合最佳行程方案", "我选择综合最佳行程方案"])
async def test_confirmation_without_history_requests_context_without_a_model(question):
    history = ChatHistoryService(InMemoryChatHistoryRepository())
    message_id = history.save_user_message("missing-confirm", "u1", question)
    vector = ProbeVector(hit=IntentCategory.HOTEL_SEARCH)
    rewriter = ProbeRewriter("不应调用")
    prepared = await pipeline_for(history, rewriter, vector).prepare(
        RequestContext(user_id="u1", session_id="missing-confirm", request_id=message_id),
    )
    assert prepared.branch == "needs_context"
    assert prepared.decision is None
    assert vector.calls == rewriter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rewritten", [CAPTURED_REWRITE, "选择综合最佳方案（酒店H2）"])
async def test_rewriter_rejects_a_confirmation_object_that_is_still_unresolved(rewritten):
    from gogo_agent.intent import HistoryMessage, ModelOutputError, QueryRewriter, RewriteContext
    original = "选择综合最佳方案" if rewritten.startswith("选择") else "确认提交"
    context = RewriteContext(query=QueryInput(question=original), reference_date=date(2026, 10, 23),
                             history=[HistoryMessage(role="agent", content="已讨论差旅申请，尚未提交")])
    model = FixedModel(dict(related=True, rewritten_question=rewritten, reason="固定改写", evidence=[original], missing_context=[]))
    with pytest.raises(ModelOutputError, match="对象"):
        await QueryRewriter(model).rewrite(context)
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("rewritten,accepted", [
    ("选择综合最佳行程方案，并提交差旅申请", False),
    ("选择北京到上海的综合最佳行程方案，不要提交申请", True),
    ("选择北京到上海的综合最佳行程方案，不必提交申请", True),
    ("选择北京到上海的综合最佳行程方案，请勿提交申请", True),
])
async def test_selecting_a_plan_cannot_invent_an_affirmative_write(rewritten, accepted):
    from gogo_agent.intent import HistoryMessage, ModelOutputError, QueryRewriter, RewriteContext
    context = RewriteContext(query=QueryInput(question="选择综合最佳方案"), reference_date=date(2026, 10, 23),
                             history=[HistoryMessage(role="agent", content="已经给出北京到上海两个待选择的行程方案")])
    model = FixedModel(dict(related=True, rewritten_question=rewritten, reason="固定改写", evidence=["选择综合最佳方案"], missing_context=[]))
    if accepted:
        assert (await QueryRewriter(model).rewrite(context)).rewritten_question == rewritten
    else:
        with pytest.raises(ModelOutputError, match="新增"):
            await QueryRewriter(model).rewrite(context)


def test_standalone_hotel_question_is_not_a_background_parameter():
    match = IntentRuleMatcher().match(QueryInput(question="提交北京到上海的差旅申请，上海酒店价格多少"))
    assert match.status is MatchStatus.AMBIGUOUS
    assert {candidate.intent for candidate in match.candidates} == {
        IntentCategory.TRAVEL_APPLICATION, IntentCategory.HOTEL_SEARCH,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("question,tool_name", [
    ("确认提交", "itinerary_manage_agent"),
    ("选择综合最佳方案", "itinerary_plan_agent"),
])
async def test_corrected_pipeline_opens_only_the_matching_delegate(question, tool_name):
    from agentscope.state import AgentState
    from scripts.demo_confirmation_routing import create_demo_app, SESSION_ID

    _, _, _, executor, _, engine = create_demo_app()
    agent = None
    try:
        request = executor._save_user_turn(SESSION_ID, "u001", question)
        prepared = await executor._prepare_turn(request)
        agent = executor._build_agent(AgentState(session_id=SESSION_ID), prepared, request, [])
        names = [schema["function"]["name"] for schema in await agent.toolkit.get_tool_schemas()]
        assert names == [tool_name]
        assert not (await agent.toolkit.get_tool(tool_name)).is_read_only
    finally:
        await executor._close_agent_model(agent)
        engine.dispose()


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_http_confirmation_reaches_real_manage_tool_and_submits_only_after_summary(accept):
    from fastapi.testclient import TestClient
    from scripts.demo_confirmation_routing import create_demo_app, SESSION_ID

    application, auth, history, executor, business, engine = create_demo_app()
    try:
        with TestClient(application) as client:
            headers = {"Authorization": auth.login("alice", "123456"), "Accept": accept}
            ids = []
            for expected_count in [0, 1, 1]:
                response = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": "确认提交"})
                assert response.status_code == 200, response.text
                if accept == "text/event-stream":
                    assert "event: message_id" in response.text and "event: error" not in response.text
                message = history.list_messages(SESSION_ID, "u001")[-1]
                assert message.extra["intent_pipeline"]["branch"] == "rewritten"
                assert message.extra["intent_pipeline"]["intents"] == ["travel_application"]
                assert message.extra["intent_pipeline"]["fast_layers"] == []
                assert message.extra["tool_calls"][0]["agent_name"] == "ItineraryManageAgent"
                tools = [item for item in message.extra["timeline"] if item["kind"] == "tool"]
                assert len(tools) == 2
                assert tools[1]["title"] == "submit_travel_approval"
                assert tools[1]["status"] == ("pending" if expected_count == 0 else "done")
                orders = business.list_orders("u001")
                assert len(orders) == expected_count
                if orders:
                    assert orders[0].status.value == "SUBMITTED"
                    ids.append(orders[0].order_id)
            assert ids[0] == ids[1]
            assert all(model.tool_names[0] == ["itinerary_manage_agent"]
                       for model in executor.models if model.role == "master")
            assert all(model.client.is_closed() for model in executor.models)
            bob = {"Authorization": auth.login("bob", "123456"), "Accept": accept}
            assert client.post(f"/api/chat/{SESSION_ID}", headers=bob, json={"message": "确认提交"}).status_code == 403
            assert business.list_orders("u002") == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("past_deadline", [0, 1])
async def test_expired_submission_token_does_not_write_an_order(monkeypatch, past_deadline):
    from scripts.demo_confirmation_routing import create_demo_app, DRAFT
    from gogo_agent.travel import confirmation
    from gogo_agent.travel.tools import build_manage_toolkit

    *_, business, engine = create_demo_app()
    try:
        toolkit = build_manage_toolkit(business, RequestContext(user_id="u001", session_id="expiry", request_id="r"))
        tool = await toolkit.get_tool("submit_travel_approval")
        import json
        clock = 1800000000
        monkeypatch.setattr(confirmation.time, "time", lambda: clock)
        first = json.loads((await tool.call(**DRAFT)).content[0].text)
        clock += confirmation.DEFAULT_TTL_SECONDS + past_deadline
        expired = json.loads((await tool.call(**DRAFT, confirmation_token=first["data"]["confirmation_token"])).content[0].text)
        assert expired["errorCode"] == "CONFIRMATION_EXPIRED"
        assert business.list_orders("u001") == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_a_token_issued_in_this_turn_cannot_submit_until_a_later_user_turn():
    import json
    from scripts.demo_confirmation_routing import create_demo_app, DRAFT
    from gogo_agent.travel.tools import build_manage_toolkit

    *_, business, engine = create_demo_app()
    try:
        async def submit(session_id, request_id, token=""):
            toolkit = build_manage_toolkit(business, RequestContext(user_id="u001", session_id=session_id, request_id=request_id))
            tool = await toolkit.get_tool("submit_travel_approval")
            return json.loads((await tool.call(**DRAFT, confirmation_token=token)).content[0].text)

        draft = await submit("same-session", "first-user-turn")
        token = draft["data"]["confirmation_token"]
        premature = await submit("same-session", "first-user-turn", token)
        assert premature["success"] is False
        assert premature["errorCode"] == "CONFIRMATION_REQUIRED"
        assert business.list_orders("u001") == []
        foreign = await submit("another-session", "later-user-turn", token)
        assert foreign["success"] is False
        assert business.list_orders("u001") == []
        confirmed = await submit("same-session", "second-user-turn", token)
        assert confirmed["success"] is True
        assert len(business.list_orders("u001")) == 1
    finally:
        engine.dispose()


def test_real_agent_loop_cannot_submit_twice_in_the_same_user_turn():
    import json
    from agentscope.message import ToolCallBlock, ToolResultBlock
    from agentscope.model import ChatResponse
    from fastapi.testclient import TestClient
    from scripts.demo_confirmation_routing import create_demo_app, ReplayModel, tool_text, DRAFT, SESSION_ID

    class PrematureManage(ReplayModel):
        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
            if self.call_count == 1:
                self.call_count += 1
                results = [block for message in messages for block in message.content if isinstance(block, ToolResultBlock)]
                token = json.loads(tool_text(results[-1]))["data"]["confirmation_token"]
                return ChatResponse(content=[ToolCallBlock(
                    id="same-turn-submit", name="submit_travel_approval",
                    input=json.dumps(dict(DRAFT, confirmation_token=token)),
                )], is_last=True)
            return await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)

    application, auth, history, executor, business, engine = create_demo_app()
    original_build = executor._build_model
    executor._build_model = lambda stream=False, role="master": (
        PrematureManage(role) if role == "manage" else original_build(stream, role=role)
    )
    try:
        with TestClient(application) as client:
            response = client.post(f"/api/chat/{SESSION_ID}", headers={"Authorization": auth.login("alice", "123456"),
                                   "Accept": "application/json"}, json={"message": "确认提交"})
            assert response.status_code == 200
            assert business.list_orders("u001") == []
            tools = [item for item in history.list_messages(SESSION_ID, "u001")[-1].extra["timeline"]
                     if item["kind"] == "tool" and item["title"] == "submit_travel_approval"]
            assert len(tools) == 2
            assert all(item["status"] == "pending" for item in tools)
    finally:
        engine.dispose()


def test_old_token_version_is_explicitly_rejected(monkeypatch):
    from gogo_agent.travel import confirmation
    with monkeypatch.context() as patcher:
        patcher.setattr(confirmation, "_TOKEN_VERSION", "v1")
        old = confirmation.issue("u001", "digest", session_id="s", request_id="first-turn")
    with pytest.raises(confirmation.ConfirmationError) as error:
        confirmation.verify(old, "u001", "digest", session_id="s", request_id="later-turn")
    assert error.value.code == "CONFIRMATION_INVALID"
