"""阶段 D 工具路由、政策语料、缓存、推荐及 HTTP 权限的针对性验收。"""

from datetime import date
import json
from zipfile import ZipFile

import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse
from agentscope.state import AgentState
from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.recommendation import generate_recommendations, normalize_questions
from gogo_agent.db.base import Base
from gogo_agent.db.models import UserProfileModel
from gogo_agent.request_context import RequestContext
from gogo_agent.travel.cache import UserPolicyCache
from gogo_agent.travel.dependencies import get_travel_service
import gogo_agent.travel.dependencies as travel_dependencies
from gogo_agent.travel.knowledge import PolicyKnowledgeIndex, build_policy_corpus
from gogo_agent.travel.models import SubmitTravelRequest, UserProfilePatch
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import TravelBusinessService
from gogo_agent.travel.tools import build_manage_toolkit
from tests.test_023_master_agent import CHILD_ANSWER, FixedInfoModel, ScriptedMasterModel, master_http_case
from gogo_agent.intent.models import ConfidenceLevel, IntentCategory, IntentItem, IntentResult


@pytest.fixture
def service():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    business = TravelBusinessService(BusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)))
    with business.repository.transaction() as session:
        session.add_all([UserProfileModel(user_id="u001", level="P6"), UserProfileModel(user_id="u002", level="P5")])
    yield business
    engine.dispose()


@pytest.mark.asyncio
async def test_manage_tool_identity_schema_and_proposed_write(service):
    order = service.submit_order("u001", SubmitTravelRequest(
        destination="上海", departure_city="北京", departure_date=date(2026, 11, 10),
        return_date=date(2026, 11, 12), purpose="客户拜访",
        confirmed=True, idempotency_key="tool-test",
    ))
    toolkit = build_manage_toolkit(service, RequestContext(user_id="u002", session_id="test", request_id="r"))
    schemas = await toolkit.get_tool_schemas()
    names = [item["function"]["name"] for item in schemas]
    assert "get_travel_order" in names and "submit_travel_approval" in names
    assert "query_booking_records" in names and "request_booking_cancellation" in names
    assert "book_flight" not in names and "cancel_supplier_order" not in names
    assert all("user_id" not in item["function"]["parameters"]["properties"] for item in schemas)
    tool = await toolkit.get_tool("get_travel_order")
    denied = await tool.call(order_id=order.order_id)
    assert json.loads(denied.content[0].text)["errorCode"] == "ORDER_NOT_FOUND"
    submit = await toolkit.get_tool("submit_travel_approval")
    pending = await submit.call(
        destination="杭州", departure_city="上海", departure_date="2026-11-20",
        return_date="2026-11-21", purpose="会议",
    )
    assert json.loads(pending.content[0].text)["errorCode"] == "CONFIRMATION_REQUIRED"
    assert service.list_orders("u002") == []
    list_bookings = await toolkit.get_tool("query_booking_records")
    assert json.loads((await list_bookings.call()).content[0].text)["data"]["bookings"] == []


@pytest.mark.asyncio
async def test_master_policy_registers_manage_and_child_has_no_supplier_tool(master_http_case, monkeypatch, service):
    _, _, executor = master_http_case
    monkeypatch.setattr("gogo_agent.chat.executor.get_travel_service", lambda: service)
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": _FallbackMockModel(stream=stream))
    request = RequestContext(user_id="u001", session_id="stage-d", request_id="r")
    child = executor._build_manage_agent(request)
    try:
        assert child.name == "ItineraryManageAgent"
        names = [item["function"]["name"] for item in await child.toolkit.get_tool_schemas()]
        assert "query_travel_policy" in names
        assert "query_user_contact_info" in names
        assert "get_travel_order" in names
        assert "cancel_booking" not in names
        assert child.react_config.max_iters == 10
    finally:
        await executor._close_agent_model(child)


@pytest.mark.asyncio
async def test_booking_intent_can_only_reach_manage_proposal_tool(master_http_case, monkeypatch, service):
    _, _, executor = master_http_case
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": _FallbackMockModel(stream=stream))
    request = executor._save_user_turn("booking-route-037", "u001", "查询我的预订记录")
    prepared = await executor._prepare_turn(request)
    booking = IntentResult(
        intents=[IntentItem(intent=IntentCategory.BOOKING, confidence=ConfidenceLevel.HIGH,
                            reason="固定预订意图", evidence=["查询我的预订记录"])],
        primary_intent=IntentCategory.BOOKING, multi_intent=False,
        overall_reason="固定预订意图",
    )
    decision = prepared.decision.model_copy(update={"result": booking})
    agent = executor._build_agent(
        state=AgentState(session_id=request.session_id),
        prepared=prepared.model_copy(update={"decision": decision, "fast_decision": decision}),
        request=request, tool_calls=[],
    )
    try:
        names = [item["function"]["name"] for item in await agent.toolkit.get_tool_schemas()]
        assert names == ["itinerary_manage_agent"]
        assert "新预订和供应商取消仍不可执行" in agent._system_prompt
    finally:
        await executor._close_agent_model(agent)


class FixedManageAnswerModel(_FallbackMockModel):
    """离线管理子 Agent 回复；主 Agent 的 FunctionTool 仍真实执行。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert any(item["function"]["name"] == "query_travel_policy" for item in tools or [])
        return ChatResponse(content=[TextBlock(text=CHILD_ANSWER)], is_last=True)


@pytest.mark.asyncio
async def test_master_dispatches_policy_to_manage_and_persists_child_state(master_http_case, monkeypatch, service):
    _, history, executor = master_http_case
    original_prepare = executor._prepare_turn

    async def policy_prepare(request):
        prepared = await original_prepare(request)
        result = IntentResult(
            intents=[IntentItem(intent=IntentCategory.POLICY_QUERY, confidence=ConfidenceLevel.HIGH,
                                reason="固定政策查询", evidence=["查询差旅政策"])],
            primary_intent=IntentCategory.POLICY_QUERY, multi_intent=False,
            overall_reason="固定政策查询",
        )
        decision = prepared.decision.model_copy(update={"result": result})
        return prepared.model_copy(update={"decision": decision, "fast_decision": decision})

    master = ScriptedMasterModel(stream=False, requested_tool="itinerary_manage_agent")
    manage = FixedManageAnswerModel(stream=False)
    monkeypatch.setattr(executor, "_prepare_turn", policy_prepare)
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": (
        master if role == "master" else manage
    ))
    monkeypatch.setattr("gogo_agent.chat.executor.get_travel_service", lambda: service)
    reply, _ = await executor.execute_turn("session-manage-029", "u001", "查询差旅政策")
    assert CHILD_ANSWER in reply and master.seen_tool_result
    assert master.tool_names == [["itinerary_manage_agent"], ["itinerary_manage_agent"]]
    saved = history.list_messages("session-manage-029", "u001")[-1]
    assert saved.extra["tool_calls"][0]["agent_name"] == "ItineraryManageAgent"
    key = executor._state_session_id("session-manage-029", "u001")
    assert executor.session_store.load_agent_state(key, agent_name="ItineraryManageAgent") is not None


def _write_docx(path, text):
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
        ))


def test_policy_docx_rebuild_changes_version_and_unknown_has_no_evidence(tmp_path):
    source = tmp_path / "policy.docx"
    target = tmp_path / "policy.json"
    _write_docx(source, "上海酒店住宿标准按职级执行，超额需个人承担。")
    first = build_policy_corpus(source, target)
    hits = PolicyKnowledgeIndex(target).retrieve("上海酒店标准")
    assert hits and hits[0].source == "policy.docx" and hits[0].version == first["version"]
    assert PolicyKnowledgeIndex(target).retrieve("量子火箭推进器") == []
    _write_docx(source, "北京交通补贴按每日标准执行。")
    second = build_policy_corpus(source, target)
    assert second["version"] != first["version"]
    assert PolicyKnowledgeIndex(target).retrieve("北京交通补贴")


def test_policy_cache_ttl_user_and_invalidation():
    clock = [0.0]
    cache = UserPolicyCache(ttl_seconds=10, clock=lambda: clock[0])
    loads = []

    def load(label):
        loads.append(label)
        return label

    assert cache.get_or_load("u001", "上海", "P6", "v1", "2026-10-07", lambda: load("A1")) == "A1"
    assert cache.get_or_load("u001", "上海", "P6", "v1", "2026-10-07", lambda: load("A2")) == "A1"
    assert cache.get_or_load("u002", "上海", "P6", "v1", "2026-10-07", lambda: load("B1")) == "B1"
    cache.invalidate_user("u001")
    assert cache.get_or_load("u001", "上海", "P6", "v1", "2026-10-07", lambda: load("A3")) == "A3"
    clock[0] = 11.0
    assert cache.get_or_load("u002", "上海", "P6", "v1", "2026-10-07", lambda: load("B2")) == "B2"
    assert loads == ["A1", "B1", "A3", "B2"]


class FixedRecommendationModel(_FallbackMockModel):
    def __init__(self, result):
        super().__init__(stream=False)
        self.result = result
        self.calls = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        assert not tools
        self.calls += 1
        return ChatResponse(content=[TextBlock(text=self.result)], is_last=True)


@pytest.mark.asyncio
async def test_recommendation_single_call_dedup_and_failure():
    questions = ["上海酒店标准是多少？", "上海酒店标准是多少?", "继续", "如何查看审批进度？", "x" * 61]
    assert normalize_questions(questions, "我要申请出差", "尚未提交申请") == [
        "上海酒店标准是多少？", "如何查看审批进度？",
    ]
    model = FixedRecommendationModel(json.dumps({"questions": questions}, ensure_ascii=False))
    try:
        assert await generate_recommendations(model, "我要申请出差", "尚未提交申请") == [
            "上海酒店标准是多少？", "如何查看审批进度？",
        ]
        assert model.calls == 1
    finally:
        await model.client.close()
    bad = FixedRecommendationModel("invalid")
    try:
        assert await generate_recommendations(bad, "问", "答") == []
    finally:
        await bad.client.close()


def test_http_confirmed_order_admin_only_and_callback_disabled(service):
    auth = AuthService(
        repository=InMemoryUserAccountRepository(load_default_seeds=True),
        token_manager=TokenManager(store=InMemoryTokenStore()),
    )
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_travel_service] = lambda: service
    try:
        client = TestClient(app)
        alice = {"Authorization": auth.login("alice", "123456")}
        body = {
            "destination": "上海", "departure_city": "北京", "departure_date": "2026-11-10",
            "return_date": "2026-11-12", "purpose": "客户拜访", "idempotency_key": "http-1",
        }
        assert client.post("/api/my-travel/orders", headers=alice, json={**body, "confirmed": False}).status_code == 409
        created = client.post("/api/my-travel/orders", headers=alice, json={**body, "confirmed": True})
        assert created.status_code == 200 and created.json()["status"] == "SUBMITTED"
        assert len(client.get("/api/my-travel/orders", headers=alice).json()) == 1
        approval_id = created.json()["approval_id"]
        assert client.post(
            f"/api/admin/approvals/{approval_id}/decision", headers=alice,
            json={"decision": "agree"},
        ).status_code == 403
        administrator = {"Authorization": auth.login("admin", "123456")}
        accepted = client.post(
            f"/api/admin/approvals/{approval_id}/decision", headers=administrator,
            json={"decision": "agree"},
        )
        assert accepted.status_code == 200 and accepted.json()["status"] == "APPROVED"
        assert client.post("/api/callback/dingtalk/approval", json={"decision": "agree"}).status_code == 503
        assert service.get_order("u001", created.json()["order_id"]).status == "APPROVED"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_existing_database_without_stage_d_tables_fails_business_dependency(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    try:
        monkeypatch.setattr(travel_dependencies, "_default_service", None)
        monkeypatch.setattr(travel_dependencies, "get_engine", lambda: engine)
        monkeypatch.setattr(travel_dependencies, "get_session_factory", lambda: sessionmaker(bind=engine))
        with pytest.raises(HTTPException) as error:
            travel_dependencies.get_travel_service()
        assert error.value.status_code == 503
        assert "travel_order" in str(error.value.detail)
    finally:
        engine.dispose()


def test_sse_message_id_precedes_recommendations(master_http_case, monkeypatch):
    auth, _, executor = master_http_case
    master = ScriptedMasterModel(stream=True)
    child = FixedInfoModel()
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": (
        master if role == "master" else child
    ))

    async def suggested(question, answer):
        assert CHILD_ANSWER in answer
        return ["还想了解当地交通吗？"]

    monkeypatch.setattr(executor, "recommend_questions", suggested)
    response = TestClient(app).post(
        "/api/chat/recommend-sse-040",
        headers={"Authorization": auth.login("alice", "123456"), "Accept": "text/event-stream"},
        json={"message": "请介绍一处景点"},
    )
    assert response.status_code == 200
    events = [line for line in response.text.splitlines() if line.startswith("event:")]
    assert events[-2:] == ["event: message_id", "event: suggestions"]


# ============================ 031 差旅申请两段式自动提交 ============================


async def _call_tool(toolkit, name: str, **kwargs) -> dict:
    tool = await toolkit.get_tool(name)
    chunk = await tool.call(**kwargs)
    return json.loads(chunk.content[0].text)


def _manage_toolkit(service, user_id: str = "u001", *, request_id: str = "r"):
    return build_manage_toolkit(service, RequestContext(user_id=user_id, session_id="s", request_id=request_id))


DRAFT = dict(destination="上海", departure_city="北京", departure_date="2026-12-01",
             return_date="2026-12-03", purpose="客户拜访")


@pytest.mark.asyncio
async def test_031_submit_is_two_phase_and_idempotent(service):
    toolkit = _manage_toolkit(service)
    first = await _call_tool(toolkit, "submit_travel_approval", **DRAFT)
    assert first["errorCode"] == "CONFIRMATION_REQUIRED"
    token = first["data"]["confirmation_token"]
    assert token and first["data"]["draft"]["destination"] == "上海"
    assert service.list_orders("u001") == []

    confirmed_toolkit = _manage_toolkit(service, request_id="confirmed-user-turn")
    second = await _call_tool(confirmed_toolkit, "submit_travel_approval", **DRAFT, confirmation_token=token)
    assert second["success"] is True
    assert second["data"]["status"] == "SUBMITTED"
    order_id = second["data"]["order_id"]
    assert len(service.list_orders("u001")) == 1

    replay = await _call_tool(confirmed_toolkit, "submit_travel_approval", **DRAFT, confirmation_token=token)
    assert replay["success"] is True and replay["data"]["order_id"] == order_id
    assert len(service.list_orders("u001")) == 1


@pytest.mark.asyncio
async def test_031_token_is_bound_to_draft_and_user(service):
    toolkit = _manage_toolkit(service)
    first = await _call_tool(toolkit, "submit_travel_approval", **DRAFT)
    token = first["data"]["confirmation_token"]

    changed = dict(DRAFT, departure_date="2026-12-05", return_date="2026-12-07")
    stale = await _call_tool(toolkit, "submit_travel_approval", **changed, confirmation_token=token)
    assert stale["errorCode"] == "CONFIRMATION_STALE"
    assert service.list_orders("u001") == []

    other_user = _manage_toolkit(service, "u002")
    foreign = await _call_tool(other_user, "submit_travel_approval", **DRAFT, confirmation_token=token)
    assert foreign["errorCode"] == "CONFIRMATION_INVALID"
    assert service.list_orders("u002") == []

    garbage = await _call_tool(toolkit, "submit_travel_approval", **DRAFT, confirmation_token="forged.token")
    assert garbage["errorCode"] == "CONFIRMATION_INVALID"


@pytest.mark.asyncio
async def test_031_high_conflict_blocks_submit_until_acknowledged(service):
    service.submit_order("u001", SubmitTravelRequest(
        destination="上海", departure_city="北京", departure_date=date(2026, 11, 10),
        return_date=date(2026, 11, 12), purpose="既有行程",
        confirmed=True, idempotency_key="seed-conflict",
    ))
    toolkit = _manage_toolkit(service)
    crossing = dict(destination="杭州", departure_city="北京", departure_date="2026-11-11",
                    return_date="2026-11-13", purpose="另一趟")

    first = await _call_tool(toolkit, "submit_travel_approval", **crossing)
    assert first["errorCode"] == "CONFIRMATION_REQUIRED"
    assert first["data"]["high_conflict"] is True

    confirmed_toolkit = _manage_toolkit(service, request_id="conflict-review-turn")
    blocked = await _call_tool(confirmed_toolkit, "submit_travel_approval", **crossing,
                               confirmation_token=first["data"]["confirmation_token"])
    assert blocked["errorCode"] == "CONFLICT_BLOCKING"
    assert len(service.list_orders("u001")) == 1

    acked = await _call_tool(confirmed_toolkit, "submit_travel_approval", **crossing, acknowledge_conflicts=True)
    final_toolkit = _manage_toolkit(service, request_id="conflict-confirmed-turn")
    done = await _call_tool(final_toolkit, "submit_travel_approval", **crossing,
                            confirmation_token=acked["data"]["confirmation_token"])
    assert done["success"] is True
    assert len(service.list_orders("u001")) == 2
